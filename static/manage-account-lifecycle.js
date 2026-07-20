"use strict";
const lifecycleUpdateQueues = new Map();
function getLifecycleElement(id) {
    return document.getElementById(id);
}
function getManagedAccounts() {
    return typeof allTokens !== "undefined" && Array.isArray(allTokens) ? allTokens : [];
}
function renderAccountActionFeedback(message, type = "status", focus = false) {
    const feedback = getLifecycleElement("accountActionFeedback");
    if (!feedback) {
        return;
    }
    feedback.textContent = String(message || "");
    feedback.setAttribute("role", type === "error" ? "alert" : "status");
    feedback.setAttribute("aria-live", type === "error" ? "assertive" : "polite");
    feedback.className = type === "error"
        ? "mx-4 mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700"
        : type === "success"
            ? "mx-4 mt-4 rounded-md border border-green-200 bg-green-50 p-3 text-sm text-green-700"
            : "mx-4 mt-4 rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-700";
    if (focus) {
        feedback.focus();
    }
}
function normalizeRuntimeMode(value) {
    return value === "persistent" ? "persistent" : "warm";
}
function formatLifecycleDate(value) {
    if (!value) {
        return "-";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return String(value);
    }
    return date.toLocaleString("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
    }).replace(/\//g, "-");
}
function renderLifecycleBadge(label, colorClasses, title = "") {
    const safeLabel = escapeLogHtml(label || "-");
    const safeTitle = escapeLogHtml(title || label || "-");
    return `<span class="inline-flex items-center rounded px-2 py-0.5 text-xs ${colorClasses}" title="${safeTitle}">${safeLabel}</span>`;
}
function getBusinessStatus(account) {
    if (account.is_active) {
        return {
            label: "已加入",
            classes: "bg-green-50 text-green-700",
            title: "账号可接收业务请求",
        };
    }
    const reasonLabels = {
        manual_disabled: "手动停用",
        membership_expired: "会员退役",
        onboarding_pending: "接入中",
        consecutive_errors: "错误停用",
        ST_REVOKED: "登录失效",
        GRANT_EXPIRED: "授权过期",
        "429_rate_limit": "限流停用",
    };
    const reason = account.ban_reason ? String(account.ban_reason) : "未启用";
    return {
        label: reasonLabels[reason] || "未加入",
        classes: reason === "membership_expired"
            ? "bg-amber-50 text-amber-700"
            : "bg-gray-100 text-gray-700",
        title: account.ban_reason ? `停用原因：${reason}` : "账号未加入业务池",
    };
}
function getMembershipStatus(account) {
    const confirmed = String(account.membership_confirmed_status || "active");
    const candidate = String(account.membership_candidate || "unknown");
    const candidateCount = Number(account.membership_candidate_count || 0);
    if (confirmed === "retired") {
        if (candidate === "paid" && candidateCount > 0) {
            return {
                label: `待恢复 ${candidateCount}/2`,
                classes: "bg-blue-50 text-blue-700",
                title: "已观察到付费会员信号，等待连续确认",
            };
        }
        return {
            label: "已退役",
            classes: "bg-gray-100 text-gray-700",
            title: "会员状态已确认退役",
        };
    }
    if (candidate === "free" && candidateCount > 0) {
        return {
            label: `待确认 ${candidateCount}/2`,
            classes: "bg-amber-50 text-amber-700",
            title: "已观察到免费账号信号，等待连续确认",
        };
    }
    return {
        label: "有效",
        classes: "bg-green-50 text-green-700",
        title: "会员状态有效",
    };
}
function getProfileStatus(account) {
    const state = String(account.profile_state || "unprovisioned");
    const labels = {
        ready: "就绪",
        unprovisioned: "未接入",
        provisioning: "接入中",
        failed: "异常",
        missing: "缺失",
    };
    const classes = state === "ready"
        ? "bg-green-50 text-green-700"
        : state === "failed" || state === "missing"
            ? "bg-red-50 text-red-700"
            : "bg-gray-100 text-gray-700";
    const verifiedEmail = account.verified_email
        ? `已验证邮箱：${String(account.verified_email)}`
        : `Profile 状态：${state}`;
    return {
        label: labels[state] || state,
        classes,
        title: verifiedEmail,
    };
}
function renderAccountLifecycleCells(account) {
    const business = getBusinessStatus(account);
    const membership = getMembershipStatus(account);
    const profile = getProfileStatus(account);
    const runtimeMode = normalizeRuntimeMode(account.runtime_mode);
    const keepaliveLabel = account.keepalive_enabled
        ? `开启 · ${runtimeMode === "persistent" ? "常驻" : "按需"}`
        : "关闭";
    const keepaliveClasses = account.keepalive_enabled
        ? "bg-blue-50 text-blue-700"
        : "bg-gray-100 text-gray-700";
    const keepaliveTitle = account.keepalive_enabled
        ? `保活已启用，运行模式：${runtimeMode}`
        : "账号保活未启用";
    const lastKeepaliveTime = formatLifecycleDate(account.last_keepalive_success_at);
    const lastKeepaliveStatus = account.last_keepalive_status
        ? String(account.last_keepalive_status)
        : "尚无成功记录";
    const failureCode = account.last_failure_code
        ? `；最近失败：${String(account.last_failure_code)}`
        : "";
    const nextDue = account.next_due_at
        ? `；下次计划：${formatLifecycleDate(account.next_due_at)}`
        : "";
    const lastKeepaliveTitle = `${lastKeepaliveStatus}${failureCode}${nextDue}`;
    const lastKeepaliveContent = lastKeepaliveTime === "-"
        ? renderLifecycleBadge("无记录", "bg-gray-100 text-gray-700", lastKeepaliveTitle)
        : `<div class="text-xs whitespace-nowrap" title="${escapeLogHtml(lastKeepaliveTitle)}">${escapeLogHtml(lastKeepaliveTime)}</div>`;
    return [
        `<td class="py-3 px-2.5 text-center align-middle whitespace-nowrap">${renderLifecycleBadge(business.label, business.classes, business.title)}</td>`,
        `<td class="py-3 px-2.5 text-center align-middle whitespace-nowrap">${renderLifecycleBadge(membership.label, membership.classes, membership.title)}</td>`,
        `<td class="py-3 px-2.5 text-center align-middle whitespace-nowrap">${renderLifecycleBadge(keepaliveLabel, keepaliveClasses, keepaliveTitle)}</td>`,
        `<td class="py-3 px-2.5 text-center align-middle whitespace-nowrap">${renderLifecycleBadge(profile.label, profile.classes, profile.title)}</td>`,
        `<td class="py-3 px-2.5 text-center align-middle whitespace-nowrap">${lastKeepaliveContent}</td>`,
    ].join("");
}
function renderTokenLifecycleActions(account) {
    const tokenId = Number(account.id);
    if (!Number.isInteger(tokenId) || tokenId <= 0) {
        return "";
    }
    const keepaliveEnabled = Boolean(account.keepalive_enabled);
    const runtimeMode = normalizeRuntimeMode(account.runtime_mode);
    const accountLabel = escapeLogHtml(account.email || `账号 ${tokenId}`);
    const toggleLabel = keepaliveEnabled ? "停保活" : "开保活";
    const nextKeepalive = keepaliveEnabled ? "false" : "true";
    return `<button onclick="validateTokenProfile(${tokenId})" class="inline-flex h-7 items-center justify-center rounded-md px-2 text-xs font-medium hover:bg-emerald-50 hover:text-emerald-700" title="验证 ${accountLabel} 的持久化 Profile">验证 Profile</button><button onclick="openOnboardingModal(${tokenId})" class="inline-flex h-7 items-center justify-center rounded-md px-2 text-xs font-medium hover:bg-purple-50 hover:text-purple-700" title="重新登录 ${accountLabel}">重登</button><button onclick="saveTokenLifecycle(${tokenId},{keepalive_enabled:${nextKeepalive}})" class="inline-flex h-7 items-center justify-center rounded-md px-2 text-xs font-medium hover:bg-blue-50 hover:text-blue-700">${toggleLabel}</button><select onchange="saveTokenLifecycle(${tokenId},{runtime_mode:this.value})" class="h-7 rounded-md border border-input bg-background px-1 text-xs" title="保活运行模式"><option value="warm"${runtimeMode === "warm" ? " selected" : ""}>按需</option><option value="persistent"${runtimeMode === "persistent" ? " selected" : ""}>常驻</option></select><button onclick="exportTokenCredentials(${tokenId})" class="inline-flex h-7 items-center justify-center rounded-md px-2 text-xs font-medium hover:bg-amber-50 hover:text-amber-700">导出凭据</button>`;
}
async function readApiPayload(response) {
    if (!response) {
        return null;
    }
    try {
        return await response.json();
    } catch (_error) {
        return null;
    }
}
function extractApiError(payload, fallbackMessage = "请求失败") {
    if (!payload || typeof payload !== "object") {
        return fallbackMessage;
    }
    const detail = payload.detail;
    if (detail && typeof detail.message === "string" && detail.message.trim()) {
        return detail.message.trim();
    }
    if (typeof detail === "string" && detail.trim()) {
        return detail.trim();
    }
    if (Array.isArray(detail)) {
        const messages = detail
            .map((item) => {
                if (item && typeof item.msg === "string") {
                    return item.msg.trim();
                }
                if (typeof item === "string") {
                    return item.trim();
                }
                return "";
            })
            .filter(Boolean);
        if (messages.length > 0) {
            return messages.join("；");
        }
    }
    if (typeof payload.message === "string" && payload.message.trim()) {
        return payload.message.trim();
    }
    if (payload.error && typeof payload.error.message === "string") {
        return payload.error.message.trim() || fallbackMessage;
    }
    return fallbackMessage;
}
async function requestApiJson(url, options, fallbackMessage) {
    const response = await apiRequest(url, options);
    if (!response) {
        throw new Error("管理员登录已失效，请重新登录");
    }
    const payload = await readApiPayload(response);
    if (!response.ok || !payload || payload.success === false) {
        throw new Error(extractApiError(payload, fallbackMessage));
    }
    return payload;
}
function encodeJsonPayload(value) {
    return JSON.stringify(value);
}
function normalizeLifecycleChanges(changes) {
    if (!changes || typeof changes !== "object" || Array.isArray(changes)) {
        throw new Error("保活设置无效");
    }
    const payload = {};
    if (Object.prototype.hasOwnProperty.call(changes, "keepalive_enabled")) {
        payload.keepalive_enabled = Boolean(changes.keepalive_enabled);
    }
    if (Object.prototype.hasOwnProperty.call(changes, "runtime_mode")) {
        payload.runtime_mode = normalizeRuntimeMode(changes.runtime_mode);
    }
    if (Object.keys(payload).length === 0) {
        throw new Error("没有需要更新的保活设置");
    }
    return payload;
}
async function saveTokenLifecycle(tokenId, changes) {
    const id = Number(tokenId);
    if (!Number.isInteger(id) || id <= 0) {
        showToast("账号 ID 无效", "error");
        renderAccountActionFeedback("保活设置更新失败：账号 ID 无效", "error", true);
        return;
    }
    let payload;
    try {
        payload = normalizeLifecycleChanges(changes);
    } catch (error) {
        const message = `保活设置更新失败：${error.message}`;
        showToast(message, "error");
        renderAccountActionFeedback(message, "error", true);
        return;
    }

    renderAccountActionFeedback("正在更新保活设置", "status");
    const previousUpdate = lifecycleUpdateQueues.get(id) || Promise.resolve();
    const currentUpdate = previousUpdate.catch(() => undefined).then(async () => {
        await requestApiJson(`/api/tokens/${id}/lifecycle`, {
            method: "PUT",
            body: encodeJsonPayload(payload),
        }, "保活设置更新失败");
        await refreshTokens();
        showToast("保活设置已更新", "success");
        renderAccountActionFeedback("保活设置已更新", "success");
    });
    lifecycleUpdateQueues.set(id, currentUpdate);
    try {
        await currentUpdate;
    } catch (error) {
        const message = `保活设置更新失败：${error.message}`;
        showToast(message, "error");
        renderAccountActionFeedback(message, "error", true);
    } finally {
        if (lifecycleUpdateQueues.get(id) === currentUpdate) {
            lifecycleUpdateQueues.delete(id);
        }
    }
}
async function validateTokenProfile(tokenId) {
    const id = Number(tokenId);
    if (!Number.isInteger(id) || id <= 0) {
        showToast("账号 ID 无效", "error");
        renderAccountActionFeedback("Profile 验证失败：账号 ID 无效", "error", true);
        return;
    }
    try {
        showToast("正在验证持久化 Profile", "info");
        renderAccountActionFeedback("正在验证持久化 Profile", "status");
        const payload = await requestApiJson(`/api/tokens/${id}/validate-profile`, {
            method: "POST",
        }, "Profile 验证失败");
        const profile = payload.profile;
        if (!profile || typeof profile !== "object") {
            throw new Error("服务器未返回 Profile 验证结果");
        }
        const tier = profile.tier || "未知层级";
        const expiry = formatLifecycleDate(profile.expiry);
        const readiness = profile.profile_ready ? "已就绪" : "未就绪";
        const message = `Profile 验证通过：邮箱 ${profile.email}；层级 ${tier}；余额 ${profile.credits}；项目数量 ${profile.project_count}；Profile ${readiness}；授权到期 ${expiry}`;
        showToast("Profile 验证通过，详细结果已显示", "success");
        renderAccountActionFeedback(message, "success", true);
    } catch (error) {
        const message = `Profile 验证失败：${error.message}`;
        showToast(message, "error");
        renderAccountActionFeedback(message, "error", true);
    }
}
async function exportTokenCredentials(tokenId) {
    const id = Number(tokenId);
    if (!Number.isInteger(id) || id <= 0) {
        showToast("账号 ID 无效", "error");
        return;
    }
    if (!window.confirm("导出的文件包含该账号的登录凭据。请确认仅保存到受信任设备，并在使用后妥善保管。是否继续？")) {
        return;
    }
    let objectUrl = null;
    try {
        const payload = await requestApiJson(`/api/tokens/${tokenId}/export`, {
            method: "POST",
        }, "凭据导出失败");
        const credentials = payload.token;
        if (!credentials || typeof credentials !== "object") {
            throw new Error("服务器未返回可导出的凭据");
        }
        const exportRecord = {
            email: credentials.email || null,
            access_token: credentials.at || null,
            session_token: credentials.st || null,
            at_expires: credentials.at_expires || null,
        };
        const dataBlob = new Blob([JSON.stringify([exportRecord], null, 2)], {
            type: "application/json;charset=utf-8",
        });
        objectUrl = URL.createObjectURL(dataBlob);
        const link = document.createElement("a");
        const safeEmail = String(credentials.email || `account-${id}`)
            .replace(/[^A-Za-z0-9@._-]+/g, "_")
            .slice(0, 80);
        link.href = objectUrl;
        link.download = `flow2api-${safeEmail}-credentials.json`;
        link.rel = "noopener";
        document.body.appendChild(link);
        link.click();
        link.remove();
        showToast("账号凭据已导出", "success");
    } catch (error) {
        showToast(`凭据导出失败：${error.message}`, "error");
    } finally {
        if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
        }
    }
}
window.renderAccountLifecycleCells = renderAccountLifecycleCells;
window.renderTokenLifecycleActions = renderTokenLifecycleActions;
window.saveTokenLifecycle = saveTokenLifecycle;
window.validateTokenProfile = validateTokenProfile;
window.exportTokenCredentials = exportTokenCredentials;
window.extractApiError = extractApiError;
