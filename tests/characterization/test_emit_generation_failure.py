"""Characterization: GenerationHandler._emit_generation_failure.

锁住从 image/video handler 的 4 处相同失败块抽出的助手:标记生成失败 +
yield 502 错误响应,无流式前缀,落库与返回同文案。
"""
import asyncio
import json

from src.services.generation_handler import GenerationHandler


async def _collect(agen):
    return [c async for c in agen]


def test_emit_generation_failure_marks_and_yields_502():
    gh = GenerationHandler.__new__(GenerationHandler)
    gr = {"success": False, "error_message": None, "error_emitted": False}
    chunks = asyncio.run(_collect(gh._emit_generation_failure(gr, "生成结果为空")))

    assert len(chunks) == 1
    err = json.loads(chunks[0])["error"]
    assert err["status_code"] == 502
    assert err["message"] == "生成结果为空"
    # generation_result 被标记失败
    assert gr["success"] is False
    assert gr["error_message"] == "生成结果为空"
    assert gr["error_emitted"] is True


def test_emit_generation_failure_custom_status():
    gh = GenerationHandler.__new__(GenerationHandler)
    gr = {"success": False, "error_message": None, "error_emitted": False}
    chunks = asyncio.run(_collect(gh._emit_generation_failure(gr, "boom", status_code=500)))
    assert json.loads(chunks[0])["error"]["status_code"] == 500
