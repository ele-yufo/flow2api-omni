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
