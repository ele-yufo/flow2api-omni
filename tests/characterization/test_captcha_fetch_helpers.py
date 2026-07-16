"""Characterization: lock browser-fetch header filtering + recaptcha page URL."""
from tests.conftest import assert_golden


def test_fetch_helpers_golden():
    from src.services.captcha.fetch_helpers import browser_fetch_headers, flow_recaptcha_page_url

    out = {
        "url": flow_recaptcha_page_url("proj-123"),
        "headers_filtered": browser_fetch_headers({
            "Accept": "application/json",
            "Authorization": "Bearer x",
            "Content-Type": "application/json",
            "Cookie": "secret",         # forbidden -> dropped
            "User-Agent": "spoof",      # forbidden -> dropped
            "Proxy-Auth": "p",          # proxy- -> dropped
            "X-Custom": "v",            # not allowed -> dropped
            "sec-ch-ua": "y",           # forbidden -> dropped
        }),
        "headers_empty": browser_fetch_headers(None),
    }
    assert out["url"] == "https://labs.google/fx/api/auth/providers"
    assert out["headers_filtered"] == {"Accept": "application/json",
                                       "Authorization": "Bearer x",
                                       "Content-Type": "application/json"}
    assert out["headers_empty"] == {"Content-Type": "application/json"}
    assert_golden("captcha_fetch_helpers", out)
