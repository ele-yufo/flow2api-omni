"""Characterization: lock OpenAI/Gemini payload-conversion helpers (contract-critical)."""
import base64
from tests.conftest import assert_golden


def test_protocol_conversion_golden():
    from src.api import routes as R

    img_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nDATA").decode()
    out = {
        "parse_json": R._parse_handler_result('{"k": 1}'),
        "parse_nonjson": R._parse_handler_result("plain text"),
        "status_error_int": R._get_error_status_code({"error": {"status_code": 403}}),
        "status_error_str": R._get_error_status_code({"error": {"status_code": "404"}}),
        "status_ok": R._get_error_status_code({"result": "x"}),
        "gemini_err": R._build_gemini_error_payload(400, "bad"),
        "gemini_err_unknown": R._build_gemini_error_payload(499, "weird"),
        "openai_content": R._extract_openai_message_content(
            {"choices": [{"message": {"content": "hello"}}]}),
        "openai_content_result": R._extract_openai_message_content({"result": "fallback"}),
        "url_direct": R._extract_url_from_openai_payload({"url": "http://direct"}),
        "url_markdown": R._extract_url_from_openai_payload(
            {"choices": [{"message": {"content": "![img](http://md.png)"}}]}),
        "url_video": R._extract_url_from_openai_payload(
            {"choices": [{"message": {"content": "<video src='http://v.mp4'>"}}]}),
        "finish_stop": R._normalize_finish_reason("stop"),
        "finish_length": R._normalize_finish_reason("length"),
        "finish_none": R._normalize_finish_reason(None),
        "guess_mime": R._guess_mime_type("http://x/a.png", "image/jpeg"),
        "decode_data_url_mime": R._decode_data_url(f"data:image/png;base64,{img_b64}")[0],
    }
    assert out["status_error_int"] == 403
    assert out["gemini_err"]["error"]["status"] == "INVALID_ARGUMENT"
    assert out["url_markdown"] == "http://md.png"
    assert out["finish_length"] == "MAX_TOKENS"
    assert out["decode_data_url_mime"] == "image/png"
    assert_golden("protocol_conversion", out)


def test_protocol_conversion_more_golden():
    from src.api import routes as R
    from src.core.models import GeminiContent

    gc = GeminiContent.model_validate({"role": "user", "parts": [{"text": "hi"}, {"text": "there"}]})
    out = {
        "model_desc_image": R._build_model_description({"type": "image", "model_name": "GEM_PIX"}),
        "model_desc_video": R._build_model_description({"type": "video", "model_key": "veo_x"}),
        "extract_text": R._extract_text_from_gemini_content(gc),
        "extract_text_none": R._extract_text_from_gemini_content(None),
        "video_parts": R._build_video_parts_from_uri("http://x/v.mp4"),
        "coerce_count": len(R._coerce_gemini_contents([{"role": "user", "parts": [{"text": "a"}]}])),
    }
    assert out["model_desc_image"] == "Image generation - GEM_PIX"
    assert out["extract_text"] == "hi\nthere"
    assert out["video_parts"][0]["fileData"]["mimeType"] == "video/mp4"
    assert_golden("protocol_conversion_more", out)
