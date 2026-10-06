"""Characterization: lock image-upsample request contracts."""
from tests.conftest import assert_golden


def test_pipeline_builders_golden():
    from src.services.flow.request_builders import build_image_upsample_request

    out = {
        "upsample": build_image_upsample_request(
            media_id="m1", target_resolution="UPSAMPLE_IMAGE_RESOLUTION_4K",
            recaptcha_token="RC", session_id="S", project_id="p1",
            user_paygate_tier="PAYGATE_TIER_ONE"),
    }
    assert out["upsample"]["targetResolution"] == "UPSAMPLE_IMAGE_RESOLUTION_4K"
    assert_golden("flow_pipeline_builders", out)
