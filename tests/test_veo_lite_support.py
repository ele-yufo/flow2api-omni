import types
import unittest
from unittest.mock import AsyncMock

from src.core.model_resolver import resolve_model_name
from src.services.flow_client import FlowClient
from src.services.generation_handler import MODEL_CONFIG, GenerationHandler


class VeoLiteModelResolverTests(unittest.TestCase):
    def test_resolve_t2v_lite_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "veo_3_1_t2v_lite",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "veo_3_1_t2v_lite_portrait")


class VeoLiteGenerationHandlerTests(unittest.TestCase):
    def test_tier_two_does_not_upgrade_lite_model_to_fake_ultra(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v_lite",
                "allow_tier_upgrade": False,
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v_lite")
        self.assertIsNone(message)

    def test_tier_two_still_upgrades_regular_model(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            {
                "model_key": "veo_3_1_t2v_fast",
            },
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "veo_3_1_t2v_fast_ultra")
        self.assertIn("ultra", message)


class VideoResponseNormalizationTests(unittest.TestCase):
    def test_old_operations_response_keeps_operation_polling(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        refs = handler._normalize_video_submit_response(
            {
                "operations": [
                    {
                        "operation": {"name": "operation-id"},
                        "sceneId": "scene-id",
                        "status": "MEDIA_GENERATION_STATUS_ACTIVE",
                    }
                ],
                "workflows": [{"name": "workflow-id"}],
            },
            project_id="project-id",
        )

        self.assertEqual(refs["operations"][0]["operation"]["name"], "operation-id")
        self.assertEqual(refs["media"], [])
        self.assertEqual(refs["workflow_id"], "workflow-id")
        self.assertEqual(refs["task_id"], "operation-id")

    def test_new_media_response_uses_media_polling_not_fake_operation(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        refs = handler._normalize_video_submit_response(
            {
                "workflows": [{"name": "workflow-id"}],
                "media": [
                    {
                        "name": "media-id",
                        "projectId": "project-id",
                        "workflowId": "workflow-id",
                        "sceneId": "scene-id",
                    }
                ],
            },
            project_id="fallback-project-id",
        )

        self.assertEqual(refs["operations"], [])
        self.assertEqual(
            refs["media"],
            [{"name": "media-id", "projectId": "project-id"}],
        )
        self.assertEqual(refs["workflow_id"], "workflow-id")
        self.assertEqual(refs["scene_id"], "scene-id")
        self.assertEqual(refs["task_id"], "media-id")


class VeoLiteFlowClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FlowClient(proxy_manager=None)
        self.client._acquire_video_launch_gate = AsyncMock(return_value=(True, None, None))
        self.client._release_video_launch_gate = AsyncMock()
        self.client._get_recaptcha_token = AsyncMock(return_value=("recaptcha-token", "browser-1"))
        self.client._notify_browser_captcha_request_finished = AsyncMock()

    async def test_generate_video_text_uses_v2_payload_for_lite(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token):
            captured["url"] = url
            captured["json_data"] = json_data
            return {"operations": [{"operation": {"name": "task-1"}}]}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.generate_video_text(
            at="at-token",
            project_id="project-1",
            prompt="猫猫",
            model_key="veo_3_1_t2v_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            use_v2_model_config=True,
        )

        json_data = captured["json_data"]
        request_data = json_data["requests"][0]
        self.assertTrue(json_data["useV2ModelConfig"])
        self.assertIn("batchId", json_data["mediaGenerationContext"])
        self.assertEqual(
            request_data["textInput"]["structuredPrompt"]["parts"][0]["text"],
            "猫猫",
        )
        self.assertNotIn("prompt", request_data["textInput"])
        self.assertEqual(request_data["videoModelKey"], "veo_3_1_t2v_lite")

    async def test_generate_video_start_end_uses_v2_payload_for_interpolation_lite(self):
        captured = {}

        async def fake_make_request(method, url, json_data, use_at, at_token):
            captured["url"] = url
            captured["json_data"] = json_data
            return {"operations": [{"operation": {"name": "task-2"}}]}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.generate_video_start_end(
            at="at-token",
            project_id="project-1",
            prompt="变身猫猫",
            model_key="veo_3_1_interpolation_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            start_media_id="start-media",
            end_media_id="end-media",
            use_v2_model_config=True,
        )

        json_data = captured["json_data"]
        request_data = json_data["requests"][0]
        self.assertTrue(json_data["useV2ModelConfig"])
        self.assertIn("batchId", json_data["mediaGenerationContext"])
        self.assertEqual(request_data["videoModelKey"], "veo_3_1_interpolation_lite")
        self.assertEqual(request_data["startImage"]["mediaId"], "start-media")
        self.assertEqual(request_data["endImage"]["mediaId"], "end-media")
        self.assertEqual(
            request_data["textInput"]["structuredPrompt"]["parts"][0]["text"],
            "变身猫猫",
        )

    async def test_media_status_refs_are_sent_as_media_payload(self):
        captured = {}

        async def fake_make_request(**kwargs):
            captured["json_data"] = kwargs["json_data"]
            return {"media": []}

        self.client._make_request = AsyncMock(side_effect=fake_make_request)

        await self.client.check_video_status(
            at="at-token",
            operations={
                "operations": [],
                "media": [{"name": "media-id", "projectId": "project-id"}],
            },
        )

        self.assertEqual(
            captured["json_data"],
            {"media": [{"name": "media-id", "projectId": "project-id"}]},
        )


if __name__ == "__main__":
    unittest.main()
