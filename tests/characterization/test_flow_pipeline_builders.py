"""Characterization: lock video-concatenation + image-upsample request contracts."""
from tests.conftest import assert_golden


def test_pipeline_builders_golden():
    from src.services.flow.request_builders import (
        build_video_concatenation_request, build_image_upsample_request)

    out = {
        "concat": build_video_concatenation_request(
            original_media_id="orig", extended_media_id="ext",
            original_duration_nanos=8000, extended_start_offset="1s"),
        "upsample": build_image_upsample_request(
            media_id="m1", target_resolution="UPSAMPLE_IMAGE_RESOLUTION_4K",
            recaptcha_token="RC", session_id="S", project_id="p1",
            user_paygate_tier="PAYGATE_TIER_ONE"),
    }
    assert out["concat"]["inputVideos"][1]["startTimeOffset"] == "1s"
    assert out["upsample"]["targetResolution"] == "UPSAMPLE_IMAGE_RESOLUTION_4K"
    assert_golden("flow_pipeline_builders", out)
