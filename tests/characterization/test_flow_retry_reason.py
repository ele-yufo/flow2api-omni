"""Characterization: lock retry-reason mapping (drives create_project/generation retries)."""
from tests.conftest import assert_golden


def test_get_retry_reason_golden():
    from src.services.flow.errors import get_retry_reason

    samples = [
        "HTTP 403 Forbidden", "429 Too Many Requests", "curl: (16) framing",
        "reCAPTCHA evaluation failed", "reCAPTCHA token missing",
        "HTTP Error 502 Bad Gateway", '{"reason":"internal"}', "service unavailable",
        "200 OK", "some random error",
        # 账号级配额耗尽：同账号重试无意义 → 不重试（上层摘除账号+换号）。
        # 2026-08-16 前这段会被 "public_error" 通配归为 "5xx/上游瞬断" 空转重试。
        "Flow API request failed: PUBLIC_ERROR_USER_QUOTA_REACHED: Resource has been exhausted (e.g. check quota).",
        "Resource has been exhausted (e.g. check quota).",
    ]
    out = {s: get_retry_reason(s) for s in samples}
    assert out["HTTP 403 Forbidden"] == "403错误"
    assert out["200 OK"] is None
    assert out["service unavailable"] == "5xx/上游瞬断"
    assert out["Flow API request failed: PUBLIC_ERROR_USER_QUOTA_REACHED: Resource has been exhausted (e.g. check quota)."] is None
    assert out["Resource has been exhausted (e.g. check quota)."] is None
    assert_golden("flow_retry_reason", out)


def test_is_user_quota_exhausted_error():
    """账号配额耗尽判定：正例必须命中（含真实上游文案），分钟级限流
    和普通 429 必须不命中（误摘除账号的代价太高）。"""
    from src.services.flow.errors import is_user_quota_exhausted_error

    positive = [
        "Flow API request failed: PUBLIC_ERROR_USER_QUOTA_REACHED: Resource has been exhausted (e.g. check quota).",
        "PUBLIC_ERROR_USER_QUOTA_REACHED",
    ]
    negative = [
        "429 Too Many Requests",
        "Quota exceeded for quota metric Read requests per minute",
        "HTTP Error 403 Forbidden",
        "some random error",
        "",
    ]
    for s in positive:
        assert is_user_quota_exhausted_error(s) is True, s
    for s in negative:
        assert is_user_quota_exhausted_error(s) is False, s


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
