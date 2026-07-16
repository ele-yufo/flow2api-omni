"""Pure parsers for Flow upstream responses / headers.

Extracted from FlowClient. All pure — golden-locked. Includes the critical rotated-ST
Set-Cookie parser (labs.google returns a rolling session-token on each /auth/session).
"""
import json
from typing import Any, Optional


def parse_json_response_text(text: str) -> Optional[Any]:
    """Safe JSON parse — returns None on empty/invalid input."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def extract_project_id_from_payload(value: Any) -> Optional[str]:
    """Recursively find the first non-empty projectId in a nested dict/list payload."""
    if isinstance(value, dict):
        project_id = value.get("projectId")
        if isinstance(project_id, str) and project_id.strip():
            return project_id.strip()
        for item in value.values():
            found = extract_project_id_from_payload(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = extract_project_id_from_payload(item)
            if found:
                return found
    return None


def extract_rotated_st_from_set_cookie(set_cookie_headers) -> Optional[str]:
    """从响应的 Set-Cookie 头里解析轮换后的 __Secure-next-auth.session-token。

    labs.google /auth/session 每次会回发一个滚动续期 ~30 天的新 ST。
    长度护栏 >= MIN_ST_LEN，防止把异常短值当成有效 ST。
    """
    from ...core.cookie_extractor import SESSION_TOKEN_KEY, MIN_ST_LEN
    for raw in set_cookie_headers or []:
        if isinstance(raw, str) and raw.startswith(SESSION_TOKEN_KEY + "="):
            value = raw.split("=", 1)[1].split(";", 1)[0].strip()
            if len(value) >= MIN_ST_LEN:
                return value
    return None
