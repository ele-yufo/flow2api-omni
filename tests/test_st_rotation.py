import unittest
from unittest.mock import AsyncMock

from src.core.config import config


class ConfigDefaultsTests(unittest.TestCase):
    def test_st_keepalive_defaults(self):
        self.assertTrue(config.st_keepalive_enabled)
        self.assertEqual(config.st_keepalive_interval_hours, 24)

    def test_st_browser_refresh_disabled_by_default(self):
        # 多账号下浏览器 ST 刷新会写错号，必须默认关闭
        self.assertFalse(config.st_browser_refresh_enabled)

    def test_min_credits_to_select_default(self):
        self.assertGreaterEqual(config.min_credits_to_select, 0)


from src.services.flow_client import FlowClient

LONG_ST = "eyJ" + "C" * 1100


class StToAtRotationTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_rotated_st_helper(self):
        client = FlowClient(proxy_manager=None)
        headers = [
            "__Host-next-auth.csrf-token=abc; Path=/; HttpOnly",
            f"__Secure-next-auth.session-token={LONG_ST}; Path=/; Expires=Thu, 16 Jul 2026 00:00:00 GMT; HttpOnly; Secure",
        ]
        self.assertEqual(client._extract_rotated_st_from_set_cookie(headers), LONG_ST)

    def test_extract_rotated_st_ignores_short(self):
        client = FlowClient(proxy_manager=None)
        headers = ["__Secure-next-auth.session-token=undefined; Path=/"]
        self.assertIsNone(client._extract_rotated_st_from_set_cookie(headers))

    async def test_st_to_at_attaches_rotated_st(self):
        client = FlowClient(proxy_manager=None)

        async def fake_make_request(**kwargs):
            cap = kwargs.get("capture_set_cookie")
            if cap is not None:
                cap.append(f"__Secure-next-auth.session-token={LONG_ST}; Path=/")
            return {"access_token": "AT", "expires": "2026-07-16T00:00:00.000Z", "user": {"email": "x@y.com"}}

        client._make_request = AsyncMock(side_effect=fake_make_request)
        result = await client.st_to_at("old-st-value")
        self.assertEqual(result["access_token"], "AT")
        self.assertEqual(result["rotated_st"], LONG_ST)
