import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

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
