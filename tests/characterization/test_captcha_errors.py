"""Characterization: lock nodriver runtime error classification (extracted from browser_captcha)."""
from tests.conftest import assert_golden


def _chain(msg, cause=None):
    e = RuntimeError(msg)
    if cause:
        e.__cause__ = cause
    return e


def test_runtime_error_classification_golden():
    from src.services.captcha.errors import (
        _is_runtime_disconnect_error,
        _is_runtime_normal_close_error,
        _flatten_exception_text,
    )

    samples = {
        "target_closed": RuntimeError("Target closed"),
        "conn_reset": RuntimeError("Connection reset by peer"),
        "ws_not_open": RuntimeError("websocket is not open"),
        "errno111": RuntimeError("[Errno 111] Connection refused"),
        "normal_1000": RuntimeError("sent 1000 (OK)"),
        "connclosedok": RuntimeError("ConnectionClosedOK"),
        "unrelated": RuntimeError("some unrelated failure"),
        "empty": RuntimeError(""),
        "chained": _chain("wrapper", cause=RuntimeError("browser has been closed")),
    }
    out = {
        k: {
            "disconnect": _is_runtime_disconnect_error(e),
            "normal_close": _is_runtime_normal_close_error(e),
        }
        for k, e in samples.items()
    }
    # sanity: normal-close implies disconnect (superset); unrelated is neither
    assert out["normal_1000"] == {"disconnect": True, "normal_close": True}
    assert out["unrelated"] == {"disconnect": False, "normal_close": False}
    assert out["chained"]["disconnect"] is True  # walks __cause__
    assert "browser has been closed" in _flatten_exception_text(samples["chained"])
    assert_golden("captcha_errors", out)


def test_is_server_side_flow_error_golden():
    from src.services.captcha.errors import is_server_side_flow_error

    out = {
        "http500": is_server_side_flow_error("HTTP Error 500"),
        "internal": is_server_side_flow_error('{"reason":"internal"}'),
        "public_error": is_server_side_flow_error("PUBLIC_ERROR_X"),
        "server_error": is_server_side_flow_error("server error"),
        "not_server": is_server_side_flow_error("403 Forbidden"),
        "empty": is_server_side_flow_error(""),
    }
    assert out["http500"] is True and out["not_server"] is False
    assert_golden("captcha_server_error", out)
