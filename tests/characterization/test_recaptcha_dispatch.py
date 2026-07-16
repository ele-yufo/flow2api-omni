"""Characterization: lock _get_recaptcha_token method dispatch (API path).

Locks that captcha_method in {yescaptcha,capmonster,ezcaptcha,capsolver} routes to the
third-party API solver and returns (token, None), after honouring the cooldown wait.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock


def test_api_method_dispatch(monkeypatch):
    from src.services import flow_client as fc_mod
    from src.services.flow_client import FlowClient

    fc = FlowClient(None)
    monkeypatch.setattr(fc_mod, "config", SimpleNamespace(captcha_method="yescaptcha"))
    fc._wait_for_captcha_cooldown = AsyncMock(return_value=None)
    fc._get_api_captcha_token = AsyncMock(return_value="RC_" + "x" * 150)  # >=100 to pass fake-token guard
    fc._set_request_fingerprint = lambda *a, **k: None
    fc._set_request_browser_context = lambda *a, **k: None

    token, browser_id = asyncio.run(fc._get_recaptcha_token("proj-1", action="IMAGE_GENERATION"))
    assert token == "RC_" + "x" * 150
    assert browser_id is None  # API methods -> no browser id
    fc._wait_for_captcha_cooldown.assert_awaited_once()   # cooldown honoured
    fc._get_api_captcha_token.assert_awaited_once_with("yescaptcha", "proj-1", "IMAGE_GENERATION")


def test_unknown_method_returns_none(monkeypatch):
    from src.services import flow_client as fc_mod
    from src.services.flow_client import FlowClient

    fc = FlowClient(None)
    monkeypatch.setattr(fc_mod, "config", SimpleNamespace(captcha_method="disabled"))
    fc._wait_for_captcha_cooldown = AsyncMock(return_value=None)

    token, browser_id = asyncio.run(fc._get_recaptcha_token("proj-1"))
    assert token is None and browser_id is None
