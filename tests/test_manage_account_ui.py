"""Static management UI contracts for onboarding and account lifecycle controls."""

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).parents[1]
MANAGE_HTML = PROJECT_ROOT / "static" / "manage.html"
LIFECYCLE_JS = PROJECT_ROOT / "static" / "manage-account-lifecycle.js"
ONBOARDING_JS = PROJECT_ROOT / "static" / "manage-account-onboarding.js"


def test_manage_page_exposes_onboarding_modal_and_lifecycle_columns():
    html = MANAGE_HTML.read_text(encoding="utf-8")

    assert 'onclick="openOnboardingModal()"' in html
    assert 'id="onboardingModal"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="onboardingDialogTitle"' in html
    assert 'id="onboardingDialogTitle"' in html
    assert 'id="onboardingDisplayValue"' in html
    assert 'id="onboardingTargetToken"' in html
    assert 'id="onboardingConflictPolicy"' in html
    assert 'id="onboardingBusinessEnabled"' in html
    assert 'id="onboardingKeepaliveEnabled"' in html
    assert 'id="onboardingRuntimeMode"' in html
    assert 'id="onboardingJobStatus"' in html
    assert 'id="onboardingStartBtn"' in html
    assert 'id="onboardingFinalizeBtn"' in html
    assert 'id="onboardingCancelBtn"' in html
    assert 'id="accountActionFeedback"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert 'tabindex="0"' in html
    for heading in ("业务池", "会员", "保活", "Profile", "最近保活"):
        assert heading in html
    assert '<script src="/static/manage-account-lifecycle.js"></script>' in html
    assert '<script src="/static/manage-account-onboarding.js"></script>' in html


def test_metadata_edit_no_longer_requires_or_prefills_session_token():
    html = MANAGE_HTML.read_text(encoding="utf-8")

    st_field = re.search(r'<textarea id="editTokenST"(?P<attrs>[^>]*)>', html)
    assert st_field is not None
    assert "required" not in st_field.group("attrs")
    assert "留空" in st_field.group("attrs") or "可选" in st_field.group("attrs")
    assert "document.getElementById('editTokenST').value=t.st" not in html
    assert "editTokenAT" not in html
    assert "addTokenAT" not in html


def test_lifecycle_script_uses_safe_endpoints_and_nonoverlapping_polling():
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (LIFECYCLE_JS, ONBOARDING_JS)
    )

    assert "/api/onboarding/jobs" in source
    assert "/start" in source
    assert "/finalize" in source
    assert "/cancel" in source
    assert "/api/tokens/${id}/lifecycle" in source
    assert "/api/tokens/${tokenId}/export" in source
    assert "setTimeout" in source
    assert "setInterval" not in source
    assert "onboardingPollRequestId" in source
    assert "extractApiError" in source
    assert "detail.message" in source
    assert "escapeLogHtml" in source


def test_profile_validation_action_and_onboarding_config_use_safe_authenticated_endpoints():
    lifecycle_source = LIFECYCLE_JS.read_text(encoding="utf-8")
    onboarding_source = ONBOARDING_JS.read_text(encoding="utf-8")
    html = MANAGE_HTML.read_text(encoding="utf-8")

    assert "validateTokenProfile" in lifecycle_source
    assert "/api/tokens/${id}/validate-profile" in lifecycle_source
    assert "验证 Profile" in lifecycle_source
    assert "/api/onboarding/config" in onboarding_source
    assert "onboardingDisplayValue" in onboarding_source
    assert "onboardingConfiguredDisplay" in onboarding_source
    assert "XRDP 显示器 ${onboardingConfiguredDisplay}" in onboarding_source
    onboarding_markup = html.split('id="onboardingModal"', 1)[1].split("<!-- Token 导入模态框 -->", 1)[0]
    assert ":11" not in onboarding_markup
    assert ":11" not in onboarding_source


def test_lifecycle_and_profile_feedback_use_persistent_accessible_region():
    lifecycle_source = LIFECYCLE_JS.read_text(encoding="utf-8")

    assert "renderAccountActionFeedback" in lifecycle_source
    assert 'feedback.setAttribute("role", type === "error" ? "alert" : "status")' in lifecycle_source
    assert 'feedback.setAttribute("aria-live", type === "error" ? "assertive" : "polite")' in lifecycle_source
    assert "feedback.focus()" in lifecycle_source
    assert "Profile 验证通过" in lifecycle_source
    assert "profile.email" in lifecycle_source
    assert "profile.tier" in lifecycle_source
    assert "profile.credits" in lifecycle_source
    assert "profile.project_count" in lifecycle_source
    assert "profile.profile_ready" in lifecycle_source
    assert "profile.expiry" in lifecycle_source


