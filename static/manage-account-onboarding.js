"use strict";
const ONBOARDING_POLL_DELAY_MS = 2000;
const ONBOARDING_POLL_TERMINAL_STATES = new Set(["completed", "cancelled", "failed"]);
const ONBOARDING_LOCKED_CANCEL_PHASES = new Set([
    "validating_destination",
    "account_commit",
    "commit_complete",
]);
const ONBOARDING_FOCUSABLE_SELECTOR = [
    "button:not([disabled])",
    "select:not([disabled])",
    "input:not([disabled])",
    "textarea:not([disabled])",
    "a[href]",
    "[tabindex]:not([tabindex='-1'])",
].join(",");
let onboardingCurrentJob = null;
let onboardingPollTimer = null;
let onboardingPollRequestId = 0;
let onboardingActionInFlight = false;
let onboardingConfiguredDisplay = null;
let onboardingConfigReady = false;
let onboardingPreviouslyFocusedElement = null;
let onboardingBackgroundState = [];
function isOnboardingModalOpen() {
    const modal = getLifecycleElement("onboardingModal");
    return Boolean(modal && !modal.classList.contains("hidden"));
}
function setOnboardingBackgroundInert(enabled) {
    const modal = getLifecycleElement("onboardingModal");
    if (!modal) {
        return;
    }
    if (enabled) {
        onboardingBackgroundState = Array.from(document.body.children)
            .filter((element) => element !== modal && !["SCRIPT", "STYLE"].includes(element.tagName))
            .map((element) => ({
                element,
                inert: Boolean(element.inert),
                ariaHidden: element.getAttribute("aria-hidden"),
            }));
        for (const state of onboardingBackgroundState) {
            state.element.inert = true;
            state.element.setAttribute("aria-hidden", "true");
        }
        return;
    }
    for (const state of onboardingBackgroundState) {
        state.element.inert = state.inert;
        if (state.ariaHidden === null) {
            state.element.removeAttribute("aria-hidden");
        } else {
            state.element.setAttribute("aria-hidden", state.ariaHidden);
        }
    }
    onboardingBackgroundState = [];
}
function getOnboardingFocusableElements() {
    const modal = getLifecycleElement("onboardingModal");
    if (!modal) {
        return [];
    }
    return Array.from(modal.querySelectorAll(ONBOARDING_FOCUSABLE_SELECTOR))
        .filter((element) => !element.disabled && element.getAttribute("aria-hidden") !== "true");
}
function focusOnboardingInitialControl() {
    const target = getLifecycleElement("onboardingTargetToken");
    const modal = getLifecycleElement("onboardingModal");
    if (target && !target.disabled) {
        target.focus();
    } else if (modal) {
        modal.focus();
    }
}
function handleOnboardingDialogKeydown(event) {
    if (!isOnboardingModalOpen()) {
        return;
    }
    if (event.key === "Escape") {
        event.preventDefault();
        closeOnboardingModal();
        return;
    }
    if (event.key !== "Tab") {
        return;
    }
    const focusable = getOnboardingFocusableElements();
    if (focusable.length === 0) {
        event.preventDefault();
        getLifecycleElement("onboardingModal").focus();
        return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
}
function populateOnboardingTargets(selectedTokenId = null) {
    const select = getLifecycleElement("onboardingTargetToken");
    if (!select) {
        return;
    }
    select.replaceChildren();
    const createOption = document.createElement("option");
    createOption.value = "";
    createOption.textContent = "新账号（登录后自动识别）";
    select.appendChild(createOption);
    for (const account of getManagedAccounts()) {
        const tokenId = Number(account.id);
        if (!Number.isInteger(tokenId) || tokenId <= 0) {
            continue;
        }
        const option = document.createElement("option");
        option.value = String(tokenId);
        option.textContent = `${account.email || `账号 ${tokenId}`}（#${tokenId}）`;
        select.appendChild(option);
    }
    select.value = selectedTokenId ? String(selectedTokenId) : "";
}
function setOnboardingConfigDisabled(disabled) {
    for (const id of [
        "onboardingTargetToken",
        "onboardingConflictPolicy",
        "onboardingBusinessEnabled",
        "onboardingKeepaliveEnabled",
        "onboardingRuntimeMode",
    ]) {
        const element = getLifecycleElement(id);
        if (element) {
            element.disabled = disabled;
        }
    }
}
function setOnboardingButtons(job = onboardingCurrentJob) {
    const startButton = getLifecycleElement("onboardingStartBtn");
    const finalizeButton = getLifecycleElement("onboardingFinalizeBtn");
    const cancelButton = getLifecycleElement("onboardingCancelBtn");
    if (!startButton || !finalizeButton || !cancelButton) {
        return;
    }
    const state = job ? String(job.state || "pending") : "";
    const phase = job ? String(job.phase || "created") : "";
    const canStart = onboardingConfigReady && (!job || state === "pending");
    const canFinalize = state === "running" || state === "failed";
    const canCancel = Boolean(job)
        && !["completed", "cancelled"].includes(state)
        && !ONBOARDING_LOCKED_CANCEL_PHASES.has(phase);
    startButton.disabled = onboardingActionInFlight || !canStart;
    finalizeButton.disabled = onboardingActionInFlight || !canFinalize;
    cancelButton.disabled = onboardingActionInFlight || !canCancel;
    startButton.textContent = onboardingActionInFlight ? "处理中" : "启动";
    finalizeButton.textContent = onboardingActionInFlight ? "处理中" : "完成接入";
    cancelButton.textContent = onboardingActionInFlight ? "处理中" : "取消任务";
}
function getOnboardingStatePresentation(state) {
    const presentations = {
        pending: ["待启动", "bg-gray-100 text-gray-700"],
        running: ["进行中", "bg-blue-50 text-blue-700"],
        completed: ["已完成", "bg-green-50 text-green-700"],
        cancelled: ["已取消", "bg-gray-100 text-gray-700"],
        failed: ["失败", "bg-red-50 text-red-700"],
    };
    return presentations[state] || [state || "未知", "bg-gray-100 text-gray-700"];
}
function getOnboardingPhaseLabel(phase) {
    const labels = {
        created: "任务已创建",
        browser_start: "启动浏览器",
        awaiting_login: "等待 XRDP 登录",
        stop_browser: "关闭接入浏览器",
        verify_account: "验证登录账号",
        migrate_profile: "迁移 Profile",
        validating_destination: "验证持久化 Profile",
        final_validation: "最终身份校验",
        account_commit: "提交账号状态",
        commit_complete: "账号状态已提交",
        completed: "接入完成",
        recovery: "恢复任务",
        cancel: "取消任务",
        cancelled: "任务已取消",
    };
    return labels[phase] || phase || "-";
}
function getOnboardingConflictStatusLabel(status) {
    const labels = {
        no_conflict: "无冲突",
        rejected: "已拒绝覆盖",
        archived_and_replaced: "已归档并替换",
    };
    return labels[status] || status || "-";
}
function renderOnboardingEmpty(message = "选择接入参数后点击“启动”。") {
    const status = getLifecycleElement("onboardingJobStatus");
    if (!status) {
        return;
    }
    status.innerHTML = `<div class="flex items-center justify-between gap-3"><p class="text-sm font-medium">任务状态</p>${renderLifecycleBadge("尚未创建", "bg-gray-100 text-gray-700")}</div><p class="mt-2 text-xs text-muted-foreground">${escapeLogHtml(message)}</p>`;
    setOnboardingButtons(null);
}
function renderOnboardingLoading(message) {
    const status = getLifecycleElement("onboardingJobStatus");
    if (!status) {
        return;
    }
    status.innerHTML = `<div class="flex items-center justify-between gap-3"><p class="text-sm font-medium">任务状态</p>${renderLifecycleBadge("加载中", "bg-blue-50 text-blue-700")}</div><p class="mt-2 text-xs text-muted-foreground">${escapeLogHtml(message)}</p>`;
}
function renderOnboardingJob(job) {
    const status = getLifecycleElement("onboardingJobStatus");
    if (!status || !job) {
        return;
    }
    const state = String(job.state || "pending");
    const phase = String(job.phase || "created");
    const [stateLabel, stateClasses] = getOnboardingStatePresentation(state);
    const discoveredEmail = job.discovered_email ? String(job.discovered_email) : "-";
    const discoveredTier = job.discovered_tier ? String(job.discovered_tier) : "-";
    const discoveredCredits = job.discovered_credits === null || job.discovered_credits === undefined
        ? "-"
        : String(job.discovered_credits);
    const discoveredExpiry = formatLifecycleDate(job.discovered_at_expires);
    const projectCount = job.project_count === null || job.project_count === undefined
        ? "-"
        : String(job.project_count);
    const profileReady = job.profile_ready === null || job.profile_ready === undefined
        ? "-"
        : job.profile_ready ? "是" : "否";
    const conflictStatus = getOnboardingConflictStatusLabel(job.conflict_status);
    const errorBlock = job.error_message || job.error_code
        ? `<div class="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-xs text-red-700"><p class="font-medium">任务错误</p><p class="mt-1 break-words">${escapeLogHtml(job.error_message || job.error_code)}</p>${job.error_code ? `<p class="mt-1 text-red-600">代码：${escapeLogHtml(job.error_code)}</p>` : ""}</div>`
        : "";
    status.innerHTML = `<div class="flex items-center justify-between gap-3"><div><p class="text-sm font-medium">任务状态</p><p class="mt-1 text-xs text-muted-foreground">任务 ${escapeLogHtml(job.job_id || "-")}</p></div>${renderLifecycleBadge(stateLabel, stateClasses, stateLabel)}</div><div class="mt-4 grid gap-3 text-xs sm:grid-cols-2"><div><span class="text-muted-foreground">当前阶段：</span><span class="font-medium">${escapeLogHtml(getOnboardingPhaseLabel(phase))}</span></div><div><span class="text-muted-foreground">目标账号：</span><span class="font-medium">${escapeLogHtml(job.target_token_id || "新账号")}</span></div><div><span class="text-muted-foreground">发现邮箱：</span><span class="font-medium">${escapeLogHtml(discoveredEmail)}</span></div><div><span class="text-muted-foreground">发现会员层级：</span><span class="font-medium">${escapeLogHtml(discoveredTier)}</span></div><div><span class="text-muted-foreground">发现余额：</span><span class="font-medium">${escapeLogHtml(discoveredCredits)}</span></div><div><span class="text-muted-foreground">授权到期：</span><span class="font-medium">${escapeLogHtml(discoveredExpiry)}</span></div><div><span class="text-muted-foreground">项目数量：</span><span class="font-medium">${escapeLogHtml(projectCount)}</span></div><div><span class="text-muted-foreground">Profile 就绪：</span><span class="font-medium">${escapeLogHtml(profileReady)}</span></div><div><span class="text-muted-foreground">冲突处理：</span><span class="font-medium">${escapeLogHtml(conflictStatus)}</span></div><div><span class="text-muted-foreground">任务到期：</span><span class="font-medium">${escapeLogHtml(formatLifecycleDate(job.expires_at))}</span></div><div><span class="text-muted-foreground">最近更新：</span><span class="font-medium">${escapeLogHtml(formatLifecycleDate(job.updated_at))}</span></div></div>${errorBlock}`;
    setOnboardingButtons(job);
}
function applyOnboardingJobChoices(job) {
    const target = getLifecycleElement("onboardingTargetToken");
    const conflictPolicy = getLifecycleElement("onboardingConflictPolicy");
    const businessEnabled = getLifecycleElement("onboardingBusinessEnabled");
    const keepaliveEnabled = getLifecycleElement("onboardingKeepaliveEnabled");
    const runtimeMode = getLifecycleElement("onboardingRuntimeMode");
    if (target) {
        target.value = job.target_token_id ? String(job.target_token_id) : "";
    }
    if (conflictPolicy) {
        conflictPolicy.value = job.conflict_policy === "archive_and_replace"
            ? "archive_and_replace"
            : "reject";
    }
    if (businessEnabled) {
        businessEnabled.checked = Boolean(job.requested_business_enabled);
    }
    if (keepaliveEnabled) {
        keepaliveEnabled.checked = Boolean(job.requested_keepalive_enabled);
    }
    if (runtimeMode) {
        runtimeMode.value = normalizeRuntimeMode(job.requested_runtime_mode);
    }
}
function setOnboardingCurrentJob(job) {
    onboardingCurrentJob = job || null;
    if (onboardingCurrentJob) {
        applyOnboardingJobChoices(onboardingCurrentJob);
        setOnboardingConfigDisabled(true);
        renderOnboardingJob(onboardingCurrentJob);
    } else {
        setOnboardingConfigDisabled(false);
        renderOnboardingEmpty();
    }
}
function stopOnboardingPolling() {
    onboardingPollRequestId += 1;
    if (onboardingPollTimer !== null) {
        window.clearTimeout(onboardingPollTimer);
        onboardingPollTimer = null;
    }
}
function shouldContinueOnboardingPolling(job) {
    return Boolean(job)
        && !ONBOARDING_POLL_TERMINAL_STATES.has(String(job.state || ""))
        && isOnboardingModalOpen()
        && !document.hidden;
}
function scheduleOnboardingPoll(jobId, requestId) {
    if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
        return;
    }
    onboardingPollTimer = window.setTimeout(async () => {
        onboardingPollTimer = null;
        await pollOnboardingJob(jobId, requestId);
    }, ONBOARDING_POLL_DELAY_MS);
}
async function pollOnboardingJob(jobId, requestId) {
    if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen() || document.hidden) {
        return;
    }
    try {
        const payload = await requestApiJson(
            `/api/onboarding/jobs/${encodeURIComponent(jobId)}`,
            { method: "GET" },
            "接入任务状态读取失败",
        );
        if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
            return;
        }
        onboardingCurrentJob = payload.job;
        renderOnboardingJob(onboardingCurrentJob);
        if (shouldContinueOnboardingPolling(onboardingCurrentJob)) {
            scheduleOnboardingPoll(jobId, requestId);
        }
    } catch (error) {
        if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
            return;
        }
        showToast(`接入任务状态读取失败：${error.message}`, "error");
        scheduleOnboardingPoll(jobId, requestId);
    }
}
function startOnboardingPolling(job) {
    stopOnboardingPolling();
    if (!shouldContinueOnboardingPolling(job) || !job.job_id) {
        return;
    }
    const requestId = onboardingPollRequestId;
    scheduleOnboardingPoll(String(job.job_id), requestId);
}
function getOnboardingTargetMatchRank(job, targetTokenId) {
    const targetId = job.target_token_id === null || job.target_token_id === undefined
        ? null
        : Number(job.target_token_id);
    const resolvedId = job.resolved_token_id === null || job.resolved_token_id === undefined
        ? null
        : Number(job.resolved_token_id);
    if (targetTokenId === null) {
        return targetId === null && resolvedId === null ? 0 : null;
    }
    const selectedId = Number(targetTokenId);
    if (targetId === selectedId) {
        return 0;
    }
    if (targetId === null && resolvedId === selectedId) {
        return 1;
    }
    return null;
}
function sortOnboardingJobsForTarget(jobs, targetTokenId) {
    return [...jobs].sort((left, right) => {
        const targetRank = getOnboardingTargetMatchRank(left, targetTokenId);
        const rightRank = getOnboardingTargetMatchRank(right, targetTokenId);
        if (targetRank !== rightRank) {
            return targetRank - rightRank;
        }
        const leftTime = new Date(left.updated_at || left.created_at || 0).getTime();
        const rightTime = new Date(right.updated_at || right.created_at || 0).getTime();
        if (leftTime !== rightTime) {
            return rightTime - leftTime;
        }
        return String(right.job_id || "").localeCompare(String(left.job_id || ""));
    });
}
async function loadResumableOnboardingJob(targetTokenId, requestId) {
    const queries = targetTokenId === null
        ? [""]
        : [
            `?target_token_id=${encodeURIComponent(targetTokenId)}`,
            `?resolved_token_id=${encodeURIComponent(targetTokenId)}`,
        ];
    const payloads = await Promise.all(queries.map((query) => requestApiJson(
        `/api/onboarding/jobs${query}`,
        { method: "GET" },
        "接入任务列表读取失败",
    )));
    if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
        return;
    }
    const jobsById = new Map();
    for (const payload of payloads) {
        for (const job of Array.isArray(payload.jobs) ? payload.jobs : []) {
            jobsById.set(String(job.job_id || ""), job);
        }
    }
    const resumable = sortOnboardingJobsForTarget(
        Array.from(jobsById.values()).filter((job) =>
            getOnboardingTargetMatchRank(job, targetTokenId) !== null
            && !["completed", "cancelled"].includes(String(job.state || "")),
        ),
        targetTokenId,
    )[0];
    setOnboardingCurrentJob(resumable || null);
    if (resumable) {
        startOnboardingPolling(resumable);
    }
}
async function recoverAndLoadOnboardingJob(targetTokenId) {
    stopOnboardingPolling();
    const requestId = onboardingPollRequestId;
    renderOnboardingLoading("正在检查可恢复的接入任务。");
    try {
        await requestApiJson(
            "/api/onboarding/recover",
            { method: "POST" },
            "接入任务恢复失败",
        );
        if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
            return;
        }
        await loadResumableOnboardingJob(targetTokenId, requestId);
    } catch (error) {
        if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
            return;
        }
        setOnboardingCurrentJob(null);
        showToast(`接入任务加载失败：${error.message}`, "error");
    }
}
async function loadOnboardingSafeConfig(requestId) {
    const payload = await requestApiJson(
        "/api/onboarding/config",
        { method: "GET" },
        "账号接入配置读取失败",
    );
    if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
        return false;
    }
    const display = String(payload.config && payload.config.display || "").trim();
    if (!/^:[0-9]+(?:\.[0-9]+)?$/.test(display)) {
        throw new Error("服务器返回的 XRDP 显示器配置无效");
    }
    onboardingConfiguredDisplay = display;
    onboardingConfigReady = true;
    const displayElement = getLifecycleElement("onboardingDisplayValue");
    if (displayElement) {
        displayElement.textContent = display;
    }
    setOnboardingButtons(onboardingCurrentJob);
    return true;
}
async function openOnboardingModal(tokenId = null) {
    const modal = getLifecycleElement("onboardingModal");
    if (!modal) {
        showToast("账号接入窗口不可用", "error");
        return;
    }
    stopOnboardingPolling();
    const requestId = onboardingPollRequestId;
    onboardingPreviouslyFocusedElement = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    onboardingCurrentJob = null;
    onboardingActionInFlight = false;
    onboardingConfiguredDisplay = null;
    onboardingConfigReady = false;
    populateOnboardingTargets(tokenId);
    getLifecycleElement("onboardingConflictPolicy").value = "reject";
    getLifecycleElement("onboardingBusinessEnabled").checked = false;
    getLifecycleElement("onboardingKeepaliveEnabled").checked = false;
    getLifecycleElement("onboardingRuntimeMode").value = "warm";
    getLifecycleElement("onboardingDisplayValue").textContent = "加载中";
    setOnboardingConfigDisabled(false);
    renderOnboardingEmpty();
    setOnboardingBackgroundInert(true);
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    focusOnboardingInitialControl();
    try {
        const loaded = await loadOnboardingSafeConfig(requestId);
        if (!loaded) {
            return;
        }
        await recoverAndLoadOnboardingJob(tokenId);
    } catch (error) {
        if (requestId === onboardingPollRequestId && isOnboardingModalOpen()) {
            renderOnboardingEmpty("无法读取安全接入配置，请稍后重试。");
            showToast(`账号接入配置读取失败：${error.message}`, "error");
        }
    }
}
function closeOnboardingModal() {
    stopOnboardingPolling();
    onboardingCurrentJob = null;
    onboardingActionInFlight = false;
    onboardingConfiguredDisplay = null;
    onboardingConfigReady = false;
    const modal = getLifecycleElement("onboardingModal");
    if (modal) {
        modal.classList.add("hidden");
        modal.setAttribute("aria-hidden", "true");
    }
    setOnboardingBackgroundInert(false);
    if (
        onboardingPreviouslyFocusedElement
        && document.contains(onboardingPreviouslyFocusedElement)
    ) {
        onboardingPreviouslyFocusedElement.focus();
    }
    onboardingPreviouslyFocusedElement = null;
}
function handleOnboardingTargetChange() {
    if (onboardingCurrentJob) {
        return;
    }
    stopOnboardingPolling();
    renderOnboardingEmpty("目标已更新，点击“启动”创建新的接入任务。");
}
function getOnboardingCreateRequest() {
    const targetValue = getLifecycleElement("onboardingTargetToken").value;
    const targetTokenId = targetValue ? Number(targetValue) : null;
    if (targetValue && (!Number.isInteger(targetTokenId) || targetTokenId <= 0)) {
        throw new Error("接入目标无效");
    }
    return {
        target_token_id: targetTokenId,
        conflict_policy: getLifecycleElement("onboardingConflictPolicy").value === "archive_and_replace"
            ? "archive_and_replace"
            : "reject",
        requested_business_enabled: getLifecycleElement("onboardingBusinessEnabled").checked,
        requested_keepalive_enabled: getLifecycleElement("onboardingKeepaliveEnabled").checked,
        requested_runtime_mode: normalizeRuntimeMode(
            getLifecycleElement("onboardingRuntimeMode").value,
        ),
    };
}
async function startOnboardingJob() {
    if (onboardingActionInFlight) {
        return;
    }
    if (!onboardingConfigReady || !onboardingConfiguredDisplay) {
        showToast("账号接入配置尚未就绪", "error");
        return;
    }
    stopOnboardingPolling();
    const requestId = onboardingPollRequestId;
    onboardingActionInFlight = true;
    setOnboardingButtons(onboardingCurrentJob);
    try {
        let job = onboardingCurrentJob;
        if (!job) {
            const createPayload = await requestApiJson(
                "/api/onboarding/jobs",
                {
                    method: "POST",
                    body: JSON.stringify(getOnboardingCreateRequest()),
                },
                "接入任务创建失败",
            );
            if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
                return;
            }
            job = createPayload.job;
            onboardingCurrentJob = job;
            setOnboardingConfigDisabled(true);
            renderOnboardingJob(job);
        }
        if (String(job.state || "") !== "pending") {
            throw new Error("当前任务不是待启动状态");
        }
        const startPayload = await requestApiJson(
            `/api/onboarding/jobs/${encodeURIComponent(job.job_id)}/start`,
            { method: "POST" },
            "接入浏览器启动失败",
        );
        if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
            return;
        }
        onboardingCurrentJob = startPayload.job;
        onboardingActionInFlight = false;
        renderOnboardingJob(onboardingCurrentJob);
        showToast(`接入浏览器已启动，请前往 XRDP 显示器 ${onboardingConfiguredDisplay} 完成登录`, "success");
        startOnboardingPolling(onboardingCurrentJob);
    } catch (error) {
        if (requestId === onboardingPollRequestId && isOnboardingModalOpen()) {
            showToast(`账号接入启动失败：${error.message}`, "error");
            if (onboardingCurrentJob && onboardingCurrentJob.job_id) {
                await refreshCurrentOnboardingJobAfterError(
                    String(onboardingCurrentJob.job_id),
                    requestId,
                );
            } else if (onboardingCurrentJob) {
                renderOnboardingJob(onboardingCurrentJob);
            }
        }
    } finally {
        if (requestId === onboardingPollRequestId && isOnboardingModalOpen()) {
            onboardingActionInFlight = false;
            setOnboardingButtons(onboardingCurrentJob);
        }
    }
}
async function refreshCurrentOnboardingJobAfterError(jobId, requestId) {
    try {
        const payload = await requestApiJson(
            `/api/onboarding/jobs/${encodeURIComponent(jobId)}`,
            { method: "GET" },
            "接入任务状态读取失败",
        );
        if (requestId === onboardingPollRequestId && isOnboardingModalOpen()) {
            onboardingCurrentJob = payload.job;
            renderOnboardingJob(onboardingCurrentJob);
        }
    } catch (_error) {
        return;
    }
}
async function finalizeOnboardingJob() {
    if (onboardingActionInFlight || !onboardingCurrentJob || !onboardingCurrentJob.job_id) {
        return;
    }
    stopOnboardingPolling();
    const requestId = onboardingPollRequestId;
    const jobId = String(onboardingCurrentJob.job_id);
    onboardingActionInFlight = true;
    setOnboardingButtons(onboardingCurrentJob);
    try {
        const payload = await requestApiJson(
            `/api/onboarding/jobs/${encodeURIComponent(jobId)}/finalize`,
            { method: "POST" },
            "账号接入完成失败",
        );
        if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
            return;
        }
        onboardingCurrentJob = payload.job;
        renderOnboardingJob(onboardingCurrentJob);
        await refreshTokens();
        if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
            return;
        }
        onboardingActionInFlight = false;
        renderOnboardingJob(onboardingCurrentJob);
        showToast("账号接入已完成", "success");
        startOnboardingPolling(onboardingCurrentJob);
    } catch (error) {
        if (requestId === onboardingPollRequestId && isOnboardingModalOpen()) {
            showToast(`账号接入完成失败：${error.message}`, "error");
            await refreshCurrentOnboardingJobAfterError(jobId, requestId);
        }
    } finally {
        if (requestId === onboardingPollRequestId && isOnboardingModalOpen()) {
            onboardingActionInFlight = false;
            setOnboardingButtons(onboardingCurrentJob);
        }
    }
}
async function cancelOnboardingJob() {
    if (onboardingActionInFlight || !onboardingCurrentJob || !onboardingCurrentJob.job_id) {
        return;
    }
    if (!window.confirm("确定取消当前账号接入任务吗？服务器只会停止该任务拥有的接入浏览器。")) {
        return;
    }
    stopOnboardingPolling();
    const requestId = onboardingPollRequestId;
    const jobId = String(onboardingCurrentJob.job_id);
    onboardingActionInFlight = true;
    setOnboardingButtons(onboardingCurrentJob);
    try {
        const payload = await requestApiJson(
            `/api/onboarding/jobs/${encodeURIComponent(jobId)}/cancel`,
            { method: "POST" },
            "接入任务取消失败",
        );
        if (requestId !== onboardingPollRequestId || !isOnboardingModalOpen()) {
            return;
        }
        onboardingCurrentJob = payload.job;
        renderOnboardingJob(onboardingCurrentJob);
        showToast("账号接入任务已取消", "success");
    } catch (error) {
        if (requestId === onboardingPollRequestId && isOnboardingModalOpen()) {
            showToast(`接入任务取消失败：${error.message}`, "error");
            await refreshCurrentOnboardingJobAfterError(jobId, requestId);
        }
    } finally {
        if (requestId === onboardingPollRequestId && isOnboardingModalOpen()) {
            onboardingActionInFlight = false;
            setOnboardingButtons(onboardingCurrentJob);
        }
    }
}
function handleAccountLifecycleTabSwitch() {
    if (isOnboardingModalOpen()) {
        closeOnboardingModal();
    } else {
        stopOnboardingPolling();
    }
}
function handleOnboardingVisibilityChange() {
    if (document.hidden) {
        stopOnboardingPolling();
        return;
    }
    if (isOnboardingModalOpen() && shouldContinueOnboardingPolling(onboardingCurrentJob)) {
        startOnboardingPolling(onboardingCurrentJob);
    }
}
window.openOnboardingModal = openOnboardingModal;
window.closeOnboardingModal = closeOnboardingModal;
window.handleOnboardingTargetChange = handleOnboardingTargetChange;
window.startOnboardingJob = startOnboardingJob;
window.finalizeOnboardingJob = finalizeOnboardingJob;
window.cancelOnboardingJob = cancelOnboardingJob;
window.stopOnboardingPolling = stopOnboardingPolling;
window.handleAccountLifecycleTabSwitch = handleAccountLifecycleTabSwitch;
document.addEventListener("keydown", handleOnboardingDialogKeydown);
document.addEventListener("visibilitychange", handleOnboardingVisibilityChange);
window.addEventListener("pagehide", stopOnboardingPolling);
window.addEventListener("beforeunload", stopOnboardingPolling);
