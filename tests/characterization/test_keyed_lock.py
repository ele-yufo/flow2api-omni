"""Characterization: lock per-key lock acquisition (same key -> same lock, DCL)."""
import asyncio


def test_get_keyed_lock():
    from src.services.tokens.locks import get_keyed_lock

    async def run():
        lock_map, guard = {}, asyncio.Lock()
        a1 = await get_keyed_lock(lock_map, guard, 1)
        a1_again = await get_keyed_lock(lock_map, guard, 1)
        b = await get_keyed_lock(lock_map, guard, 2)
        return a1 is a1_again, a1 is b, len(lock_map)

    same, different, count = asyncio.run(run())
    assert same is True      # same key -> same lock
    assert different is False # different keys -> different locks
    assert count == 2
