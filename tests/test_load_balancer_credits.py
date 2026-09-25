import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

from src.services.concurrency_manager import ConcurrencyManager
from src.services.load_balancer import LoadBalancer
from src.core.models import Token


class CreditsFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_drained_tokens(self):
        tokens = [
            Token(id=1, st="a", email="a@x.com", credits=0, user_paygate_tier="PAYGATE_TIER_ONE"),
            Token(id=2, st="b", email="b@x.com", credits=500, user_paygate_tier="PAYGATE_TIER_ONE"),
        ]
        tm = AsyncMock()
        tm.get_active_tokens = AsyncMock(return_value=tokens)
        tm.needs_at_refresh = lambda t: False
        tm.ensure_valid_token = AsyncMock(side_effect=lambda t: t)
        lb = LoadBalancer(token_manager=tm, concurrency_manager=None)
        lb._get_token_load = AsyncMock(return_value=(0, 999))
        selected = await lb.select_token(for_image_generation=True)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, 2)  # 额度为 0 的被跳过


def _make_lb(tokens, loads):
    tm = AsyncMock()
    tm.get_active_tokens = AsyncMock(return_value=tokens)
    tm.needs_at_refresh = lambda t: False
    tm.ensure_valid_token = AsyncMock(side_effect=lambda t: t)
    lb = LoadBalancer(token_manager=tm, concurrency_manager=None)
    lb._get_token_load = AsyncMock(
        side_effect=lambda token_id, for_image_generation, for_video_generation: loads[token_id]
    )
    return lb


class TierPreferenceTests(unittest.IsolatedAsyncioTestCase):
    def _tokens(self):
        return [
            Token(id=1, st="a", email="pro@x.com", credits=500, user_paygate_tier="PAYGATE_TIER_ONE"),
            Token(id=2, st="b", email="ult@x.com", credits=500, user_paygate_tier="PAYGATE_TIER_TWO"),
            Token(id=3, st="c", email="free@x.com", credits=500, user_paygate_tier=None),
        ]

    async def test_prefers_higher_tier_despite_higher_inflight(self):
        # Ult inflight=2 > Pro inflight=0，tier 优先时仍选 Ult
        lb = _make_lb(self._tokens(), {1: (0, 999), 2: (2, 999), 3: (0, 999)})
        selected = await lb.select_token(for_image_generation=True)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, 2)

    async def test_tier_preference_disabled_falls_back_to_load(self):
        from src.services import load_balancer as lb_module

        # Free inflight=1 > Pro inflight=0，关掉 tier 优先后 Pro 是唯一最低负载
        lb = _make_lb(self._tokens(), {1: (0, 999), 2: (2, 999), 3: (1, 999)})
        with patch.object(
            type(lb_module.config), "prefer_higher_tier_accounts", new_callable=PropertyMock
        ) as mock_prop:
            mock_prop.return_value = False
            selected = await lb.select_token(for_image_generation=True)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, 1)  # 关掉 tier 优先后回到纯负载：inflight 最低的 Pro

    async def test_tier_preference_breaks_tie_deterministically(self):
        # 同负载下 Pro/Free 之间也该是高层级先被选中（不再靠 random 抛硬币）
        lb = _make_lb(self._tokens(), {1: (0, 999), 2: (0, 999), 3: (0, 999)})
        selected = await lb.select_token(for_image_generation=True)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, 2)


