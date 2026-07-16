"""nodriver runtime error classification (pure).

Extracted from browser_captcha_personal. Walks exception chains and matches keyword
tables to distinguish "browser/websocket disconnect" from "normal 1000 close" — used to
decide crash recovery vs. expected teardown. Unit-testable without a live browser.
"""
from typing import Any


_RUNTIME_ERROR_KEYWORDS = (
    "has been closed",
    "browser has been closed",
    "target closed",
    "connection closed",
    "connection lost",
    "connection refused",
    "connection reset",
    "broken pipe",
    "session closed",
    "not attached to an active page",
    "no session with given id",
    "cannot find context with specified id",
    "websocket is not open",
    "no close frame received or sent",
    "cannot call write to closing transport",
    "cannot write to closing transport",
    "cannot call send once a close message has been sent",
    "connectionclosederror",
    "connectionrefusederror",
    "disconnected",
    "errno 111",
)

_NORMAL_CLOSE_KEYWORDS = (
    "connectionclosedok",
    "normal closure",
    "normal_closure",
    "sent 1000 (ok)",
    "received 1000 (ok)",
    "close(code=1000",
)


def _flatten_exception_text(error: Any) -> str:
    """拼接异常链文本，便于统一识别 nodriver 运行态断连。"""
    visited: set[int] = set()
    pending = [error]
    parts: list[str] = []

    while pending:
        current = pending.pop()
        if current is None:
            continue

        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)

        parts.append(type(current).__name__)

        message = str(current or "").strip()
        if message:
            parts.append(message)

        args = getattr(current, "args", None)
        if isinstance(args, tuple):
            for arg in args:
                arg_text = str(arg or "").strip()
                if arg_text:
                    parts.append(arg_text)

        pending.append(getattr(current, "__cause__", None))
        pending.append(getattr(current, "__context__", None))

    return " | ".join(parts).lower()


def _is_runtime_disconnect_error(error: Any) -> bool:
    """识别浏览器 / websocket 运行态断连。"""
    error_text = _flatten_exception_text(error)
    if not error_text:
        return False
    return any(keyword in error_text for keyword in _RUNTIME_ERROR_KEYWORDS) or any(
        keyword in error_text for keyword in _NORMAL_CLOSE_KEYWORDS
    )


def _is_runtime_normal_close_error(error: Any) -> bool:
    """识别 websocket 正常关闭（1000）这类预期退场。"""
    error_text = _flatten_exception_text(error)
    if not error_text:
        return False
    return any(keyword in error_text for keyword in _NORMAL_CLOSE_KEYWORDS)
