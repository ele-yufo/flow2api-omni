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

    async def test_st_to_at_no_rotated_st_when_same(self):
        client = FlowClient(proxy_manager=None)

        async def fake_make_request(**kwargs):
            cap = kwargs.get("capture_set_cookie")
            if cap is not None:
                cap.append(f"__Secure-next-auth.session-token={LONG_ST}; Path=/")
            return {"access_token": "AT", "expires": "2026-07-16T00:00:00.000Z", "user": {}}

        client._make_request = AsyncMock(side_effect=fake_make_request)
        result = await client.st_to_at(LONG_ST)  # 入参 ST 与回发 ST 相同 → 不附 rotated_st
        self.assertNotIn("rotated_st", result)


from src.services.token_manager import TokenManager
from src.core.models import Token


class FakeDB:
    def __init__(self, token):
        self._token = token
        self.updates = []

    async def get_token(self, token_id):
        return self._token

    async def update_token(self, token_id, **kwargs):
        self.updates.append(kwargs)
        for k, v in kwargs.items():
            setattr(self._token, k, v)


class PersistRotatedStTests(unittest.IsolatedAsyncioTestCase):
    def _make_manager(self, rotated_st):
        token = Token(id=7, st="old-st", email="ruby@gmail.com", at="old-at", credits=1000)
        db = FakeDB(token)
        tm = TokenManager(db=db, flow_client=None)
        fake_flow = AsyncMock()
        fake_flow.st_to_at = AsyncMock(return_value={
            "access_token": "new-at",
            "expires": "2026-07-16T00:00:00.000Z",
            "user": {"email": "ruby@gmail.com"},
            "rotated_st": rotated_st,
        })
        fake_flow.get_credits = AsyncMock(return_value={"credits": 1000, "userPaygateTier": "PAYGATE_TIER_ONE"})
        tm.flow_client = fake_flow
        return tm, db

    async def test_do_refresh_at_persists_rotated_st(self):
        new_st = "eyJ" + "D" * 1100
        tm, db = self._make_manager(new_st)
        ok = await tm._do_refresh_at(7, "old-st")
        self.assertTrue(ok)
        self.assertTrue(any(u.get("st") == new_st for u in db.updates))

    async def test_do_refresh_at_skips_when_rotated_equals_current(self):
        tm, db = self._make_manager("old-st")  # 与当前相同
        await tm._do_refresh_at(7, "old-st")
        self.assertFalse(any("st" in u for u in db.updates))


class KeepaliveConservativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_does_not_disable(self):
        token = Token(id=7, st="old-st", email="r@x.com", is_active=True)
        db = FakeDB(token)
        tm = TokenManager(db=db, flow_client=AsyncMock())
        tm._do_refresh_at = AsyncMock(return_value=False)  # 刷新失败，但 ban_reason 未变
        tm.disable_token = AsyncMock()
        tm._send_st_alert = AsyncMock()
        ok = await tm.keepalive_rotate_st(7)
        self.assertFalse(ok)
        tm.disable_token.assert_not_called()  # 瞬时错误不禁用
        tm._send_st_alert.assert_not_called()

    async def test_revoked_disables_and_alerts(self):
        token = Token(id=7, st="old-st", email="r@x.com", is_active=True)
        db = FakeDB(token)
        tm = TokenManager(db=db, flow_client=AsyncMock())

        async def fake_refresh(tid, st):
            token.ban_reason = "ST_REVOKED"  # 模拟 _do_refresh_at 在确认 401 时标记
            return False

        tm._do_refresh_at = AsyncMock(side_effect=fake_refresh)
        tm.disable_token = AsyncMock()
        tm._send_st_alert = AsyncMock()
        ok = await tm.keepalive_rotate_st(7)
        self.assertFalse(ok)
        tm.disable_token.assert_called_once()
        tm._send_st_alert.assert_called_once()

    async def test_stale_revoked_not_refalse_disabled_on_transient(self):
        # 账号历史上被标过 ST_REVOKED（但仍 active）；本次只是瞬时失败、未重新标记。
        # 不应因历史遗留原因误杀。
        token = Token(id=7, st="old-st", email="r@x.com", is_active=True, ban_reason="ST_REVOKED")
        db = FakeDB(token)
        tm = TokenManager(db=db, flow_client=AsyncMock())
        tm._do_refresh_at = AsyncMock(return_value=False)  # 瞬时失败，ban_reason 未变
        tm.disable_token = AsyncMock()
        tm._send_st_alert = AsyncMock()
        ok = await tm.keepalive_rotate_st(7)
        self.assertFalse(ok)
        tm.disable_token.assert_not_called()
        tm._send_st_alert.assert_not_called()