class TierOverflowTests(unittest.IsolatedAsyncioTestCase):
    """并发打满的高层级账号必须让位，否则一个 Ult 号会独自扛下整个并发。

    这里必须用真实的 ConcurrencyManager + 真实的 pending 计数：生成主路径不走
    reserve，manager 的 inflight 恒为 0，用 mock 把 can_use_* 打成 False 是现实中
    不存在的状态——那样测试会全绿，而线上照旧全压在一个账号上。
    """

    def _tokens(self, image_concurrency=2):
        return [
            Token(id=1, st="a", email="pro@x.com", credits=500,
                  user_paygate_tier="PAYGATE_TIER_ONE", image_concurrency=image_concurrency),
            Token(id=2, st="b", email="ult@x.com", credits=500,
                  user_paygate_tier="PAYGATE_TIER_TWO", image_concurrency=image_concurrency),
            Token(id=3, st="c", email="free@x.com", credits=500,
                  user_paygate_tier=None, image_concurrency=image_concurrency),
        ]

    async def _make_real_lb(self, tokens):
        cm = ConcurrencyManager()
        await cm.initialize(tokens)
        tm = AsyncMock()
        tm.get_active_tokens = AsyncMock(return_value=tokens)
        tm.needs_at_refresh = lambda t: False
        tm.ensure_valid_token = AsyncMock(side_effect=lambda t: t)
        return LoadBalancer(token_manager=tm, concurrency_manager=cm)

    async def _pick(self, lb, model=None):
        """模拟一次生成请求的选号（与 generation_handler 的调用参数一致）。"""

        return await lb.select_token(
            for_image_generation=True,
            model=model,
            reserve=False,
            enforce_concurrency_filter=False,
            track_pending=True,
        )

    async def test_burst_overflows_across_tiers(self):
        # 每个账号并发上限 2：前两发打满 Ult，之后必须溢出到 Pro，再溢出到 Free
        lb = await self._make_real_lb(self._tokens(image_concurrency=2))
        picks = [(await self._pick(lb)).id for _ in range(6)]
        self.assertEqual(picks[:2], [2, 2], f"前两发应落在 Ult：{picks}")
        self.assertEqual(sorted(picks), [1, 1, 2, 2, 3, 3], f"6 发应打满三个账号：{picks}")
        self.assertEqual(picks[2:4], [1, 1], f"Ult 打满后应溢出到 Pro：{picks}")

    async def test_single_request_still_prefers_ult(self):
        # 没有并发压力时，层级优先不受影响
        lb = await self._make_real_lb(self._tokens())
        self.assertEqual((await self._pick(lb)).id, 2)

    async def test_released_slot_returns_to_ult(self):
        # Ult 的请求结束后，下一发应该回到 Ult，而不是继续留在低层级
        lb = await self._make_real_lb(self._tokens(image_concurrency=1))
        first = await self._pick(lb)
        self.assertEqual(first.id, 2)
        self.assertEqual((await self._pick(lb)).id, 1)  # Ult 已满 → Pro
        await lb.release_pending(2, for_image_generation=True)
        self.assertEqual((await self._pick(lb)).id, 2)  # Ult 腾出来了 → 回到 Ult

    async def test_all_saturated_still_returns_a_token(self):
        # 全池打满时不能直接失败：仍要给出账号，让请求进下游等待队列
        lb = await self._make_real_lb(self._tokens(image_concurrency=1))
        for _ in range(3):
            await self._pick(lb)
        selected = await self._pick(lb)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, 2, "都打满时回到层级优先")

    async def test_unlimited_concurrency_is_never_saturated(self):
        # 未设并发上限的账号不存在"打满"，层级优先照常生效
        lb = await self._make_real_lb(self._tokens(image_concurrency=0))
        picks = [(await self._pick(lb)).id for _ in range(4)]
        self.assertEqual(picks, [2, 2, 2, 2], f"无上限时应始终优先 Ult：{picks}")

    async def test_2k_images_spread_across_pro_and_keep_ultra_for_4k(self):
        tokens = [
            Token(id=1, st="a", email="pro1@x.com", credits=500,
                  user_paygate_tier="PAYGATE_TIER_ONE", image_concurrency=-1),
            Token(id=2, st="b", email="ult@x.com", credits=500,
                  user_paygate_tier="PAYGATE_TIER_TWO", image_concurrency=-1),
            Token(id=3, st="c", email="pro2@x.com", credits=500,
                  user_paygate_tier="PAYGATE_TIER_ONE", image_concurrency=-1),
        ]
        lb = await self._make_real_lb(tokens)
        picks = [
            (await self._pick(lb, "gemini-3.1-flash-image-landscape-2k")).id
            for _ in range(4)
        ]
        self.assertEqual(sorted(picks), [1, 1, 3, 3])
        self.assertEqual(
            (await self._pick(lb, "gemini-3.1-flash-image-landscape-4k")).id,
            2,
        )
