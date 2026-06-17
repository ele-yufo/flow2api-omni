import unittest
from unittest.mock import AsyncMock, patch

from src.services.alert_notifier import build_discord_payload, AlertNotifier


class BuildPayloadTests(unittest.TestCase):
    def test_critical_red_embed_with_fields(self):
        p = build_discord_payload(
            title="账号失效需重登",
            description="账号 a@b.com 的 ST 已失效",
            fields=[("账号", "a@b.com", True), ("Token ID", "7", True), ("建议操作", "重登并粘贴 cookies.txt", False)],
            severity="critical",
        )
        self.assertIn("embeds", p)
        embed = p["embeds"][0]
        self.assertEqual(embed["title"], "账号失效需重登")
        self.assertEqual(embed["description"], "账号 a@b.com 的 ST 已失效")
        self.assertEqual(embed["color"], 15158332)  # red
        self.assertEqual(len(embed["fields"]), 3)
        self.assertEqual(embed["fields"][0], {"name": "账号", "value": "a@b.com", "inline": True})
        self.assertIn("timestamp", embed)
        self.assertIn("username", p)

    def test_warning_orange(self):
        p = build_discord_payload(title="额度耗尽", description="x", fields=None, severity="warning")
        self.assertEqual(p["embeds"][0]["color"], 15105570)  # orange
        self.assertEqual(p["embeds"][0].get("fields", []), [])


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_url_is_log_only_no_post(self):
        n = AlertNotifier("")
        with patch("src.services.alert_notifier.AsyncSession") as S:
            ok = await n.send_alert("t", "d")
        self.assertFalse(ok)
        S.assert_not_called()

    async def test_posts_discord_body(self):
        n = AlertNotifier("https://discord.test/webhook")
        sent = {}
        class FakeSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, **kw): sent["url"] = url; sent["json"] = kw.get("json")
        with patch("src.services.alert_notifier.AsyncSession", return_value=FakeSession()):
            ok = await n.send_alert("账号失效", "desc", fields=[("账号", "a@b.com", True)], severity="critical")
        self.assertTrue(ok)
        self.assertEqual(sent["url"], "https://discord.test/webhook")
        self.assertIn("embeds", sent["json"])

    async def test_post_exception_returns_false(self):
        n = AlertNotifier("https://discord.test/webhook")
        class BoomSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): raise RuntimeError("network down")
        with patch("src.services.alert_notifier.AsyncSession", return_value=BoomSession()):
            ok = await n.send_alert("t", "d")
        self.assertFalse(ok)
