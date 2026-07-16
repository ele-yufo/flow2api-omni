"""Characterization: lock urllib JSON transport (request build + response/error handling)."""
import json
from contextlib import contextmanager
from unittest.mock import MagicMock
import pytest


class _FakeResp:
    def __init__(self, body: bytes, code: int):
        self._body, self._code = body, code
    def read(self): return self._body
    def getcode(self): return self._code
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_opener(body: bytes, code: int):
    op = MagicMock()
    op.open.return_value = _FakeResp(body, code)
    return op


def test_urllib_transport_success(monkeypatch):
    from src.services.flow import transport as T

    captured = {}
    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return _fake_opener(b'{"ok": true, "n": 5}', 200)
    monkeypatch.setattr(T.urllib.request, "build_opener", fake_build_opener)

    # capture the Request that gets built
    real_request = T.urllib.request.Request
    def spy_request(**kw):
        captured["req"] = kw
        return real_request(**kw)
    monkeypatch.setattr(T.urllib.request, "Request", spy_request)

    result = T.sync_json_request_via_urllib(
        "POST", "https://x/api", {"X-H": "1"}, {"a": 1}, None, 30)
    assert result == {"ok": True, "n": 5}
    # POST with json -> body encoded + Content-Type set + Accept defaulted
    assert captured["req"]["method"] == "POST"
    assert json.loads(captured["req"]["data"]) == {"a": 1}
    assert captured["req"]["headers"]["Content-Type"] == "application/json"
    assert captured["req"]["headers"]["Accept"] == "application/json"


def test_urllib_transport_get_no_body(monkeypatch):
    from src.services.flow import transport as T
    monkeypatch.setattr(T.urllib.request, "build_opener",
                        lambda *h: _fake_opener(b'{}', 200))
    captured = {}
    real = T.urllib.request.Request
    monkeypatch.setattr(T.urllib.request, "Request",
                        lambda **kw: captured.update(kw) or real(**kw))
    T.sync_json_request_via_urllib("GET", "https://x", None, {"ignored": 1}, None, 10)
    assert captured["data"] is None  # GET never sends body


def test_urllib_transport_http_error(monkeypatch):
    from src.services.flow import transport as T
    monkeypatch.setattr(T.urllib.request, "build_opener",
                        lambda *h: _fake_opener(b'{"error":"bad"}', 500))
    with pytest.raises(Exception, match="HTTP Error 500"):
        T.sync_json_request_via_urllib("POST", "https://x", None, {}, None, 10)


def test_urllib_transport_invalid_json(monkeypatch):
    from src.services.flow import transport as T
    monkeypatch.setattr(T.urllib.request, "build_opener",
                        lambda *h: _fake_opener(b'not json', 200))
    with pytest.raises(Exception, match="Invalid JSON"):
        T.sync_json_request_via_urllib("POST", "https://x", None, {}, None, 10)


def test_flowclient_delegates(monkeypatch):
    from src.services.flow import transport as T
    from src.services.flow_client import FlowClient
    monkeypatch.setattr(T.urllib.request, "build_opener",
                        lambda *h: _fake_opener(b'{"via": "fc"}', 200))
    fc = FlowClient(None)
    assert fc._sync_json_request_via_urllib("POST", "https://x", None, {}, None, 5) == {"via": "fc"}


def test_build_remote_browser_http_timeout():
    from src.services.flow import transport as T
    t = T.build_remote_browser_http_timeout(2.0)
    # returns httpx.Timeout (or float if httpx absent); read clamped to >=3
    if T.httpx is not None:
        assert t.read == 3.0
    else:
        assert t == 3.0


def test_stdlib_json_http_request(monkeypatch):
    from src.services.flow import transport as T

    class _R:
        status = 200
        headers = type("H", (), {"get_content_charset": lambda self: "utf-8"})()
        def read(self): return b'{"ok": 1}'
        def getcode(self): return 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    op = type("O", (), {"open": lambda self, req, timeout: _R()})()
    monkeypatch.setattr(T.urllib.request, "build_opener", lambda *h: op)
    import asyncio
    status, parsed, text = asyncio.run(
        T.stdlib_json_http_request("POST", "https://x", {}, {"a": 1}, 5))
    assert status == 200 and parsed == {"ok": 1}
