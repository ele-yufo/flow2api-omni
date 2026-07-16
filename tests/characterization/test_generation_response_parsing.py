"""Characterization: lock upstream response normalization (operations/media/workflow shapes)."""
from tests.conftest import assert_golden


def test_normalize_video_submit_response_golden():
    from src.services.generation.response_parsing import normalize_video_submit_response

    cases = {
        "operations": {"operations": [{"operation": {"name": "task-1"}, "sceneId": "s1"}]},
        "media_new": {"media": [{"name": "m1", "sceneId": "s2", "workflowId": "wf1"}]},
        "media_dict": {"media": {"name": "m2", "projectId": "p-override"}},
        "workflow": {"workflow": {"name": "wf-top"}, "media": [{"name": "m3"}]},
        "empty": {},
        "junk_operation": {"operations": [{"operation": {}}, "notdict"]},
    }
    out = {k: normalize_video_submit_response(v, "proj-default") for k, v in cases.items()}
    assert out["operations"]["task_id"] == "task-1"
    assert out["media_dict"]["media"][0]["projectId"] == "p-override"
    assert out["junk_operation"]["operations"] == []  # filtered
    assert_golden("gen_normalize_submit", out)


def test_coerce_media_status_to_operations_golden():
    from src.services.generation.response_parsing import coerce_media_status_to_operations

    refs = {"media": [{"sceneId": "s-fallback"}], "scene_id": "s-fallback"}
    cases = {
        "successful_by_fifeurl": {
            "media": [{"name": "m1", "video": {"generatedVideo": {"fifeUrl": "http://x"}}}]
        },
        "explicit_status": {
            "media": [{"name": "m2", "mediaMetadata": {"mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_ACTIVE"}}}]
        },
        "empty": {},
        "no_name": {"media": [{"video": {}}]},
    }
    out = {k: coerce_media_status_to_operations(v, refs) for k, v in cases.items()}
    assert out["successful_by_fifeurl"][0]["status"] == "MEDIA_GENERATION_STATUS_SUCCESSFUL"
    assert out["empty"] == []
    assert out["no_name"] == []
    assert_golden("gen_coerce_status", out)
