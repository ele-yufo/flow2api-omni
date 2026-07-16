"""Characterization: lock the video-status poll request contract."""
from tests.conftest import assert_golden


def test_video_status_request_golden():
    from src.services.flow.request_builders import build_video_status_request

    out = {
        "list": build_video_status_request([{"operation": {"name": "t1"}}]),
        "dict_operations": build_video_status_request({"operations": [{"operation": {"name": "t2"}}]}),
        "dict_media": build_video_status_request({"media": [{"name": "m1", "projectId": "p1"}]}),
        "dict_both": build_video_status_request({"operations": [{"o": 1}], "media": [{"m": 2}]}),
        "dict_empty": build_video_status_request({}),
    }
    assert out["dict_empty"] == {}
    assert "operations" in out["list"]
    assert_golden("flow_status_request", out)
