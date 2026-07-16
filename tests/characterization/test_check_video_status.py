"""Characterization: lock check_video_status polling behavior (happy path + retry)."""
import asyncio
from unittest.mock import AsyncMock


def _fc():
    from src.services.flow_client import FlowClient
    return FlowClient(None)


def test_check_video_status_happy_path():
    fc = _fc()
    fc._make_request = AsyncMock(return_value={"operations": [{"operation": {"name": "t1"},
                                                               "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL"}]})
    result = asyncio.run(fc.check_video_status("AT", [{"operation": {"name": "t1"}}]))
    assert result["operations"][0]["status"] == "MEDIA_GENERATION_STATUS_SUCCESSFUL"
    assert fc._make_request.await_count == 1


def test_check_video_status_retries_then_succeeds(monkeypatch):
    fc = _fc()
    calls = {"n": 0}

    async def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("curl: (16) HTTP/2 framing")  # retryable
        return {"operations": []}

    fc._make_request = AsyncMock(side_effect=flaky)
    async def _nosleep(*a, **k): return None
    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    result = asyncio.run(fc.check_video_status("AT", [{"operation": {"name": "t1"}}]))
    assert result == {"operations": []}
    assert calls["n"] == 2  # retried once after retryable error
