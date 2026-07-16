"""Pure request-body builders for Flow generation actions.

Extracted from FlowClient's action methods (deep P5 split). Builders are pure: all
non-deterministic inputs (seed, scene_id, session_id, batch_id, recaptcha_token) are
passed in by the caller, so the request contract is deterministic + unit-testable.
Locked by tests/characterization/test_flow_video_request.py (mock-characterization).
"""
from typing import Any, Dict, Optional


def build_video_text_input(prompt: str, use_v2_model_config: bool = False) -> Dict[str, Any]:
    if use_v2_model_config:
        return {
            "structuredPrompt": {
                "parts": [{
                    "text": prompt
                }]
            }
        }
    return {
        "prompt": prompt
    }


def build_video_text_request(
    *,
    recaptcha_token: str,
    session_id: str,
    project_id: str,
    user_paygate_tier: str,
    aspect_ratio: str,
    seed: int,
    text_input: Dict[str, Any],
    model_key: str,
    scene_id: str,
    use_v2_model_config: bool,
    batch_id: Optional[str],
) -> Dict[str, Any]:
    """Assemble the batchAsyncGenerateVideoText request body (deterministic given inputs)."""
    client_context = {
        "recaptchaContext": {
            "token": recaptcha_token,
            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
        },
        "sessionId": session_id,
        "projectId": project_id,
        "tool": "PINHOLE",
        "userPaygateTier": user_paygate_tier
    }
    request_data = {
        "aspectRatio": aspect_ratio,
        "seed": seed,
        "textInput": text_input,
        "videoModelKey": model_key,
        "metadata": {
            "sceneId": scene_id
        }
    }
    json_data: Dict[str, Any] = {
        "clientContext": client_context,
        "requests": [request_data]
    }
    if use_v2_model_config:
        json_data["mediaGenerationContext"] = {
            "batchId": batch_id,
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS"
        }
        json_data["useV2ModelConfig"] = True

    return json_data
