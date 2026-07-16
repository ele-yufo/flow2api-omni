"""Characterization: lock get_media_url (3rd pipeline step: 307 redirect -> signed URL)."""
import asyncio


class _Resp:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {}
        if location is not None:
            self.headers["location"] = location


class _Session:
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, **kw):
        assert kw.get("allow_redirects") is False  # must not follow the 307
        return self._resp


def _fc(monkeypatch, resp):
    from src.services import flow_client as fc_mod
    from src.services.flow_client import FlowClient
    fc = FlowClient(None)
    monkeypatch.setattr(fc_mod, "AsyncSession", lambda *a, **k: _Session(resp))
    return fc


def test_media_url_307_returns_location(monkeypatch):
    fc = _fc(monkeypatch, _Resp(307, "https://signed.example.com/video.mp4?sig=abc"))
    url = asyncio.run(fc.get_media_url("media-1", "ST"))
    assert url == "https://signed.example.com/video.mp4?sig=abc"


def test_media_url_non_redirect_returns_none(monkeypatch):
    fc = _fc(monkeypatch, _Resp(200))
    assert asyncio.run(fc.get_media_url("media-1", "ST")) is None


def test_media_url_307_missing_location_returns_none(monkeypatch):
    fc = _fc(monkeypatch, _Resp(307, None))
    assert asyncio.run(fc.get_media_url("media-1", "ST")) is None


def test_media_url_missing_name_returns_none(monkeypatch):
    from src.services.flow_client import FlowClient
    fc = FlowClient(None)
    assert asyncio.run(fc.get_media_url("", "ST")) is None
