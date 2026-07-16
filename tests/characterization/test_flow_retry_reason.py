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


def test_is_captcha_rejection_reason_golden():
    from src.services.flow.errors import is_captcha_rejection_reason

    out = {
        "recaptcha": is_captcha_rejection_reason(error_message="reCAPTCHA evaluation failed"),
        "unusual_activity": is_captcha_rejection_reason(error_reason="PUBLIC_ERROR_UNUSUAL_ACTIVITY"),
        "unusual_space": is_captcha_rejection_reason(error_message="unusual activity detected"),
        "captcha": is_captcha_rejection_reason(error_message="captcha required"),
        "403_not_captcha": is_captcha_rejection_reason(error_message="403 Forbidden"),  # must be False
        "empty": is_captcha_rejection_reason(),
    }
    assert out["recaptcha"] is True and out["unusual_activity"] is True
    assert out["403_not_captcha"] is False  # 关键:403 不算 captcha rejection
    assert out["empty"] is False
    assert_golden("flow_captcha_rejection", out)
