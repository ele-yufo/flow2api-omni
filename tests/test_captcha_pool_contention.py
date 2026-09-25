"""打码池竞争行为测试。

覆盖 2026-09-25 生图慢事故的两处池层根因：
1. `_ensure_resident_tab` 慢路径：排队建 tab 期间已有 tab 释放时应立即复用，
   而不是干等 build 锁（实测 7 个请求空转 7s，两个空闲 tab 无人用）。
2. 空闲回收保底：低流量后池子不得被回收到 `min_resident_tabs` 以下，
   否则波峰要重付每 tab ~7s 的建造成本（预热满 5 个→10 分钟后剩 2）。
"""
import asyncio
import time
import unittest
import types

from src.services.browser_captcha_personal import BrowserCaptchaService, ResidentTabInfo


def _make_resident(service: BrowserCaptchaService, slot_id: str, *, ready: bool = True) -> ResidentTabInfo:
    info = ResidentTabInfo(tab=types.SimpleNamespace(), slot_id=slot_id)
    info.recaptcha_ready = ready
    service._resident_tabs[slot_id] = info
    return info


class EnsureResidentTabContentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()
        self.service._max_resident_tabs = 3
        self.service._min_resident_tabs = 2

    async def test_reuses_released_slot_while_waiting_for_build_lock(self):
        """慢路径等 build 锁期间，已释放的 ready tab 应被立即复用。"""
        busy = _make_resident(self.service, "slot-1")
        _make_resident(self.service, "slot-2")
        # 初始全忙（进入慢路径的条件）
        await busy.solve_lock.acquire()

        build_started = asyncio.Event()
        created: list[str] = []

        async def hold_build_lock():
            await self.service._tab_build_lock.acquire()
            build_started.set()
            await asyncio.sleep(30)  # 模拟建 tab 的长加载，测试期间一直持有

        async def release_slot_later():
            await asyncio.sleep(0.8)
            busy.solve_lock.release()

        async def fake_create(slot_id, project_id=None):
            created.append(slot_id)
            return None

        self.service._create_resident_tab = fake_create

        holder = asyncio.create_task(hold_build_lock())
        releaser = asyncio.create_task(release_slot_later())
        await build_started.wait()

        started_at = time.monotonic()
        slot_id, resident_info = await self.service._ensure_resident_tab(
            "proj-a", return_slot_key=True
        )
        elapsed = time.monotonic() - started_at

        # 复用了已释放的 slot（slot-1 或 slot-2 均可），没有等建 tab
        self.assertIn(slot_id, ("slot-1", "slot-2"))
        self.assertTrue(resident_info.recaptcha_ready)
        self.assertFalse(resident_info.solve_lock.locked())
        self.assertEqual(created, [])
        # 0.8s 释放 + 回查周期 0.5s 内返回，远短于干等 build 锁
        self.assertLess(elapsed, 2.5)

        holder.cancel()
        releaser.cancel()
        await asyncio.gather(holder, releaser, return_exceptions=True)

    async def test_recheck_does_not_return_cold_slot(self):
        """回查只认 recaptcha_ready 的 tab；cold tab（未就绪）解锁后也不得提前返回。"""
        _make_resident(self.service, "slot-1", ready=True)
        cold = _make_resident(self.service, "slot-2", ready=False)
        busy = self.service._resident_tabs["slot-1"]
        # 全忙才进慢路径：slot-1、slot-2 都先锁上
        await busy.solve_lock.acquire()
        await cold.solve_lock.acquire()

        async def fake_create(slot_id, project_id=None):
            info = ResidentTabInfo(tab=types.SimpleNamespace(), slot_id=slot_id)
            info.recaptcha_ready = True
            return info

        self.service._create_resident_tab = fake_create
        # 占住 build 锁，让回查周期内 slot-2 解锁但仍 cold
        await self.service._tab_build_lock.acquire()
        ensure_task = asyncio.create_task(
            self.service._ensure_resident_tab("proj-a", return_slot_key=True)
        )
        await asyncio.sleep(0.3)
        cold.solve_lock.release()  # cold tab 空闲了，但 recaptcha_ready=False
        await asyncio.sleep(0.9)  # 越过多个回查周期
        self.assertFalse(ensure_task.done())  # 没有被 cold slot 满足

        self.service._tab_build_lock.release()
        slot_id, resident_info = await ensure_task
        self.assertTrue(resident_info.recaptcha_ready)
        self.assertNotEqual(slot_id, "slot-2")  # 新建而非复用 cold

    async def test_force_create_still_waits_for_build_lock(self):
        """force_create=True（预热场景）不得回查复用，必须走建 tab 路径。"""
        self.service._resident_slot_seq = 10  # 避开手工命名的 slot-1
        _make_resident(self.service, "slot-1")  # 空闲也不许复用
        build_started = asyncio.Event()

        async def hold_build_lock():
            await self.service._tab_build_lock.acquire()
            build_started.set()
            await asyncio.sleep(30)

        async def fake_create(slot_id, project_id=None):
            info = ResidentTabInfo(tab=types.SimpleNamespace(), slot_id=slot_id)
            info.recaptcha_ready = True
            return info

        self.service._create_resident_tab = fake_create

        holder = asyncio.create_task(hold_build_lock())
        await build_started.wait()

        ensure_task = asyncio.create_task(
            self.service._ensure_resident_tab("proj-a", force_create=True, return_slot_key=True)
        )
        await asyncio.sleep(1.4)  # 超过两个回查周期，确认没有提前返回空闲 slot
        self.assertFalse(ensure_task.done())

        self.service._tab_build_lock.release()  # 让出 build 锁，fake_create 生效
        slot_id, resident_info = await ensure_task
        self.assertIsNotNone(resident_info)
        self.assertNotEqual(slot_id, "slot-1")  # 新建的，不是复用

        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)


class IdleReaperFloorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()
        self.service._max_resident_tabs = 5
        self.service._min_resident_tabs = 2
        self.service._idle_tab_ttl_seconds = 600

    def _age_all(self, idle_seconds: float):
        for info in self.service._resident_tabs.values():
            info.last_used_at = time.time() - idle_seconds

    async def test_reaper_keeps_min_resident_tabs(self):
        """全部空闲超时也最多回收到保底数。"""
        for i in range(4):
            _make_resident(self.service, f"slot-{i}")
        self._age_all(1200)  # 全部超时

        tabs_to_close = await self.service._collect_idle_tabs_to_close()

        self.assertEqual(len(tabs_to_close), 2)  # 4 - min(2) = 2

    async def test_reaper_skips_busy_tabs(self):
        """执行中的 tab 即使超时也不回收。"""
        infos = [_make_resident(self.service, f"slot-{i}") for i in range(4)]
        self._age_all(1200)
        await infos[3].solve_lock.acquire()  # slot-3 在跑

        tabs_to_close = await self.service._collect_idle_tabs_to_close()

        self.assertEqual(len(tabs_to_close), 2)
        self.assertNotIn("slot-3", tabs_to_close)

    async def test_reaper_floor_zero_allows_full_reap(self):
        """min=0 时允许全部回收（语义兜底）。"""
        self.service._min_resident_tabs = 0
        for i in range(3):
            _make_resident(self.service, f"slot-{i}")
        self._age_all(1200)

        tabs_to_close = await self.service._collect_idle_tabs_to_close()

        self.assertEqual(len(tabs_to_close), 3)

    async def test_reaper_noop_when_within_ttl(self):
        """未超时的 tab 不回收。"""
        for i in range(3):
            _make_resident(self.service, f"slot-{i}")
        self._age_all(60)

        tabs_to_close = await self.service._collect_idle_tabs_to_close()

        self.assertEqual(tabs_to_close, [])


if __name__ == "__main__":
    unittest.main()
