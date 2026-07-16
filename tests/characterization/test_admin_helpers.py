"""Characterization: lock admin pure helpers (validate/mask/truncate/error-summary/UA)."""
from tests.conftest import assert_golden


def test_admin_helpers_golden():
    from src.api import admin as A

    out = {
        "proxy_ok": A._validate_browser_proxy_url("http://u:p@1.2.3.4:8080"),
        "proxy_bad": A._validate_browser_proxy_url("garbage"),
        "proxy_empty": A._validate_browser_proxy_url(""),
        "mask_short": A._mask_token("short"),
        "mask_long": A._mask_token("A" * 40),
        "truncate": A._truncate_text("x" * 300),
        "err_dict": A._extract_error_summary({"error": {"message": "boom"}}),
        "err_nested": A._extract_error_summary({"response": {"detail": "deep"}}),
        "err_str_json": A._extract_error_summary('{"message": "j"}'),
        "hints_chrome_win": A._guess_client_hints_from_user_agent(
            "Mozilla/5.0 (Windows NT 10.0) Chrome/124.0 Safari/537.36"),
        "hints_mobile": A._guess_client_hints_from_user_agent(
            "Mozilla/5.0 (iPhone) Mobile Chrome/120.0"),
        "impersonate_124": A._guess_impersonate_from_user_agent("Chrome/130.0"),
        "impersonate_none": A._guess_impersonate_from_user_agent("curl/8"),
        "proxy_map": A._build_proxy_map("http://x:1"),
        "normalize_url": A._normalize_http_base_url("http://host:9/"),
    }
    assert out["proxy_ok"] == (True, None)
    assert out["proxy_bad"][0] is False
    assert out["mask_long"].startswith("AAAAAAAAAAAAAAAAAA...")
    assert out["err_dict"] == "boom"
    assert out["impersonate_124"] == "chrome124"
    assert out["hints_chrome_win"]["sec-ch-ua-platform"] == '"Windows"'
    assert_golden("admin_helpers", out)
