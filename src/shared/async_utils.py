"""Generic async utilities (shared)."""
import asyncio


async def run_with_timeout(awaitable, timeout_seconds: float, label: str):
    """统一收口异步操作超时，超时抛带 label 的 TimeoutError。"""
    effective_timeout = max(0.5, float(timeout_seconds or 0))
    try:
        return await asyncio.wait_for(awaitable, timeout=effective_timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"{label} 超时 ({effective_timeout:.1f}s)") from e
