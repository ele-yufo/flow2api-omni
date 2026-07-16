"""urllib JSON transport — curl_cffi network fallback (extracted from FlowClient).

Parameter-driven (no instance state). Builds a urllib request (optionally via proxy),
executes it, and returns parsed JSON — raising on HTTP >= 400 or invalid JSON.
Locked by tests/characterization/test_flow_transport.py (mocked opener).
"""
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


def sync_json_request_via_urllib(
    method: str,
    url: str,
    headers: Optional[Dict[str, Any]],
    json_data: Optional[Dict[str, Any]],
    proxy_url: Optional[str],
    timeout: int,
) -> Dict[str, Any]:
    """使用 urllib 执行 JSON 请求，作为 curl_cffi 的网络回退。"""
    request_headers = dict(headers or {})
    request_headers.setdefault("Accept", "application/json")

    data = None
    if method.upper() != "GET" and json_data is not None:
        data = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
    if proxy_url:
        handlers.append(
            urllib.request.ProxyHandler(
                {"http": proxy_url, "https": proxy_url}
            )
        )

    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        url=url,
        data=data,
        headers=request_headers,
        method=method.upper(),
    )

    try:
        with opener.open(
            request,
            timeout=timeout,
        ) as response:
            payload = response.read()
            status_code = int(response.getcode() or 0)
    except urllib.error.HTTPError as exc:
        payload = exc.read() if hasattr(exc, "read") else b""
        status_code = int(getattr(exc, "code", 500) or 500)
        body_text = payload.decode("utf-8", errors="replace")
        raise Exception(f"HTTP Error {status_code}: {body_text[:200]}") from exc
    except Exception as exc:
        raise Exception(str(exc)) from exc

    body_text = payload.decode("utf-8", errors="replace")
    if status_code >= 400:
        raise Exception(f"HTTP Error {status_code}: {body_text[:200]}")

    try:
        return json.loads(body_text) if body_text else {}
    except Exception as exc:
        raise Exception(f"Invalid JSON response: {body_text[:200]}") from exc
