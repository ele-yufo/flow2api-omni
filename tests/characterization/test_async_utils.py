"""Characterization: lock run_with_timeout (pass-through + labeled timeout)."""
import asyncio
import pytest


def test_run_with_timeout_passthrough():
    from src.shared.async_utils import run_with_timeout
    async def quick(): return 42
    assert asyncio.run(run_with_timeout(quick(), 5, "op")) == 42


def test_run_with_timeout_raises_labeled():
    from src.shared.async_utils import run_with_timeout
    async def slow():
        await asyncio.sleep(10)
    with pytest.raises(TimeoutError, match="myop 超时"):
        asyncio.run(run_with_timeout(slow(), 0.5, "myop"))
