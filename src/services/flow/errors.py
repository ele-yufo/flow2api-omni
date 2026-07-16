"""Network error classification for Flow request retries (pure).

Extracted from FlowClient (P5). These keyword tables decide fast-fail-retry behavior
on TLS/connection/timeout errors — golden-locked so a refactor can't silently drop a
keyword and change retry semantics.
"""


def is_timeout_error(error) -> bool:
    """判断是否为网络超时，便于快速失败重试。"""
    error_lower = str(error).lower()
    return any(keyword in error_lower for keyword in [
        "timed out",
        "timeout",
        "curl: (28)",
        "connection timed out",
        "operation timed out",
    ])


def is_retryable_network_error(error_str: str) -> bool:
    """识别可重试的 TLS/连接类网络错误。"""
    error_lower = (error_str or "").lower()
    return any(keyword in error_lower for keyword in [
        "curl: (16)",   # HTTP/2 framing error (curl_cffi 大 body bug，需要重试)
        "curl: (35)",
        "curl: (52)",
        "curl: (56)",
        "http/2 framing",
        "ssl_error_syscall",
        "tls connect error",
        "ssl connect error",
        "connection reset",
        "connection aborted",
        "connection was reset",
        "unexpected eof",
        "unexpected_eof",       # ssl._ssl.SSLError "UNEXPECTED_EOF_WHILE_READING"
        "empty reply from server",
        "recv failure",
        "send failure",
        "connection refused",
        "network is unreachable",
        "remote host closed connection",
        # urllib / http.client 措辞（force_urllib 路径上的网络抖动）
        "remote end closed connection",
        "incompleteread",
        "badstatusline",
        "chunkedencodingerror",
    ])
