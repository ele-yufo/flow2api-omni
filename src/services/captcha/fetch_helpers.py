"""Pure helpers for browser-fetch reCAPTCHA (page URL + allowed fetch headers).

Extracted from browser_captcha_personal (static methods). Locked by
tests/characterization/test_captcha_fetch_helpers.py.
"""
from typing import Any, Dict, Optional


def flow_recaptcha_page_url(project_id: Optional[str]) -> str:
    # 必须用轻量 JSON 端点而不是 SPA 主页(auth/providers <1s ready;
    # reCAPTCHA Enterprise 评分只看 origin+siteKey+action+指纹+IP,不看正文)。
    _ = project_id
    return "https://labs.google/fx/api/auth/providers"


def browser_fetch_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """保留浏览器 fetch 允许设置的业务头，UA/client hints 由浏览器真实发送。"""
    forbidden = {
        "accept-charset", "accept-encoding", "access-control-request-headers",
        "access-control-request-method", "connection", "content-length",
        "cookie", "cookie2", "date", "dnt", "expect", "host", "keep-alive",
        "origin", "referer", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
        "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "te", "trailer",
        "transfer-encoding", "upgrade", "user-agent", "via",
    }
    allowed = {"accept", "authorization", "content-type"}
    result: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        key_text = str(key)
        key_lower = key_text.lower()
        if key_lower not in allowed:
            continue
        if key_lower in forbidden or key_lower.startswith("proxy-"):
            continue
        if value is None:
            continue
        result[key_text] = str(value)
    result.setdefault("Content-Type", "application/json")
    return result
