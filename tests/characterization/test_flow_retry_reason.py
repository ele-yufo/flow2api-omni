"""Characterization: lock retry-reason mapping (drives create_project/generation retries)."""
from tests.conftest import assert_golden


def test_get_retry_reason_golden():
    from src.services.flow.errors import get_retry_reason

    samples = [
        "HTTP 403 Forbidden", "429 Too Many Requests", "curl: (16) framing",
        "reCAPTCHA evaluation failed", "reCAPTCHA token missing",
        "HTTP Error 502 Bad Gateway", '{"reason":"internal"}', "service unavailable",
        "200 OK", "some random error",
    ]
    out = {s: get_retry_reason(s) for s in samples}
    assert out["HTTP 403 Forbidden"] == "403错误"
    assert out["200 OK"] is None
    assert out["service unavailable"] == "5xx/上游瞬断"
    assert_golden("flow_retry_reason", out)
