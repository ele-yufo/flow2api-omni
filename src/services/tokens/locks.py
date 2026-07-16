"""Per-key async lock acquisition (double-checked) — extracted from TokenManager.

Generic: avoids serializing unrelated keys behind one global lock. Locked by
tests/characterization/test_keyed_lock.py.
"""
import asyncio


async def get_keyed_lock(lock_map: dict, guard: asyncio.Lock, key) -> asyncio.Lock:
    """按 key 维度获取锁，避免不同 key 之间串行阻塞。"""
    async with guard:
        lock = lock_map.get(key)
        if lock is None:
            lock = asyncio.Lock()
            lock_map[key] = lock
        return lock
