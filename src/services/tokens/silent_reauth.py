"""Mint a fresh labs.google session from a Chrome profile — no browser, no human.

Since 2026-09-11 the next-auth session behind ``labs.google/fx`` can no longer refresh its
Google access token: ``/fx/api/auth/session`` answers ``ACCESS_TOKEN_REFRESH_NEEDED`` and
keeps handing back the already-expired token, so ``/credits`` returns 401 and every account
bans itself with ``GRANT_EXPIRED``. The browser keepalive cannot heal it either — the Flow
tool page it used to refresh now redirects to the first-party ``flow.google.com`` app, which
never touches next-auth.

The Google account cookies inside each keepalive profile stay valid, and they are enough to
replay the next-auth Google sign-in over plain HTTP: ``/auth/csrf`` → ``/auth/signin/google``
→ follow the OAuth redirect chain → the final ``Set-Cookie`` carries a brand new session
token. The account consented to this OAuth client long ago, so Google answers silently.

Nothing here logs or raises a cookie value; callers only ever receive the new ST.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from ...core.cookie_extractor import MIN_ST_LEN

LABS_API_BASE = "https://labs.google/fx/api"
CSRF_URL = f"{LABS_API_BASE}/auth/csrf"
SIGNIN_URL = f"{LABS_API_BASE}/auth/signin/google"
CALLBACK_URL = "https://labs.google/fx/tools/flow"
SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
SESSION_COOKIE_DOMAIN = "labs.google"
DEFAULT_TIMEOUT_SECONDS = 90.0
# curl_cffi 0.7.3 最高支持 chrome124，与 flow_client 的 impersonate 选择保持一致。
IMPERSONATE = "chrome124"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class SilentReauthError(RuntimeError):
    """Raised when the profile's Google cookies cannot mint a new session."""

    def __init__(self, message: str, *, code: str = "reauth_failed"):
        super().__init__(message)
        self.code = code


CookieJarReader = Callable[[Path], Any]
SessionFactory = Callable[..., Any]


def _default_cookie_jar_reader(profile_path: Path) -> Any:
    """Read the whole Chrome cookie jar (Google account cookies included)."""

    import browser_cookie3

    cookie_file = profile_path / "Default" / "Cookies"
    if not cookie_file.is_file():
        raise SilentReauthError(
            "profile has no Chrome cookie store", code="profile_missing"
        )
    return browser_cookie3.chrome(cookie_file=str(cookie_file))


def _default_session_factory(**kwargs) -> Any:
    from curl_cffi.requests import AsyncSession

    return AsyncSession(**kwargs)


def _extract_session_token(session: Any, minimum_length: int) -> str:
    """Pull the rotated next-auth cookie out of the sign-in session's jar."""

    candidates = []
    for cookie in getattr(session.cookies, "jar", ()):
        if getattr(cookie, "name", None) != SESSION_COOKIE_NAME:
            continue
        if SESSION_COOKIE_DOMAIN not in str(getattr(cookie, "domain", "") or ""):
            continue
        value = getattr(cookie, "value", None)
        if isinstance(value, str) and value:
            candidates.append(value)
    if not candidates:
        raise SilentReauthError(
            "sign-in completed without a next-auth session cookie",
            code="session_cookie_missing",
        )
    session_token = max(candidates, key=lambda value: len(value.encode("utf-8")))
    if len(session_token.encode("utf-8")) < minimum_length:
        raise SilentReauthError(
            f"minted session token is shorter than {minimum_length} bytes",
            code="session_cookie_short",
        )
    return session_token


async def silent_reauthorize(
    profile_path: os.PathLike[str] | str,
    *,
    proxy_url: Optional[str] = None,
    cookie_jar_reader: CookieJarReader = _default_cookie_jar_reader,
    session_factory: SessionFactory = _default_session_factory,
    minimum_length: int = MIN_ST_LEN,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Replay the next-auth Google sign-in and return a brand new session token."""

    canonical_profile = Path(profile_path).expanduser().resolve(strict=False)
    if not canonical_profile.is_dir():
        raise SilentReauthError("browser profile is missing", code="profile_missing")
    jar = cookie_jar_reader(canonical_profile)

    async with session_factory(
        cookies=jar,
        proxy=proxy_url,
        impersonate=IMPERSONATE,
        timeout=timeout_seconds,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
    ) as session:
        csrf_response = await session.get(CSRF_URL)
        try:
            csrf_token = csrf_response.json().get("csrfToken")
        except Exception as error:  # noqa: BLE001 - body shape is upstream's choice
            raise SilentReauthError(
                f"csrf endpoint returned no JSON object ({type(error).__name__})",
                code="csrf_failed",
            ) from error
        if not isinstance(csrf_token, str) or not csrf_token.strip():
            raise SilentReauthError("csrf endpoint returned no token", code="csrf_failed")

        signin_response = await session.post(
            SIGNIN_URL,
            data={
                "csrfToken": csrf_token,
                "callbackUrl": CALLBACK_URL,
                "json": "true",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )
        try:
            authorize_url = signin_response.json().get("url")
        except Exception as error:  # noqa: BLE001 - body shape is upstream's choice
            raise SilentReauthError(
                f"sign-in returned no JSON object ({type(error).__name__})",
                code="signin_failed",
            ) from error
        if not isinstance(authorize_url, str) or not authorize_url.startswith("https://"):
            raise SilentReauthError(
                "sign-in returned no authorize URL", code="signin_failed"
            )

        # Google answers the authorize request silently for an already-consented client and
        # walks the whole chain back to the next-auth callback, which sets the new cookie.
        await session.get(authorize_url, allow_redirects=True)
        return _extract_session_token(session, minimum_length)
