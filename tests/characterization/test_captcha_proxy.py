"""Characterization: lock proxy URL parsing + proxy-auth extension content."""
import json
import os

from tests.conftest import assert_golden


def test_parse_proxy_url_golden():
    from src.services.captcha.proxy import _parse_proxy_url

    cases = {
        "bare_host_port": "127.0.0.1:7890",
        "http_with_auth": "http://user:pass@1.2.3.4:8080",
        "socks5": "socks5://10.0.0.1:1080",
        "socks5h_normalized": "socks5h://10.0.0.1:1080",
        "https_auth": "https://u:p@proxy.example.com:443",
        "empty": "",
        "garbage": "not a url",
    }
    out = {k: list(_parse_proxy_url(v)) for k, v in cases.items()}
    assert out["bare_host_port"] == ["http", "127.0.0.1", "7890", None, None]
    assert out["socks5h_normalized"][0] == "socks5"  # normalized
    assert out["empty"] == [None, None, None, None, None]
    assert_golden("captcha_parse_proxy", out)


def test_proxy_auth_extension_content_golden():
    from src.services.captcha.proxy import _create_proxy_auth_extension

    ext_dir = _create_proxy_auth_extension("http", "1.2.3.4", "8080", "alice", "secret")
    try:
        with open(os.path.join(ext_dir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        with open(os.path.join(ext_dir, "background.js"), encoding="utf-8") as f:
            background = f.read()
        assert manifest["manifest_version"] == 2
        assert 'host: "1.2.3.4"' in background
        assert 'username: "alice"' in background
        assert_golden("captcha_proxy_ext", {"manifest": manifest, "background_js": background})
    finally:
        for fn in ("manifest.json", "background.js"):
            try:
                os.remove(os.path.join(ext_dir, fn))
            except OSError:
                pass
        try:
            os.rmdir(ext_dir)
        except OSError:
            pass
