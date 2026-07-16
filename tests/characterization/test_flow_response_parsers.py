"""Characterization: lock Flow response/header parsers (rotated ST, projectId, json, urllib-fallback)."""
from tests.conftest import assert_golden


def test_response_parsers_golden():
    from src.services.flow.response_parsers import (
        extract_project_id_from_payload,
        extract_rotated_st_from_set_cookie,
        parse_json_response_text,
    )
    from src.services.flow.errors import should_fallback_to_urllib
    from src.core.cookie_extractor import SESSION_TOKEN_KEY, MIN_ST_LEN

    long_st = "x" * (MIN_ST_LEN + 5)
    short_st = "x" * 10
    out = {
        "project_id_nested": extract_project_id_from_payload({"a": {"b": [{"projectId": " p1 "}]}}),
        "project_id_missing": extract_project_id_from_payload({"a": 1}),
        "json_valid": parse_json_response_text('{"k": 1}'),
        "json_invalid": parse_json_response_text("not json"),
        "json_empty": parse_json_response_text(""),
        "rotated_st_ok": extract_rotated_st_from_set_cookie([f"{SESSION_TOKEN_KEY}={long_st}; Path=/"]),
        "rotated_st_too_short": extract_rotated_st_from_set_cookie([f"{SESSION_TOKEN_KEY}={short_st}"]),
        "rotated_st_none": extract_rotated_st_from_set_cookie(["other=cookie"]),
        "fallback_curl16": should_fallback_to_urllib("curl: (16) HTTP/2 framing"),
        "fallback_no": should_fallback_to_urllib("403 Forbidden"),
    }
    assert out["project_id_nested"] == "p1"  # stripped
    assert out["rotated_st_ok"] == long_st
    assert out["rotated_st_too_short"] is None  # length guard
    assert out["fallback_curl16"] is True
    assert out["fallback_no"] is False
    assert_golden("flow_response_parsers", out)


def test_extract_google_error_reason():
    from src.services.flow.response_parsers import extract_google_error_reason
    assert extract_google_error_reason(403, {"error": {"message": "denied",
        "details": [{"reason": "PERMISSION_DENIED"}]}}) == "PERMISSION_DENIED: denied"
    assert extract_google_error_reason(500, {"error": {"message": "boom"}}) == "HTTP Error 500: boom"
    assert extract_google_error_reason(404, None) == "HTTP Error 404"
    assert extract_google_error_reason(400, {"no_error": 1}) == "HTTP Error 400"
