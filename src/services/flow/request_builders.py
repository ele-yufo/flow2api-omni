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


def build_video_reference_images_request(
    *,
    recaptcha_token: str,
    session_id: str,
    project_id: str,
    user_paygate_tier: str,
    aspect_ratio: str,
    seed: int,
    prompt: str,
    model_key: str,
    reference_images: list,
    scene_id: str,
    batch_id: str,
) -> Dict[str, Any]:
    """Assemble the batchAsyncGenerateVideoReferenceImages (R2V) request body."""
    return {
        "mediaGenerationContext": {
            "batchId": batch_id
        },
        "clientContext": {
            "recaptchaContext": {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
            },
            "sessionId": session_id,
            "projectId": project_id,
            "tool": "PINHOLE",
            "userPaygateTier": user_paygate_tier
        },
        "requests": [{
            "aspectRatio": aspect_ratio,
            "seed": seed,
            "textInput": {
                "structuredPrompt": {
                    "parts": [{
                        "text": prompt
                    }]
                }
            },
            "videoModelKey": model_key,
            "referenceImages": reference_images,
            "metadata": {
                "sceneId": scene_id
            }
        }],
        "useV2ModelConfig": True
    }


def build_video_image_request(
    *,
    recaptcha_token: str,
    session_id: str,
    project_id: str,
    user_paygate_tier: str,
    aspect_ratio: str,
    seed: int,
    text_input: Dict[str, Any],
    model_key: str,
    start_media_id: str,
    scene_id: str,
    use_v2_model_config: bool,
    batch_id: Optional[str],
    end_media_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble I2V request body — start-image (end_media_id=None) or start+end frames.

    Covers batchAsyncGenerateVideoStartImage and ...StartAndEndImage. mediaGenerationContext
    here carries only batchId (no audioFailurePreference, unlike text-to-video).
    """
    request_data: Dict[str, Any] = {
        "aspectRatio": aspect_ratio,
        "seed": seed,
        "textInput": text_input,
        "videoModelKey": model_key,
        "startImage": {
            "mediaId": start_media_id
        },
        "metadata": {
            "sceneId": scene_id
        }
    }
    if end_media_id is not None:
        request_data["endImage"] = {
            "mediaId": end_media_id
        }

    json_data: Dict[str, Any] = {
        "clientContext": {
            "recaptchaContext": {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
            },
            "sessionId": session_id,
            "projectId": project_id,
            "tool": "PINHOLE",
            "userPaygateTier": user_paygate_tier
        },
        "requests": [request_data]
    }
    if use_v2_model_config:
        json_data["mediaGenerationContext"] = {
            "batchId": batch_id
        }
        json_data["useV2ModelConfig"] = True

    return json_data


def build_image_request(
    *,
    recaptcha_token: str,
    session_id: str,
    project_id: str,
    seed: int,
    model_name: str,
    aspect_ratio: str,
    prompt: str,
    image_inputs: Optional[list],
    batch_id: str,
) -> Dict[str, Any]:
    """Assemble the flowMedia:batchGenerateImages request body.

    clientContext appears both top-level and inside the single request (matching the
    upstream new-media image API). imageInputs is [] for text-to-image.
    """
    client_context = {
        "recaptchaContext": {
            "token": recaptcha_token,
            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
        },
        "sessionId": session_id,
        "projectId": project_id,
        "tool": "PINHOLE"
    }
    request_data = {
        "clientContext": client_context,
        "seed": seed,
        "imageModelName": model_name,
        "imageAspectRatio": aspect_ratio,
        "structuredPrompt": {
            "parts": [{
                "text": prompt
            }]
        },
        "imageInputs": image_inputs or []
    }
    return {
        "clientContext": client_context,
        "mediaGenerationContext": {
            "batchId": batch_id
        },
        "useNewMedia": True,
        "requests": [request_data]
    }
