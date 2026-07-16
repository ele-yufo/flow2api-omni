"""Proxy URL parsing + Chrome proxy-auth extension generation (extracted, pure-ish).

_parse_proxy_url is pure. _create_proxy_auth_extension writes a temp extension dir but
its generated content is deterministic given inputs (testable by reading the files back).
"""
import json
import os
import re
import tempfile


def _parse_proxy_url(proxy_url: str):
    """Parse a proxy URL into (protocol, host, port, username, password)."""
    if not proxy_url:
        return None, None, None, None, None
    url = proxy_url.strip()
    if not re.match(r'^(http|https|socks5h?|socks5)://', url):
        url = f"http://{url}"
    m = re.match(r'^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$', url)
    if not m:
        return None, None, None, None, None
    protocol, username, password, host, port = m.groups()
    if protocol == "socks5h":
        protocol = "socks5"
    return protocol, host, port, username, password


def _create_proxy_auth_extension(protocol: str, host: str, port: str, username: str, password: str) -> str:
    """Create a temporary Chrome extension directory for proxy authentication.
    Returns the path to the extension directory."""
    ext_dir = tempfile.mkdtemp(prefix="nodriver_proxy_auth_")

    scheme_map = {"http": "http", "https": "https", "socks5": "socks5"}
    scheme = scheme_map.get(protocol, "http")

    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth Helper",
        "permissions": [
            "proxy", "tabs", "unlimitedStorage", "storage",
            "<all_urls>", "webRequest", "webRequestBlocking"
        ],
        "background": {"scripts": ["background.js"]},
        "minimum_chrome_version": "76.0.0"
    }
    background_js = (
        "var config = {\n"
        '    mode: "fixed_servers",\n'
        "    rules: {\n"
        "        singleProxy: {\n"
        f'            scheme: "{scheme}",\n'
        f'            host: "{host}",\n'
        f"            port: parseInt({port})\n"
        "        },\n"
        '        bypassList: ["localhost"]\n'
        "    }\n"
        "};\n"
        'chrome.proxy.settings.set({value: config, scope: "regular"}, function(){});\n'
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "    function(details) {\n"
        "        return {\n"
        "            authCredentials: {\n"
        f'                username: "{username}",\n'
        f'                password: "{password}"\n'
        "            }\n"
        "        };\n"
        "    },\n"
        '    {urls: ["<all_urls>"]},\n'
        "    ['blocking']\n"
        ");\n"
    )
    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(background_js)
    return ext_dir
