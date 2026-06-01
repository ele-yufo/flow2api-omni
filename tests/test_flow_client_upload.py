import asyncio
import unittest
from unittest.mock import AsyncMock

from src.core.config import config
from src.core.logger import debug_logger
from src.services.flow_client import FlowClient


JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16


class FlowClientUploadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_scoped_upload_uses_new_endpoint_with_project_id(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            return {
                "media": {
                    "name": "new-media-id",
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(request_calls), 1)
        self.assertTrue(request_calls[0]["url"].endswith("/flow/uploadImage"))
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )

    async def test_project_scoped_upload_does_not_fallback_to_legacy_endpoint(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            self.fail("带 project_id 的上传不应回退到 legacy 接口")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        with self.assertRaisesRegex(RuntimeError, "legacy :uploadUserImage fallback is disabled"):
            await client.upload_image(
                at="test-at",
                image_bytes=JPEG_BYTES,
                aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                project_id="project-123",
            )

        self.assertEqual(len(request_calls), 1)
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )

    async def test_upload_without_project_id_keeps_legacy_fallback(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            if kwargs["url"].endswith(":uploadUserImage"):
                return {
                    "mediaGenerationId": {
                        "mediaGenerationId": "legacy-media-id",
                    }
                }
            self.fail(f"Unexpected url: {kwargs['url']}")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=None,
        )

        self.assertEqual(media_id, "legacy-media-id")
        self.assertEqual(len(request_calls), 2)
        self.assertNotIn(
            "projectId",
            request_calls[1]["json_data"]["clientContext"],
        )


class FlowClientFingerprintTests(unittest.TestCase):
    def test_fallback_user_agent_is_chromium_only(self):
        client = FlowClient(proxy_manager=None)

        user_agent = client._generate_user_agent("account-1")

        self.assertIn("Chrome/", user_agent)
        self.assertNotIn("Firefox/", user_agent)
        self.assertNotIn("Version/", user_agent)

    def test_fingerprint_headers_do_not_get_android_defaults(self):
        client = FlowClient(proxy_manager=None)

        headers = client._build_request_headers(
            headers=None,
            st_token=None,
            at_token="at-token",
            use_st=False,
            use_at=True,
            fingerprint={
                "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
                "accept_language": "zh-CN",
                "sec_ch_ua_mobile": "?0",
                "sec_ch_ua_platform": "\"Linux\"",
            },
        )

        self.assertEqual(headers["User-Agent"].split("Chrome/")[1].split(" ")[0], "132.0.0.0")
        self.assertEqual(headers["sec-ch-ua-mobile"], "?0")
        self.assertEqual(headers["sec-ch-ua-platform"], "\"Linux\"")

    def test_browser_fetch_headers_keep_only_cors_safe_business_headers(self):
        from src.services.browser_captcha_personal import BrowserCaptchaService

        headers = BrowserCaptchaService._browser_fetch_headers(
            {
                "authorization": "Bearer at-token",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
                "sec-ch-ua": '"Chrome";v="132"',
                "x-browser-validation": "value",
                "x-client-data": "value",
            }
        )

        self.assertEqual(headers, {
            "authorization": "Bearer at-token",
            "Content-Type": "application/json",
        })

    def test_personal_captcha_uses_lightweight_auth_endpoint(self):
        """reCAPTCHA tab 必须用轻量 JSON 端点。

        SPA 主页 /fx/tools/flow/project/{id} 在未登录态下永远到不了
        readyState=complete，warmup 7s 全超时；auth/providers 是固定 JSON
        返回，<1s 就 ready，token 评分跟页面 origin/siteKey/fingerprint 有
        关，不依赖页面 body。
        """
        from src.services.browser_captcha_personal import BrowserCaptchaService

        self.assertEqual(
            BrowserCaptchaService._flow_recaptcha_page_url("project-1"),
            "https://labs.google/fx/api/auth/providers",
        )
        self.assertEqual(
            BrowserCaptchaService._flow_recaptcha_page_url(None),
            "https://labs.google/fx/api/auth/providers",
        )

    def test_captcha_failure_cooldown_increases_then_clears(self):
        client = FlowClient(proxy_manager=None)

        first_delay = client._record_captcha_rejection("project-1")
        second_delay = client._record_captcha_rejection("project-1")

        self.assertGreater(second_delay, first_delay)
        self.assertGreater(client._get_captcha_cooldown_delay("project-1"), 0)

        client._clear_captcha_rejection("project-1")

        self.assertEqual(client._get_captcha_cooldown_delay("project-1"), 0)

    def test_recaptcha_bound_requests_prefer_browser_submit(self):
        client = FlowClient(proxy_manager=None)
        original_flow_config = dict(config._config.get("flow", {}))
        config._config.setdefault("flow", {})["browser_submit_enabled"] = True
        try:
            client._set_request_browser_context({"method": "personal"})
            client._make_request_via_captcha_browser = AsyncMock(return_value={"ok": True})

            result = asyncio.run(
                client._make_request(
                    method="POST",
                    url=f"{client.api_base_url}/video:batchAsyncGenerateVideoText",
                    json_data={
                        "clientContext": {
                            "projectId": "project-1",
                            "recaptchaContext": {"token": "recaptcha-token"},
                        }
                    },
                    use_at=True,
                    at_token="at-token",
                )
            )
        finally:
            config._config["flow"] = original_flow_config
            client.clear_request_fingerprint()

        self.assertEqual(result, {"ok": True})
        client._make_request_via_captcha_browser.assert_awaited_once()


class BrowserCaptchaPersonalTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_submit_tab_is_popped_once_by_project(self):
        from src.services.browser_captcha_personal import BrowserCaptchaService

        service = BrowserCaptchaService(db=None)
        tab = object()

        await service._remember_legacy_submit_tab("project-1", tab)

        self.assertIs(await service._pop_legacy_submit_tab("project-1"), tab)
        self.assertIsNone(await service._pop_legacy_submit_tab("project-1"))


class PersistentProfileTests(unittest.TestCase):
    """验证 captcha 持久化 profile 配置生效路径。

    持久化 profile 把 nodriver 的 user-data-dir 固定到一个目录，让 Google
    登录态 cookie 跨重启保留 — 这是降低 PUBLIC_ERROR_UNUSUAL_ACTIVITY 拒绝
    率的关键路径。
    """

    def setUp(self):
        self._original_captcha = dict(config._config.get("captcha", {}))

    def tearDown(self):
        config._config["captcha"] = self._original_captcha

    def _new_service(self):
        from src.services.browser_captcha_personal import BrowserCaptchaService
        return BrowserCaptchaService(db=None)

    def test_disabled_falls_back_to_temp_dir(self):
        """未启用时 user_data_dir=None，nodriver 自动用临时目录（原有行为）。"""
        config._config.setdefault("captcha", {})["persistent_profile_enabled"] = False
        service = self._new_service()
        self.assertIsNone(service.user_data_dir)
        self.assertFalse(service._persistent_profile_enabled)

    def test_enabled_uses_configured_path(self):
        config._config.setdefault("captcha", {})["persistent_profile_enabled"] = True
        config._config["captcha"]["persistent_profile_path"] = "/tmp/flow2api-test-profile"
        service = self._new_service()
        self.assertEqual(service.user_data_dir, "/tmp/flow2api-test-profile")
        self.assertTrue(service._persistent_profile_enabled)

    def test_singleton_lock_present_raises(self):
        """profile 被 GUI Chrome 占用时启动应直接失败，而不是 nodriver 神秘 hang。"""
        import os
        import tempfile
        with tempfile.TemporaryDirectory(prefix="flow2api-profile-test-") as tmp:
            singleton = os.path.join(tmp, "SingletonLock")
            with open(singleton, "w") as f:
                f.write("dummy")
            config._config.setdefault("captcha", {})["persistent_profile_enabled"] = True
            config._config["captcha"]["persistent_profile_path"] = tmp
            service = self._new_service()
            with self.assertRaisesRegex(RuntimeError, "SingletonLock"):
                service._validate_persistent_profile()

    def test_missing_cookies_does_not_raise(self):
        """空 profile 不应抛错，只 warning — 让 nodriver 自己初始化（等同匿名）。"""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="flow2api-profile-test-") as tmp:
            config._config.setdefault("captcha", {})["persistent_profile_enabled"] = True
            config._config["captcha"]["persistent_profile_path"] = tmp
            service = self._new_service()
            service._validate_persistent_profile()  # 不抛即通过

    def test_nonexistent_path_does_not_raise(self):
        """目录不存在时只 warning 引导用户去 GUI 登录，不阻塞启动。"""
        config._config.setdefault("captcha", {})["persistent_profile_enabled"] = True
        config._config["captcha"]["persistent_profile_path"] = "/tmp/flow2api-not-exist-12345"
        service = self._new_service()
        service._validate_persistent_profile()


