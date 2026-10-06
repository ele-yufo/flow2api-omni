"""Static management UI contracts for account lifecycle controls."""

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).parents[1]
MANAGE_HTML = PROJECT_ROOT / "static" / "manage.html"
LIFECYCLE_JS = PROJECT_ROOT / "static" / "manage-account-lifecycle.js"


def test_manage_page_exposes_lifecycle_columns_without_onboarding_modal():
    html = MANAGE_HTML.read_text(encoding="utf-8")

    assert 'id="accountActionFeedback"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert 'tabindex="0"' in html
    for heading in ("业务池", "会员", "保活", "Profile", "最近保活"):
        assert heading in html
    assert '<script src="/static/manage-account-lifecycle.js"></script>' in html
    # The XRDP onboarding modal and its script were removed together with the
    # deprecated onboarding service; nothing may reference them anymore.
    assert "manage-account-onboarding.js" not in html
    assert "onboardingModal" not in html
    assert "openOnboardingModal" not in html


def test_metadata_edit_no_longer_requires_or_prefills_session_token():
    html = MANAGE_HTML.read_text(encoding="utf-8")

    st_field = re.search(r'<textarea id="editTokenST"(?P<attrs>[^>]*)>', html)
    assert st_field is not None
    assert "required" not in st_field.group("attrs")
    assert "留空" in st_field.group("attrs") or "可选" in st_field.group("attrs")
    assert "document.getElementById('editTokenST').value=t.st" not in html
    assert "editTokenAT" not in html
    assert "addTokenAT" not in html


def test_lifecycle_script_uses_safe_endpoints():
    source = LIFECYCLE_JS.read_text(encoding="utf-8")

    assert "/api/tokens/${id}/lifecycle" in source
    assert "/api/tokens/${tokenId}/export" in source
    assert "lifecycleUpdateQueues" in source
    assert "extractApiError" in source
    assert "escapeLogHtml" in source
    assert "setInterval" not in source
    # Onboarding endpoints are gone from the UI along with the 410'd service.
    assert "/api/onboarding/" not in source
    assert "validate-profile" not in source


def test_lifecycle_feedback_uses_persistent_accessible_region():
    lifecycle_source = LIFECYCLE_JS.read_text(encoding="utf-8")

    assert "renderAccountActionFeedback" in lifecycle_source
    assert 'feedback.setAttribute("role", type === "error" ? "alert" : "status")' in lifecycle_source
    assert 'feedback.setAttribute("aria-live", type === "error" ? "assertive" : "polite")' in lifecycle_source
    assert "feedback.focus()" in lifecycle_source


def test_lifecycle_update_never_resends_credentials_and_export_requires_confirmation():
    source = LIFECYCLE_JS.read_text(encoding="utf-8")

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
    assert "manage-account-onboarding.js" not in manage_response.text
    assert lifecycle_response.status_code == 200
    assert lifecycle_response.headers["content-type"].startswith("text/javascript")
    # The deleted onboarding script must no longer be served.
    assert onboarding_response.status_code == 404


def test_management_scripts_stay_within_file_size_limit():
    assert len(LIFECYCLE_JS.read_text(encoding="utf-8").splitlines()) <= 800


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
