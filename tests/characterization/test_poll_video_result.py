"""Characterization: GenerationHandler._poll_video_result core polling behavior.

给最大的编排巨核(504 行异步生成器)建端到端安全网——mock 在 flow_client/db/
file_cache 的 I/O 边界,保留真实的委托方法 + 纯逻辑,锁住 4 条核心路径的
yield 序列与副作用。这是后续把该方法安全拆成小方法的前置安全网。

不覆盖 upsample/extend/concat 分支(那些是独立内联循环,后续单独特征化)。
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.generation_handler import GenerationHandler


def _fake_config(max_poll_attempts=10):
    return SimpleNamespace(
        max_poll_attempts=max_poll_attempts,
        poll_interval=0,
        cache_enabled=False,
        watermark_enabled=False,
        cache_base_url="",
        server_host="127.0.0.1",
        server_port=8000,
    )


def _make_handler(check_result=None, check_side_effect=None):
    gh = GenerationHandler.__new__(GenerationHandler)
    gh.flow_client = MagicMock()
    if check_side_effect is not None:
        gh.flow_client.check_video_status = AsyncMock(side_effect=check_side_effect)
    else:
        gh.flow_client.check_video_status = AsyncMock(return_value=check_result)
    gh.flow_client.get_media_url = AsyncMock(return_value=None)
    gh.file_cache = MagicMock()
    gh.file_cache.download_and_cache = AsyncMock(return_value="cached.mp4")
    gh.db = MagicMock()
    gh.db.update_task = AsyncMock()
    return gh


def _token():
    return SimpleNamespace(
        at="AT", st="ST", id=1,
        user_paygate_tier="PAYGATE_TIER_NOT_PAID", video_concurrency=1,
    )


async def _drive(gh, operations, **kw):
    """跑 _poll_video_result,收集 yield 出的 chunk。sleep 置空,config 换假。"""
    generation_result = {"success": False, "error_message": None, "error_emitted": False}
    chunks = []
    with patch("src.services.generation_handler.config", _fake_config(kw.pop("max_poll_attempts", 10))), \
         patch("src.services.generation_handler.debug_logger", MagicMock()), \
         patch("asyncio.sleep", new=AsyncMock()):
        agen = gh._poll_video_result(
            _token(), "proj-1", operations, stream=False,
            generation_result=generation_result, **kw,
        )
        async for c in agen:
            chunks.append(c)
    return chunks, generation_result


def _successful_result(fife_url="https://cdn/v.mp4"):
    return {"operations": [{
        "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
        "operation": {
            "name": "550e8400-e29b-41d4-a716-446655440000",
            "metadata": {"video": {
                "fifeUrl": fife_url,
                "mediaGenerationId": "mgid",
                "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
            }},
        },
    }]}


def test_simple_success_non_stream():
    gh = _make_handler(check_result=_successful_result())
    chunks, gr = asyncio.run(_drive(gh, [{"operation": {"name": "task-1"}}]))

    assert len(chunks) == 1
    payload = json.loads(chunks[0])
    assert "https://cdn/v.mp4" in payload["choices"][0]["message"]["content"]
    assert gr["success"] is True
    # 任务被标记 completed,结果 URL 为 fifeUrl
    gh.db.update_task.assert_awaited()
    _, kwargs = gh.db.update_task.await_args
    assert kwargs["status"] == "completed"
    assert kwargs["result_urls"] == ["https://cdn/v.mp4"]


def test_success_fetches_media_url_when_fife_missing():
    # fifeUrl 缺失但有 mediaGenerationId -> 走 get_media_url 换签名 URL
    gh = _make_handler(check_result=_successful_result(fife_url=None))
    gh.flow_client.get_media_url = AsyncMock(return_value="https://signed/cdn.mp4")
    chunks, gr = asyncio.run(_drive(gh, [{"operation": {"name": "t"}}]))

    gh.flow_client.get_media_url.assert_awaited_once()
    assert gr["success"] is True
    payload = json.loads(chunks[0])
    assert "https://signed/cdn.mp4" in payload["choices"][0]["message"]["content"]


def test_failed_status_returns_502():
    result = {"operations": [{
        "status": "MEDIA_GENERATION_STATUS_FAILED",
        "operation": {"name": "x", "error": {"code": "QUOTA", "message": "配额不足"}},
    }]}
    gh = _make_handler(check_result=result)
    chunks, gr = asyncio.run(_drive(gh, [{"operation": {"name": "x"}}]))

    assert len(chunks) == 1
    err = json.loads(chunks[0])["error"]
    assert err["status_code"] == 502
    assert "配额不足" in err["message"]
    assert gr["success"] is False
    assert "配额不足" in gr["error_message"]


def test_error_prefixed_status_returns_502():
    result = {"operations": [{
        "status": "MEDIA_GENERATION_STATUS_ERROR_SAFETY",
        "operation": {"name": "x"},
    }]}
    gh = _make_handler(check_result=result)
    chunks, gr = asyncio.run(_drive(gh, [{"operation": {"name": "x"}}]))

    err = json.loads(chunks[0])["error"]
    assert err["status_code"] == 502
    assert "MEDIA_GENERATION_STATUS_ERROR_SAFETY" in err["message"]
    assert gr["success"] is False


def test_timeout_returns_504():
    # 所有轮询都返回空 operations -> 耗尽 max_attempts -> 504 超时
    gh = _make_handler(check_result={"operations": []})
    chunks, gr = asyncio.run(_drive(gh, [{"operation": {"name": "t"}}], max_poll_attempts=2))

    assert len(chunks) == 1
    err = json.loads(chunks[0])["error"]
    assert err["status_code"] == 504
    assert "超时" in err["message"]
    assert gr["success"] is False


def test_consecutive_poll_errors_return_502():
    # check_video_status 连续抛异常,达到 3 次上限 -> 502
    gh = _make_handler(check_side_effect=RuntimeError("net down"))
    chunks, gr = asyncio.run(_drive(gh, [{"operation": {"name": "t"}}], max_poll_attempts=10))

    assert len(chunks) == 1
    err = json.loads(chunks[0])["error"]
    assert err["status_code"] == 502
    assert "视频状态查询失败" in err["message"]
    # 恰好在第 3 次连续错误后失败(不是跑满 10 次)
    assert gh.flow_client.check_video_status.await_count == 3
