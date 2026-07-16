"""Characterization: lock proxy-line parsing across all supported formats."""
from tests.conftest import assert_golden


def test_parse_proxy_line_golden():
    from src.shared.proxy_parse import parse_proxy_line

    cases = [
        "http://u:p@1.2.3.4:8080",
        "socks5://1.2.3.4:1080:user:pass",
        "st5 1.2.3.4:1080:user:pass",
        "1.2.3.4:8080",
        "1.2.3.4:8080:user:pass",
        "user:pass@host:9",
        "socks5h://u:p@h:1080",
        "",
        "garbage no colon",
    ]
    out = {c: parse_proxy_line(c) for c in cases}
    assert out["1.2.3.4:8080"] == "http://1.2.3.4:8080"
    assert out["socks5://1.2.3.4:1080:user:pass"] == "socks5://user:pass@1.2.3.4:1080"
    assert out["st5 1.2.3.4:1080:user:pass"] == "socks5://user:pass@1.2.3.4:1080"
    assert out["1.2.3.4:8080:user:pass"] == "http://user:pass@1.2.3.4:8080"
    assert out[""] is None
    assert_golden("proxy_parse", out)
