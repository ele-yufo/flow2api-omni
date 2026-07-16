"""Characterization: lock _make_request core transport path (mocked curl_cffi AsyncSession).

Safety net for the transport orchestration core before any future decomposition.
Covers: GET/POST happy path (parsed json), HTTP>=400 error extraction, set-cookie capture.
"""
import asyncio
import pytest


class _FakeHeaders(dict):
    def get_list(self, key):
        v = self.get(key)
        return v if isinstance(v, list) else ([v] if v else [])


class _FakeResp:
    def __init__(self, status=200, payload=None, text="", set_cookie=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text
        h = _FakeHeaders()
        if set_cookie is not None:
            h["set-cookie"] = set_cookie
        self.headers = h

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._resp

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._resp


def _client_with(monkeypatch, resp):
    from src.services import flow_client as fc_mod
    from src.services.flow_client import FlowClient
    fc = FlowClient(None)
    fc._should_submit_via_captcha_browser = lambda *a, **k: False
    session = _FakeSession(resp)
    monkeypatch.setattr(fc_mod, "AsyncSession", lambda *a, **k: session)
    return fc, session


def test_make_request_get_success(monkeypatch):
    fc, session = _client_with(monkeypatch, _FakeResp(200, {"credits": 900}))
    result = asyncio.run(fc._make_request("GET", "https://x/credits", use_at=True, at_token="AT"))
    assert result == {"credits": 900}
    assert session.calls[0][0] == "GET"


def test_make_request_post_success(monkeypatch):
    fc, session = _client_with(monkeypatch, _FakeResp(200, {"ok": True}))
    result = asyncio.run(fc._make_request("POST", "https://x/api", json_data={"a": 1},
                                          use_at=True, at_token="AT"))
    assert result == {"ok": True}
    assert session.calls[0][0] == "POST"
    assert session.calls[0][2]["headers"]["authorization"] == "Bearer AT"


def test_make_request_http_error_extracts_reason(monkeypatch):
    resp = _FakeResp(403, {"error": {"message": "denied",
                                     "details": [{"reason": "PERMISSION_DENIED"}]}}, text="err")
    fc, _ = _client_with(monkeypatch, resp)
    with pytest.raises(Exception, match="PERMISSION_DENIED: denied"):
        asyncio.run(fc._make_request("GET", "https://x", use_at=True, at_token="AT"))


def test_make_request_captures_set_cookie(monkeypatch):
    fc, _ = _client_with(monkeypatch, _FakeResp(200, {"ok": 1}, set_cookie=["k=v; Path=/"]))
    captured = []
    asyncio.run(fc._make_request("GET", "https://x", use_st=True, st_token="ST",
                                 capture_set_cookie=captured))
    assert captured == ["k=v; Path=/"]
