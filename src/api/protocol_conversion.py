"""OpenAI <-> Gemini payload conversion helpers + protocol constants (pure).

Extracted from api/routes.py. Pure payload/URL/error/mime transforms — no request/DB.
Locked by tests/characterization/test_protocol_conversion.py.
"""
import base64
import json
import mimetypes
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

from ..core.models import GeminiContent


MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
HTML_VIDEO_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)
DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)
GEMINI_STATUS_MAP = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ABORTED",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    match = DATA_URL_RE.match(data_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid data URL")
    return match.group("mime"), base64.b64decode(match.group("data"))


def _guess_mime_type(uri: str, fallback: str) -> str:
    guessed, _ = mimetypes.guess_type(urlparse(uri).path)
    return guessed or fallback


def _parse_handler_result(result: str) -> Dict[str, Any]:
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return {"result": result}


def _get_error_status_code(payload: Dict[str, Any]) -> int:
    error = payload.get("error")
    if isinstance(error, dict):
        status_code = error.get("status_code")
        if isinstance(status_code, int):
            return status_code
        if isinstance(status_code, str) and status_code.isdigit():
            return int(status_code)
        return 400
    return 200


def _build_gemini_error_payload(status_code: int, message: str) -> Dict[str, Any]:
    return {
        "error": {
            "code": status_code,
            "message": message,
            "status": GEMINI_STATUS_MAP.get(status_code, "UNKNOWN"),
        }
    }


def _extract_openai_message_content(payload: Dict[str, Any]) -> str:
    choices = payload.get("choices", [])
    if not choices:
        return payload.get("result", "")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    return content if isinstance(content, str) else ""


def _extract_url_from_openai_payload(payload: Dict[str, Any]) -> Optional[str]:
    direct_url = payload.get("url")
    if isinstance(direct_url, str) and direct_url.strip():
        return direct_url.strip()

    content = _extract_openai_message_content(payload).strip()
    if not content:
        return None

    image_match = MARKDOWN_IMAGE_RE.search(content)
    if image_match:
        return image_match.group(1).strip()

    video_match = HTML_VIDEO_RE.search(content)
    if video_match:
        return video_match.group(1).strip()

    return None


def _enrich_payload_with_direct_url(payload: Dict[str, Any]) -> Dict[str, Any]:
    extracted_url = _extract_url_from_openai_payload(payload)
    if extracted_url and not payload.get("url"):
        payload["url"] = extracted_url
    return payload


def _normalize_finish_reason(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    mapping = {
        "stop": "STOP",
        "length": "MAX_TOKENS",
        "content_filter": "SAFETY",
    }
    return mapping.get(reason, "STOP")


def _build_model_description(model_config: Dict[str, Any]) -> str:
    """Build a human-readable description for model listing endpoints."""
    description = f"{model_config['type'].capitalize()} generation"
    if model_config["type"] == "image":
        description += f" - {model_config['model_name']}"
    else:
        description += f" - {model_config['model_key']}"
    return description


def _coerce_gemini_contents(raw_contents: Optional[List[Any]]) -> List[GeminiContent]:
    contents: List[GeminiContent] = []
    for item in raw_contents or []:
        if isinstance(item, GeminiContent):
            contents.append(item)
        else:
            contents.append(GeminiContent.model_validate(item))
    return contents


def _extract_text_from_gemini_content(content: Optional[GeminiContent]) -> str:
    if content is None:
        return ""
    text_parts = [part.text.strip() for part in content.parts if part.text]
    return "\n".join(part for part in text_parts if part).strip()


def _build_video_parts_from_uri(uri: str) -> List[Dict[str, Any]]:
    return [
        {
            "fileData": {
                "mimeType": _guess_mime_type(uri, "video/mp4"),
                "fileUri": uri,
            }
        }
    ]
