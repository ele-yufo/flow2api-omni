"""urllib JSON transport — curl_cffi network fallback (extracted from FlowClient).

Parameter-driven (no instance state). Builds a urllib request (optionally via proxy),
executes it, and returns parsed JSON — raising on HTTP >= 400 or invalid JSON.
Locked by tests/characterization/test_flow_transport.py (mocked opener).
"""
import asyncio
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .response_parsers import parse_json_response_text

try:
    import httpx
except ImportError:
    httpx = None


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


def build_remote_browser_http_timeout(read_timeout: float) -> Any:
    read_value = max(3.0, float(read_timeout))
    write_value = min(10.0, max(3.0, read_value))
    if httpx is None:
        return read_value
    return httpx.Timeout(
        connect=2.5,
        read=read_value,
        write=write_value,
        pool=2.5,
    )


async def stdlib_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_data: Optional[bytes] = None

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_data = json.dumps(payload).encode("utf-8")

    def do_request() -> tuple:
        request = urllib.request.Request(
            url=url,
            data=request_data,
            headers=req_headers,
            method=request_method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return status_code, body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            charset = exc.headers.get_content_charset() if exc.headers else None
            return int(getattr(exc, "code", 0) or 0), body.decode(charset or "utf-8", errors="replace")

    try:
        status_code, text = await asyncio.to_thread(do_request)
    except Exception as e:
        raise RuntimeError(f"remote_browser 请求失败: {e}") from e

    return status_code, parse_json_response_text(text), text


async def sync_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple:
    req_headers = dict(headers or {})
    req_headers.setdefault("Accept", "application/json")
    request_method = (method or "GET").upper()
    request_kwargs: Dict[str, Any] = {
        "headers": req_headers,
        "timeout": build_remote_browser_http_timeout(timeout),
    }

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_kwargs["json"] = payload

    if httpx is None:
        return await stdlib_json_http_request(
            method=method, url=url, headers=req_headers, payload=payload, timeout=timeout,
        )

    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as session:
            response = await session.request(method=request_method, url=url, **request_kwargs)
    except Exception as e:
        raise RuntimeError(f"remote_browser 请求失败: {e}") from e

    status_code = int(getattr(response, "status_code", 0) or 0)
    text = response.text or ""
    return status_code, parse_json_response_text(text), text
