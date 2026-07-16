"""HTTP header / User-Agent construction for Flow requests.

Extracted from FlowClient (P5). Holds per-account UA cache + static browser headers
+ client-hints fallback. UA/client-hint headers are built per request so they don't
contradict the browser that solved reCAPTCHA. No network, no config — unit-testable.
"""
import hashlib
import random
from typing import Any, Dict, Optional


class HeaderBuilder:
    """Builds request headers with a coherent (non-contradictory) browser fingerprint."""

    def __init__(self):
        # 缓存每个账号的 User-Agent
        self._user_agent_cache: Dict[str, str] = {}

        # Site-level browser headers. UA/client-hint headers are built per request
        # so they do not contradict the browser that solved reCAPTCHA.
        self._default_client_headers = {
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "origin": "https://labs.google",
            "referer": "https://labs.google/",
            "x-browser-channel": "stable",
            "x-browser-copyright": "Copyright 2026 Google LLC. All Rights reserved.",
            "x-browser-validation": "UujAs0GAwdnCJ9nvrswZ+O+oco0=",
            "x-browser-year": "2026",
            "x-client-data": "CJS2yQEIpLbJAQipncoBCNj9ygEIlKHLAQiFoM0BGP6lzwE="
        }
        self._fallback_chromium_client_hints = {
            "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not_A Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
        }

    def generate_user_agent(self, account_id: str = None) -> str:
        """基于账号ID生成固定的 User-Agent

        Args:
            account_id: 账号标识（如 email 或 token_id），相同账号返回相同 UA

        Returns:
            User-Agent 字符串
        """
        # 如果没有提供账号ID，生成随机UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"

        # 如果已缓存，直接返回
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]

        # 使用账号ID作为随机种子，确保同一账号生成相同的UA
        seed = int(hashlib.md5(account_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)

        # Fallback 必须和底层 curl_cffi Chrome impersonation 保持同一浏览器族。
        # 有头浏览器成功取到 token 时会优先使用真实指纹，这里只处理缺失指纹的兜底。
        # 必须与 _select_impersonate_for_headers 的 JA3 版本一致(chrome124),
        # 否则 UA 报新版、TLS 指纹却是另一版,Google 风控会判为自动化。
        chrome_versions = ["124.0.0.0"]

        os_configs = [
            "Windows NT 10.0; Win64; x64",
            "Macintosh; Intel Mac OS X 10_15_7",
            "X11; Linux x86_64",
        ]

        platform = rng.choice(os_configs)
        user_agent = (
            f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{rng.choice(chrome_versions)} Safari/537.36"
        )

        # 缓存结果
        self._user_agent_cache[account_id] = user_agent

        return user_agent

    def build_request_headers(
        self,
        headers: Optional[Dict] = None,
        st_token: Optional[str] = None,
        at_token: Optional[str] = None,
        use_st: bool = False,
        use_at: bool = False,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build request headers without mixing incompatible browser fingerprints."""
        request_headers = dict(headers or {})

        if use_st and st_token:
            request_headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"
        if use_at and at_token:
            request_headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint = fingerprint if isinstance(fingerprint, dict) else None
        fingerprint_user_agent = fingerprint.get("user_agent") if fingerprint else None

        request_headers["Content-Type"] = "application/json"
        request_headers["User-Agent"] = fingerprint_user_agent or self.generate_user_agent(account_id)

        if fingerprint:
            if fingerprint.get("accept_language"):
                request_headers.setdefault("Accept-Language", fingerprint["accept_language"])
            for fp_key, header_key in (
                ("sec_ch_ua", "sec-ch-ua"),
                ("sec_ch_ua_mobile", "sec-ch-ua-mobile"),
                ("sec_ch_ua_platform", "sec-ch-ua-platform"),
            ):
                if fingerprint.get(fp_key):
                    request_headers[header_key] = fingerprint[fp_key]
        else:
            for key, value in self._fallback_chromium_client_hints.items():
                request_headers.setdefault(key, value)

        for key, value in self._default_client_headers.items():
            request_headers.setdefault(key, value)

        return request_headers

    @staticmethod
    def get_user_agent_family(user_agent: str) -> str:
        ua = (user_agent or "").lower()
        if "edg/" in ua:
            return "edge"
        if "chrome/" in ua or "chromium/" in ua:
            return "chrome"
        if "firefox/" in ua:
            return "firefox"
        if "safari/" in ua:
            return "safari"
        return "unknown"