def test_start_failure_reloads_persisted_backend_job():
    source = ONBOARDING_JS.read_text(encoding="utf-8")
    start_function = re.search(
        r"async function startOnboardingJob\(\)[\s\S]*?\n}\nasync function refreshCurrentOnboardingJobAfterError",
        source,
    )

    assert start_function is not None
    assert "refreshCurrentOnboardingJobAfterError" in start_function.group(0)


def test_onboarding_job_panel_renders_persisted_result_metadata():
    source = ONBOARDING_JS.read_text(encoding="utf-8")

    assert "job.project_count" in source
    assert "job.profile_ready" in source
    assert "job.conflict_status" in source
    assert "项目数量" in source
    assert "Profile 就绪" in source
    assert "冲突处理" in source


def test_onboarding_dialog_traps_focus_restores_focus_and_inerts_background():
    source = ONBOARDING_JS.read_text(encoding="utf-8")

    assert "onboardingPreviouslyFocusedElement" in source
    assert "setOnboardingBackgroundInert(true)" in source
    assert "setOnboardingBackgroundInert(false)" in source
    assert ".inert = true" in source
    assert 'event.key === "Escape"' in source
    assert 'event.key !== "Tab"' in source
    assert "getOnboardingFocusableElements" in source
    assert "focusOnboardingInitialControl" in source
    assert "onboardingPreviouslyFocusedElement.focus()" in source
    assert 'document.addEventListener("keydown", handleOnboardingDialogKeydown)' in source


def test_lifecycle_update_never_resends_credentials_and_export_requires_confirmation():
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (LIFECYCLE_JS, ONBOARDING_JS)
    )

    lifecycle_function = re.search(
        r"async function saveTokenLifecycle\(tokenId[\s\S]*?\n}\n",
        source,
    )
    assert lifecycle_function is not None
    lifecycle_body = lifecycle_function.group(0)
    assert "changes" in lifecycle_body
    assert "lifecycleUpdateQueues" in source
    assert "keepalive_enabled" in source
    assert "runtime_mode" in source
    assert ".st" not in lifecycle_body
    assert '"st"' not in lifecycle_body
    assert "session_token" not in lifecycle_body

    export_function = re.search(
        r"async function exportTokenCredentials\(tokenId[\s\S]*?\n}\n",
        source,
    )
    assert export_function is not None
    assert "window.confirm" in export_function.group(0)
    assert "URL.revokeObjectURL" in export_function.group(0)


def test_onboarding_resume_matches_resolved_new_accounts_with_deterministic_precedence():
    source = ONBOARDING_JS.read_text(encoding="utf-8")

    assert "getOnboardingTargetMatchRank" in source
    assert "job.target_token_id" in source
    assert "job.resolved_token_id" in source
    assert "targetTokenId === null" in source
    assert "targetRank - rightRank" in source
    assert "resolved_token_id=${encodeURIComponent(targetTokenId)}" in source
    assert "target_token_id=${encodeURIComponent(targetTokenId)}" in source


def test_manage_page_handles_empty_accounts_and_structured_edit_errors():
    html = MANAGE_HTML.read_text(encoding="utf-8")

    assert "if(!allTokens.length)" in html
    assert 'colspan="15"' in html
    assert "window.extractApiError(d" in html


def test_application_serves_lifecycle_script_with_javascript_content_type():
    from fastapi.testclient import TestClient

    from src.main import app

    client = TestClient(app)
    manage_response = client.get("/manage")
    lifecycle_response = client.get("/static/manage-account-lifecycle.js")
    onboarding_response = client.get("/static/manage-account-onboarding.js")

    assert manage_response.status_code == 200
    assert '<script src="/static/manage-account-lifecycle.js"></script>' in manage_response.text
    assert '<script src="/static/manage-account-onboarding.js"></script>' in manage_response.text
    for response in (lifecycle_response, onboarding_response):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
    assert "async function startOnboardingJob()" in onboarding_response.text


def test_management_scripts_stay_within_file_size_limit():
    for path in (LIFECYCLE_JS, ONBOARDING_JS):
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 800


def test_application_rejects_unlisted_cross_origin_preflight():
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient

    from src.main import app

    cors_middleware = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    assert "*" not in cors_middleware.kwargs["allow_origins"]

    client = TestClient(app)
    response = client.options(
        "/api/plugin/update-token",
        headers={
            "Origin": "https://unlisted.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_configured_web_and_extension_origins_keep_bearer_preflight_contract():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.main import add_configured_cors

    allowed_origins = [
        "https://console.example.com",
        "chrome-extension://abcdefghijklmnop",
    ]
    cors_app = FastAPI()
    add_configured_cors(cors_app, allowed_origins)
    client = TestClient(cors_app)

    for origin in allowed_origins:
        response = client.options(
            "/api/plugin/update-token",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert "authorization" in response.headers["access-control-allow-headers"].lower()
