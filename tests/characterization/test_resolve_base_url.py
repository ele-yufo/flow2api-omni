"""Characterization: lock base_url resolution precedence."""
from tests.conftest import assert_golden


def test_resolve_base_url_golden():
    from src.services.generation.state import resolve_base_url

    out = {
        "cache_wins": resolve_base_url("http://cdn.example.com/", "http://req", "1.2.3.4", 8000),
        "response_next": resolve_base_url("", "http://req.example.com/", "1.2.3.4", 8000),
        "server_fallback": resolve_base_url("", "", "5.6.7.8", 9000),
        "wildcard_host_to_localhost": resolve_base_url("", "", "0.0.0.0", 8000),
        "empty_host_to_localhost": resolve_base_url("", None, "", 8000),
    }
    assert out["cache_wins"] == "http://cdn.example.com"       # cache stripped, wins
    assert out["response_next"] == "http://req.example.com"
    assert out["server_fallback"] == "http://5.6.7.8:9000"
    assert out["wildcard_host_to_localhost"] == "http://127.0.0.1:8000"
    assert_golden("resolve_base_url", out)