class GeminiOmniModelRegistryTests(unittest.TestCase):
    """验证 Gemini Omni Flash (abra) 模型注册项的字段一致性。

    上游 model key 形如 abra_{t2v|r2v}_{4|6|8|10}s；32 个 OpenAI 命名变体覆盖
    T2V/R2V × landscape/portrait × 4 时长 × {原版, 1080p 上采样}。
    """

    def setUp(self):
        from src.services.generation_handler import MODEL_CONFIG
        self.cfg = MODEL_CONFIG

    def test_thirty_two_omni_entries_registered(self):
        omni = [k for k in self.cfg if k.startswith("gemini_omni_")]
        self.assertEqual(len(omni), 32, f"expected 32 entries, found {len(omni)}: {omni}")

    def test_t2v_entries_have_no_image_support(self):
        for name, cfg in self.cfg.items():
            if not name.startswith("gemini_omni_t2v_"):
                continue
            self.assertEqual(cfg["video_type"], "t2v", name)
            self.assertFalse(cfg["supports_images"], name)
            self.assertTrue(cfg.get("use_v2_model_config"), name)

    def test_omni_disables_tier_upgrade(self):
        """abra 系列没有 _ultra 变体，TIER_TWO 自动升级会变成不存在的 model_key
        (e.g. abra_t2v_4s_ultra)，上游返回 HTTP 500。必须 allow_tier_upgrade=False。"""
        for name, cfg in self.cfg.items():
            if not name.startswith("gemini_omni_"):
                continue
            self.assertFalse(
                cfg.get("allow_tier_upgrade", True),
                f"{name} must set allow_tier_upgrade=False to prevent _ultra suffix injection",
            )

    def test_r2v_entries_carry_image_constraints(self):
        """abra R2V 上游官网最多接受 7 张 reference；3 张已实测通过。"""
        for name, cfg in self.cfg.items():
            if not name.startswith("gemini_omni_r2v_"):
                continue
            self.assertEqual(cfg["video_type"], "r2v", name)
            self.assertTrue(cfg["supports_images"], name)
            self.assertEqual(cfg["min_images"], 0, name)
            self.assertEqual(cfg["max_images"], 7, name)
            self.assertTrue(cfg.get("use_v2_model_config"), name)

    def test_model_key_matches_duration_suffix(self):
        """gemini_omni_t2v_6s 必须映射到上游 abra_t2v_6s（时长后缀准确）。"""
        for duration in ("4s", "6s", "8s", "10s"):
            for kind in ("t2v", "r2v"):
                upstream = f"abra_{kind}_{duration}"
                for tail in ("", "_1080p"):
                    for orientation, aspect in (
                        ("", "VIDEO_ASPECT_RATIO_LANDSCAPE"),
                        ("_portrait", "VIDEO_ASPECT_RATIO_PORTRAIT"),
                    ):
                        name = f"gemini_omni_{kind}{orientation}_{duration}{tail}"
                        cfg = self.cfg.get(name)
                        self.assertIsNotNone(cfg, f"missing entry: {name}")
                        self.assertEqual(cfg["model_key"], upstream, name)
                        self.assertEqual(cfg["aspect_ratio"], aspect, name)

    def test_1080p_variants_use_existing_veo_upsampler(self):
        """1080p 变体上采样必须复用现有的 veo_3_1_upsampler_1080p（HAR 抓包验证）。"""
        for name, cfg in self.cfg.items():
            if not name.startswith("gemini_omni_"):
                continue
            if not name.endswith("_1080p"):
                self.assertNotIn("upsample", cfg, f"{name} should not have upsample")
                continue
            up = cfg["upsample"]
            self.assertEqual(up["resolution"], "VIDEO_RESOLUTION_1080P", name)
            self.assertEqual(up["model_key"], "veo_3_1_upsampler_1080p", name)


class DebugLoggerSanitizationTests(unittest.TestCase):
    def test_sanitizes_tokens_cookies_and_large_payloads(self):
        sanitized = debug_logger._sanitize_for_log(
            {
                "authorization": "Bearer ya29.secret-token",
                "set-cookie": "__Secure-next-auth.session-token=st-secret-value; Path=/",
                "clientContext": {
                    "recaptchaContext": {
                        "token": "recaptcha-secret-value",
                        "gRecaptchaResponse": "grecaptcha-secret-value",
                    }
                },
                "imageBytes": "a" * 500,
            }
        )

        as_text = str(sanitized)
        self.assertNotIn("ya29.secret-token", as_text)
        self.assertNotIn("st-secret-value", as_text)
        self.assertNotIn("recaptcha-secret-value", as_text)
        self.assertNotIn("grecaptcha-secret-value", as_text)
        self.assertIn("truncated", sanitized["imageBytes"])


if __name__ == "__main__":
    unittest.main()
