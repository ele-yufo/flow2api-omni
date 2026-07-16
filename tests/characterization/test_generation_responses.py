"""Characterization: lock OpenAI-compatible response/SSE formatters (P4 extract).

Time-dependent fields (id/created) are normalized so the golden is stable.
"""
import json
import re

from tests.conftest import assert_golden


def _norm(s: str) -> str:
    s = re.sub(r'"id": "chatcmpl-\d+"', '"id": "chatcmpl-X"', s)
    s = re.sub(r'"created": \d+', '"created": 0', s)
    return s


def test_response_formatters_golden():
    from src.services.generation.responses import (
        create_completion_response,
        create_error_response,
        create_stream_chunk,
    )

    out = {
        "stream_role": _norm(create_stream_chunk("hi", role="assistant")),
        "stream_reasoning": _norm(create_stream_chunk("thinking...")),
        "stream_finish": _norm(create_stream_chunk("done", finish_reason="stop")),
        "completion_image": _norm(create_completion_response("http://x/i.png", "image")),
        "completion_video": _norm(create_completion_response("http://x/v.mp4", "video")),
        "completion_availability": _norm(
            create_completion_response("Service unavailable", "image", True)
        ),
        "error_500": create_error_response("boom", 500),
        "error_400": create_error_response("bad request", 400),
    }
    # 确保仍是可解析的 JSON / SSE
    assert out["stream_role"].startswith("data: ")
    json.loads(out["completion_image"])
    json.loads(out["error_500"])
    assert_golden("generation_responses", out)


def test_delegation_still_wired():
    """GenerationHandler 的薄委托方法输出与纯函数一致(91 调用点不变)。"""
    from src.services.generation_handler import GenerationHandler
    from src.services.generation import responses as R

    gh = GenerationHandler.__new__(GenerationHandler)  # 纯方法,无需构造依赖
    assert _norm(gh._create_error_response("x", 400)) == _norm(R.create_error_response("x", 400))
    assert _norm(gh._create_stream_chunk("y", finish_reason="stop")) == _norm(
        R.create_stream_chunk("y", finish_reason="stop")
    )
