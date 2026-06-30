import types
import unittest
from unittest.mock import AsyncMock

from src.services.browser_captcha_personal import BrowserCaptchaService, ResidentTabInfo


class _FakeTab:
    def __init__(self, result):
        self._result = result

    async def evaluate(self, expression, await_promise=False, return_by_value=False):
        return self._result


class BrowserCaptchaPersonalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()

    @staticmethod
    def _make_remote_object_result(token: str):
        return types.SimpleNamespace(
            type_="object",
            value=None,
            deep_serialized_value=types.SimpleNamespace(
                type_="object",
                value=[
                    ["ok", {"type": "boolean", "value": True}],
                    ["token", {"type": "string", "value": token}],
                ],
            ),
        )

    async def test_tab_evaluate_normalizes_deep_serialized_remote_object(self):
        tab = _FakeTab(self._make_remote_object_result("token-123"))

        result = await self.service._tab_evaluate(
            tab,
            "ignored",
            label="unit_test_tab_evaluate",
            await_promise=True,
            return_by_value=True,
        )

        self.assertEqual(result, {"ok": True, "token": "token-123"})

    async def test_execute_recaptcha_on_tab_accepts_remote_object_success_result(self):
        # Real reCAPTCHA tokens are >200 chars; fixtures must clear the
        # 100-char guard that drops suspected fake tokens (e.g. "undefined").
        realistic_token = "0cAFcWeA7zzq_" + "A" * 200 + "_end"
        tab = _FakeTab(self._make_remote_object_result(realistic_token))

        token = await self.service._execute_recaptcha_on_tab(tab, action="IMAGE_GENERATION")

        self.assertEqual(token, realistic_token)

    async def test_execute_recaptcha_on_tab_drops_short_fake_token(self):
        # JS sometimes resolves with "undefined" (len 9) when grecaptcha
        # isn't fully ready — we must drop these instead of submitting them.
        tab = _FakeTab(self._make_remote_object_result("undefined"))

        token = await self.service._execute_recaptcha_on_tab(tab, action="IMAGE_GENERATION")

        self.assertIsNone(token)

    async def test_create_resident_tab_returns_none_when_browser_missing(self):
        self.service.browser = None

        resident_info = await self.service._create_resident_tab("slot-1", project_id="project-1")

        self.assertIsNone(resident_info)

    async def test_restart_browser_for_project_reuses_recent_healthy_runtime(self):
        resident_info = ResidentTabInfo(tab=object(), slot_id="slot-1", project_id="project-1")
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._initialized = True
        self.service._mark_runtime_restart()
        self.service._probe_browser_runtime = AsyncMock(return_value=True)
        self.service._ensure_resident_tab = AsyncMock(return_value=("slot-1", resident_info))
        self.service._restart_browser_for_project_unlocked = AsyncMock(return_value=True)

        result = await self.service._restart_browser_for_project("project-1")

        self.assertTrue(result)
        self.service._restart_browser_for_project_unlocked.assert_not_awaited()
        self.service._ensure_resident_tab.assert_awaited_once()

    async def test_report_flow_success_clears_streak_for_resolved_slot(self):
        """T1-4 修复：streak 应仅在 GENERATION 真正成功（Google 接受 token）时清零。"""
        # 模拟该 slot 历史有失败 streak
        self.service._resident_error_streaks["slot-1"] = 5
        self.service._resolve_resident_slot_for_project_locked = lambda pid: (
            ("slot-1", object()) if pid == "project-1" else (None, None)
        )

        await self.service.report_flow_success("project-1")

        self.assertNotIn("slot-1", self.service._resident_error_streaks)

    async def test_report_flow_success_with_unknown_project_is_safe_noop(self):
        self.service._resident_error_streaks["slot-1"] = 5
        self.service._resolve_resident_slot_for_project_locked = lambda pid: (None, None)

        await self.service.report_flow_success("unknown-project")

        # 未知 project 不应误清其它 slot 的 streak
        self.assertEqual(self.service._resident_error_streaks.get("slot-1"), 5)

    async def test_report_flow_success_with_empty_project_id_is_noop(self):
        self.service._resident_error_streaks["slot-1"] = 5
        await self.service.report_flow_success("")
        self.assertEqual(self.service._resident_error_streaks.get("slot-1"), 5)


if __name__ == "__main__":
    unittest.main()
