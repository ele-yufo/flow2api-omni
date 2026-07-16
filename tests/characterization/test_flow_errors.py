"""Characterization: lock network-error retry classification (extracted P5).

The keyword tables drive fast-fail-retry; golden-lock them so a refactor can't
silently change which errors retry.
"""
from tests.conftest import assert_golden


def test_error_classification_golden():
    from src.services.flow.errors import is_retryable_network_error, is_timeout_error

    samples = [
        "curl: (28) Operation timed out after 120000ms",
        "curl: (16) HTTP/2 framing error",
        "curl: (35) SSL connect error",
        "SSL_ERROR_SYSCALL",
        "Connection reset by peer",
        "UNEXPECTED_EOF_WHILE_READING",
        "Empty reply from server",
        "http.client.IncompleteRead(0 bytes read)",
        "requests.exceptions.ChunkedEncodingError",
        "network is unreachable",
        "403 Forbidden PUBLIC_ERROR_UNUSUAL_ACTIVITY",  # NOT retryable
        "200 OK",                                        # NOT retryable
        "",                                              # empty
    ]
    out = {
        s: {"timeout": is_timeout_error(s), "retryable": is_retryable_network_error(s)}
        for s in samples
    }
    # sanity: policy/non-network errors must not be classified retryable
    assert out["200 OK"] == {"timeout": False, "retryable": False}
    assert out["403 Forbidden PUBLIC_ERROR_UNUSUAL_ACTIVITY"]["retryable"] is False
    assert_golden("flow_errors", out)


def test_flowclient_delegation():
    from src.services.flow_client import FlowClient
    from src.services.flow import errors as E

    fc = FlowClient(None)
    for s in ["curl: (28) timed out", "curl: (16) framing", "200 OK"]:
        assert fc._is_timeout_error(s) == E.is_timeout_error(s)
        assert fc._is_retryable_network_error(s) == E.is_retryable_network_error(s)
