"""Characterization: _poll_video_result extend + concat path.

给 _poll_video_result 里最大的剩余内联段(视频延长 + 拼接,~193 行)建多阶段
mock 序列网,作为后续抽 _handle_video_extend 的前置安全网。

延长成功路径涉及 4 段 I/O:延长提交 -> 轮询延长 -> 拼接提交 -> 轮询拼接 ->
base64 解码 -> 写盘 -> 落库 -> yield 最终 15s 视频。任一失败都回退原视频(fall
through 到 finalize)。
"""
import asyncio
import base64
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.generation_handler import GenerationHandler


def _fake_config():
    return SimpleNamespace(
        max_poll_attempts=10, poll_interval=0,
        cache_enabled=False, watermark_enabled=False,
        cache_base_url="", server_host="127.0.0.1", server_port=8000,
    )


def _token():
    return SimpleNamespace(
        at="AT", st="ST", id=1,
        user_paygate_tier="PAYGATE_TIER_NOT_PAID", video_concurrency=1,
    )


# 主轮询成功:fifeUrl 末尾带合法 UUID(供 source_media_id 从 URL 解析)
_MAIN_MEDIA_UUID = "12345678-1234-1234-1234-123456789abc"


def _main_success():
    return {"operations": [{
        "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
        "operation": {
            "name": "550e8400-e29b-41d4-a716-446655440000",
            "metadata": {"video": {
                "fifeUrl": f"https://cdn/{_MAIN_MEDIA_UUID}?x=1",
                "mediaGenerationId": "main-mgid",
                "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            }},
        },
    }]}


def _extend_success():
    return {"operations": [{
        "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
        "operation": {
            "name": "extend-op",
            "metadata": {"video": {"mediaGenerationId": "extend-mgid"}},
        },
    }]}


def _make_handler(cache_dir, check_side_effect, concat_status=None,
                  extend_submit=None, concat_submit=None):
    gh = GenerationHandler.__new__(GenerationHandler)
    gh.flow_client = MagicMock()
    gh.flow_client.check_video_status = AsyncMock(side_effect=check_side_effect)
    gh.flow_client.get_media_url = AsyncMock(return_value=None)
    gh.flow_client.extend_video = AsyncMock(
        return_value=extend_submit if extend_submit is not None
        else {"operations": [{"operation": {"name": "extend-op"}}]})
    gh.flow_client.concatenate_videos = AsyncMock(
        return_value=concat_submit if concat_submit is not None else {"name": "concat-op"})
    gh.flow_client.check_concatenation_status = AsyncMock(
        return_value=concat_status or {
            "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
            "encodedVideo": base64.b64encode(b"fake-15s-video").decode(),
        })
    gh.file_cache = MagicMock()
    gh.file_cache.cache_dir = Path(cache_dir)
    gh.file_cache.download_and_cache = AsyncMock(return_value="cached.mp4")
    gh.db = MagicMock()
    gh.db.update_task = AsyncMock()
    return gh


async def _drive_extend(gh):
    generation_result = {"success": False, "error_message": None, "error_emitted": False}
    chunks = []
    with patch("src.services.generation_handler.config", _fake_config()), \
         patch("src.services.generation_handler.debug_logger", MagicMock()), \
         patch("asyncio.sleep", new=AsyncMock()):
        agen = gh._poll_video_result(
            _token(), "proj-1",
            [{"operation": {"name": "task-1"}}],
            stream=False,
            extend_config={"model_key": "veo_extend"},
            generation_workflow_id="wf-1",
            generation_result=generation_result,
        )
        async for c in agen:
            chunks.append(c)
    return chunks, generation_result


def test_extend_and_concat_success_returns_15s_video():
    with tempfile.TemporaryDirectory() as d:
        gh = _make_handler(d, check_side_effect=[_main_success(), _extend_success()])
        chunks, gr = asyncio.run(_drive_extend(gh))

    assert gr["success"] is True
    gh.flow_client.extend_video.assert_awaited_once()
    gh.flow_client.concatenate_videos.assert_awaited_once()
    payload = json.loads(chunks[-1])
    content = payload["choices"][0]["message"]["content"]
    # 最终返回拼接后的本机 15s 视频
    assert "_15s.mp4" in content
    _, kwargs = gh.db.update_task.await_args
    assert kwargs["status"] == "completed"
    assert kwargs["result_urls"][0].endswith("_15s.mp4")
    assert kwargs["result_urls"][0].startswith("http://127.0.0.1:8000/tmp/")


def test_extend_submit_empty_falls_back_to_original():
    # 延长任务创建失败(无 operations/media)-> 回退原视频
    with tempfile.TemporaryDirectory() as d:
        gh = _make_handler(d, check_side_effect=[_main_success()], extend_submit={})
        chunks, gr = asyncio.run(_drive_extend(gh))

    assert gr["success"] is True
    gh.flow_client.concatenate_videos.assert_not_awaited()
    _, kwargs = gh.db.update_task.await_args
    # 回退:结果 URL 为原始 fifeUrl
    assert kwargs["result_urls"] == [f"https://cdn/{_MAIN_MEDIA_UUID}?x=1"]


def test_extend_poll_failed_falls_back_to_original():
    # 延长轮询返回 FAILED -> 回退原视频,不拼接
    extend_failed = {"operations": [{
        "status": "MEDIA_GENERATION_STATUS_FAILED",
        "operation": {"name": "extend-op"},
    }]}
    with tempfile.TemporaryDirectory() as d:
        gh = _make_handler(d, check_side_effect=[_main_success(), extend_failed])
        chunks, gr = asyncio.run(_drive_extend(gh))

    assert gr["success"] is True
    gh.flow_client.concatenate_videos.assert_not_awaited()
    _, kwargs = gh.db.update_task.await_args
    assert kwargs["result_urls"] == [f"https://cdn/{_MAIN_MEDIA_UUID}?x=1"]


def test_concat_status_failed_falls_back_to_original():
    # 拼接轮询返回 FAILED -> 回退原视频
    concat_failed = {"status": "MEDIA_GENERATION_STATUS_FAILED"}
    with tempfile.TemporaryDirectory() as d:
        gh = _make_handler(d, check_side_effect=[_main_success(), _extend_success()],
                           concat_status=concat_failed)
        chunks, gr = asyncio.run(_drive_extend(gh))

    assert gr["success"] is True
    gh.flow_client.concatenate_videos.assert_awaited_once()
    _, kwargs = gh.db.update_task.await_args
    # 拼接失败 -> 落库原始 fifeUrl
    assert kwargs["result_urls"] == [f"https://cdn/{_MAIN_MEDIA_UUID}?x=1"]
