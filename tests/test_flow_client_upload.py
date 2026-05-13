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
