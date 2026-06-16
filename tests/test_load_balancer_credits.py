import unittest
from unittest.mock import AsyncMock

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
