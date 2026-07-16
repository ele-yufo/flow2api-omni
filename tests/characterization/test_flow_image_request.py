"""Characterization: lock the image-generation request contract (batchGenerateImages)."""
import asyncio
from unittest.mock import AsyncMock

from tests.conftest import assert_golden


class _FixedUUID:
    def __str__(self):
        return "00000000-0000-0000-0000-000000000000"


def _capture_image_request(monkeypatch, *, with_inputs: bool):
    from src.services import flow_client as fc_mod
    from src.services.flow_client import FlowClient

    fc = FlowClient(None)
    monkeypatch.setattr(fc_mod.random, "randint", lambda a, b: 77)
    monkeypatch.setattr(fc_mod.uuid, "uuid4", lambda: _FixedUUID())
    fc._generate_session_id = lambda: "SESSION_FIXED"
    fc._get_recaptcha_token = AsyncMock(return_value=("RECAPTCHA_FIXED", "browser-1"))
    fc._clear_captcha_rejection = lambda project_id: None
    fc._notify_browser_captcha_request_finished = AsyncMock(return_value=None)

    captured = {}

    async def fake_img_request(**kwargs):
        captured.update(kwargs)
        return {"media": [{"name": "img-1"}]}

    fc._make_image_generation_request = AsyncMock(side_effect=fake_img_request)

    image_inputs = [{"mediaId": "ref-1"}] if with_inputs else None

    async def run():
        return await fc.generate_image(
            at="AT_FIXED",
            project_id="proj-1",
            prompt="a red apple",
            model_name="GEM_PIX_2",
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            image_inputs=image_inputs,
        )

    result, session_id, _perf = asyncio.run(run())
    return {"json_data": captured.get("json_data"), "url": captured.get("url"),
            "session_id": session_id, "result": result}


def test_image_request_contract_golden(monkeypatch):
    out = {
        "t2i": _capture_image_request(monkeypatch, with_inputs=False),
        "i2i": _capture_image_request(monkeypatch, with_inputs=True),
    }
    jd = out["t2i"]["json_data"]
    assert jd["useNewMedia"] is True
    assert jd["requests"][0]["imageModelName"] == "GEM_PIX_2"
    assert jd["requests"][0]["imageInputs"] == []
    assert out["i2i"]["json_data"]["requests"][0]["imageInputs"] == [{"mediaId": "ref-1"}]
    # clientContext present both top-level and inside the request
    assert jd["clientContext"]["recaptchaContext"]["token"] == "RECAPTCHA_FIXED"
    assert jd["requests"][0]["clientContext"]["sessionId"] == "SESSION_FIXED"
    assert_golden("flow_image_request", out)
