"""Characterization: lock the text-to-video request contract sent to Google.

Mocks the transport (_make_request) + reCAPTCHA + non-deterministic bits (random/uuid/
session) so the exact request body FlowClient builds is golden-locked. This protects the
upstream API contract before any deep transport/actions split of flow_client.

Uses the monkeypatch fixture so module-level random/uuid patches are auto-restored
(never leak into other tests).
"""
import asyncio
from unittest.mock import AsyncMock

from tests.conftest import assert_golden


class _FixedUUID:
    hex = "0" * 32

    def __str__(self):
        return "00000000-0000-0000-0000-000000000000"


def _capture_video_text_request(monkeypatch, use_v2: bool):
    from src.services import flow_client as fc_mod
    from src.services.flow_client import FlowClient

    fc = FlowClient(None)

    # freeze non-deterministic pieces (auto-restored by monkeypatch)
    monkeypatch.setattr(fc_mod.random, "randint", lambda a, b: 42)
    monkeypatch.setattr(fc_mod.uuid, "uuid4", lambda: _FixedUUID())
    fc._generate_session_id = lambda: "SESSION_FIXED"
    fc._get_recaptcha_token = AsyncMock(return_value=("RECAPTCHA_FIXED", "browser-1"))
    fc._clear_captcha_rejection = lambda project_id: None

    captured = {}

    async def fake_make_request(**kwargs):
        captured.update(kwargs)
        return {"operations": [{"operation": {"name": "task-1"}}], "remainingCredits": 900}

    fc._make_request = AsyncMock(side_effect=fake_make_request)

    async def run():
        return await fc.generate_video_text(
            at="AT_FIXED",
            project_id="proj-1",
            prompt="a cat chasing a butterfly",
            model_key="veo_3_1_t2v_fast_landscape",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=use_v2,
            user_paygate_tier="PAYGATE_TIER_ONE",
        )

    result = asyncio.run(run())
    return {"request": {"url": captured.get("url"), "json_data": captured.get("json_data"),
                        "use_at": captured.get("use_at")}, "result": result}


def test_video_text_request_contract_golden(monkeypatch):
    out = {
        "v1": _capture_video_text_request(monkeypatch, use_v2=False),
        "v2": _capture_video_text_request(monkeypatch, use_v2=True),
    }
    # sanity: the request must carry the reCAPTCHA token + model key to Google
    jd = out["v1"]["request"]["json_data"]
    assert jd["requests"][0]["videoModelKey"] == "veo_3_1_t2v_fast_landscape"
    assert jd["clientContext"]["recaptchaContext"]["token"] == "RECAPTCHA_FIXED"
    assert out["v2"]["request"]["json_data"]["useV2ModelConfig"] is True
    assert_golden("flow_video_text_request", out)


def _capture_video_r2v_request(monkeypatch):
    from src.services import flow_client as fc_mod
    from src.services.flow_client import FlowClient

    fc = FlowClient(None)
    monkeypatch.setattr(fc_mod.random, "randint", lambda a, b: 42)
    monkeypatch.setattr(fc_mod.uuid, "uuid4", lambda: _FixedUUID())
    fc._generate_session_id = lambda: "SESSION_FIXED"
    fc._get_recaptcha_token = AsyncMock(return_value=("RECAPTCHA_FIXED", "browser-1"))
    fc._clear_captcha_rejection = lambda project_id: None
    fc._notify_browser_captcha_request_finished = AsyncMock(return_value=None)

    captured = {}

    async def fake_make_request(**kwargs):
        captured.update(kwargs)
        return {"operations": [{"operation": {"name": "task-r2v"}}], "remainingCredits": 800}

    fc._make_request = AsyncMock(side_effect=fake_make_request)

    async def run():
        return await fc.generate_video_reference_images(
            at="AT_FIXED",
            project_id="proj-1",
            prompt="blend these references",
            model_key="veo_3_1_r2v_fast",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            reference_images=[{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "m1"}],
            user_paygate_tier="PAYGATE_TIER_ONE",
        )

    result = asyncio.run(run())
    return {"request": {"url": captured.get("url"), "json_data": captured.get("json_data")},
            "result": result}


def test_video_r2v_request_contract_golden(monkeypatch):
    out = _capture_video_r2v_request(monkeypatch)
    jd = out["request"]["json_data"]
    assert out["request"]["url"].endswith("/video:batchAsyncGenerateVideoReferenceImages")
    assert jd["requests"][0]["referenceImages"][0]["mediaId"] == "m1"
    assert jd["useV2ModelConfig"] is True
    assert_golden("flow_video_r2v_request", out)
