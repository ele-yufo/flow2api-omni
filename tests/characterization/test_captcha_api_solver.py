"""Characterization: lock third-party captcha API solve flow (mocked AsyncSession + config)."""
import asyncio
from types import SimpleNamespace


class _Resp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p


class _Session:
    def __init__(self, responses): self._responses = list(responses); self.posts = []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        return _Resp(self._responses.pop(0))


def _fake_config(**kw):
    base = dict(yescaptcha_api_key="", yescaptcha_base_url="https://yc",
                capmonster_api_key="", capmonster_base_url="https://cm",
                ezcaptcha_api_key="", ezcaptcha_base_url="https://ez",
                capsolver_api_key="", capsolver_base_url="https://cs")
    base.update(kw)
    return SimpleNamespace(**base)


def test_api_solver_success(monkeypatch):
    from src.services.captcha import api_solver as S
    monkeypatch.setattr(S, "config", _fake_config(yescaptcha_api_key="KEY"))
    sess = _Session([{"taskId": "T1"},
                     {"status": "ready", "solution": {"gRecaptchaResponse": "TOKEN123"}}])
    monkeypatch.setattr(S, "AsyncSession", lambda *a, **k: sess)
    async def _nosleep(*a, **k): return None
    monkeypatch.setattr(S.asyncio, "sleep", _nosleep)

    token = asyncio.run(S.get_api_captcha_token("yescaptcha", "proj-1", "IMAGE_GENERATION"))
    assert token == "TOKEN123"
    create_body = sess.posts[0][1]
    assert create_body["task"]["pageAction"] == "IMAGE_GENERATION"
    assert create_body["task"]["type"] == "RecaptchaV3TaskProxylessM1"


def test_api_solver_no_key_returns_none(monkeypatch):
    from src.services.captcha import api_solver as S
    monkeypatch.setattr(S, "config", _fake_config())  # all keys empty
    assert asyncio.run(S.get_api_captcha_token("capmonster", "p", "IMAGE_GENERATION")) is None


def test_api_solver_unknown_method(monkeypatch):
    from src.services.captcha import api_solver as S
    monkeypatch.setattr(S, "config", _fake_config())
    assert asyncio.run(S.get_api_captcha_token("nope", "p")) is None
