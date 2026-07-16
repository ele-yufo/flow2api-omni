"""Characterization: lock upsample + extend video request contracts."""
import asyncio
from unittest.mock import AsyncMock

from tests.conftest import assert_golden


class _FixedUUID:
    def __str__(self):
        return "00000000-0000-0000-0000-000000000000"


def _setup(monkeypatch, fc, fc_mod):
    monkeypatch.setattr(fc_mod.random, "randint", lambda a, b: 42)
    monkeypatch.setattr(fc_mod.uuid, "uuid4", lambda: _FixedUUID())
    fc._generate_session_id = lambda: "SESSION_FIXED"
    fc._get_recaptcha_token = AsyncMock(return_value=("RECAPTCHA_FIXED", "browser-1"))
    fc._clear_captcha_rejection = lambda project_id: None
    fc._notify_browser_captcha_request_finished = AsyncMock(return_value=None)


def _capture(monkeypatch, which):
    from src.services import flow_client as fc_mod
    from src.services.flow_client import FlowClient

    fc = FlowClient(None)
    _setup(monkeypatch, fc, fc_mod)
    captured = {}

    async def fake_make_request(**kwargs):
        captured.update(kwargs)
        return {"operations": [{"operation": {"name": f"task-{which}"}}]}

    fc._make_request = AsyncMock(side_effect=fake_make_request)

    async def run():
        if which == "upsample":
            return await fc.upsample_video(
                at="AT", project_id="proj-1", video_media_id="vid-1",
                aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE", resolution="VIDEO_RESOLUTION_4K",
                model_key="veo_3_1_upsampler_4k")
        return await fc.extend_video(
            at="AT", project_id="proj-1", video_media_id="vid-1",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE", workflow_id="wf-1",
            model_key="veo_3_1_extend_landscape", prompt="continue",
            user_paygate_tier="PAYGATE_TIER_ONE")

    result = asyncio.run(run())
    return {"url": captured.get("url"), "json_data": captured.get("json_data"), "result": result}


def test_upsample_extend_request_golden(monkeypatch):
    out = {"upsample": _capture(monkeypatch, "upsample"), "extend": _capture(monkeypatch, "extend")}
    assert out["upsample"]["url"].endswith("/video:batchAsyncGenerateVideoUpsampleVideo")
    assert out["upsample"]["json_data"]["requests"][0]["resolution"] == "VIDEO_RESOLUTION_4K"
    assert out["extend"]["url"].endswith("/video:batchAsyncGenerateVideoExtendVideo")
    assert out["extend"]["json_data"]["requests"][0]["metadata"]["workflowId"] == "wf-1"
    assert out["extend"]["json_data"]["useV2ModelConfig"] is True
    assert_golden("flow_video_pipeline_request", out)
