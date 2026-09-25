"""Headless next-auth re-authorization tests with no real cookies or network."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.tokens.silent_reauth import (
    SilentReauthError,
    silent_reauthorize,
)


NEW_ST = "eyJ" + "n" * 1200
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth?client_id=stub"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def make_cookie(value: str, *, name: str, domain: str):
    return SimpleNamespace(name=name, value=value, domain=domain)


class FakeSession:
    """Minimal stand-in for curl_cffi's AsyncSession used by the sign-in replay."""

    def __init__(self, *, csrf, signin, cookies, init_kwargs, calls):
        self._csrf = csrf
        self._signin = signin
        self.cookies = SimpleNamespace(jar=cookies)
        self.init_kwargs = init_kwargs
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if "auth/csrf" in url:
            return FakeResponse(self._csrf)
        return FakeResponse({})

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return FakeResponse(self._signin)


def make_factory(
    *,
    csrf=None,
    signin=None,
    cookies=None,
):
    calls: list = []
    sessions: list[FakeSession] = []

    def factory(**init_kwargs):
        session = FakeSession(
            csrf=csrf if csrf is not None else {"csrfToken": "csrf-value"},
            signin=signin if signin is not None else {"url": AUTHORIZE_URL},
            cookies=cookies
            if cookies is not None
            else [
                make_cookie(
                    NEW_ST,
                    name="__Secure-next-auth.session-token",
                    domain="labs.google",
                )
            ],
            init_kwargs=init_kwargs,
            calls=calls,
        )
        sessions.append(session)
        return session

    return factory, calls, sessions


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    path = tmp_path / "profile"
    path.mkdir()
    return path


@pytest.mark.asyncio
async def test_replays_signin_and_returns_the_minted_session_token(profile):
    factory, calls, sessions = make_factory()
    jar = object()

    session_token = await silent_reauthorize(
        profile,
        proxy_url="http://127.0.0.1:7890",
        cookie_jar_reader=lambda _: jar,
        session_factory=factory,
    )

    assert session_token == NEW_ST
    assert sessions[0].init_kwargs["cookies"] is jar
    assert sessions[0].init_kwargs["proxy"] == "http://127.0.0.1:7890"
    assert [call[0] for call in calls] == ["get", "post", "get"]
    assert calls[0][1].endswith("/auth/csrf")
    assert calls[1][1].endswith("/auth/signin/google")
    assert calls[1][2]["data"]["csrfToken"] == "csrf-value"
    assert calls[1][2]["allow_redirects"] is False
    # The authorize chain must be followed, or Google never reaches the callback that
    # sets the new session cookie.
    assert calls[2][1] == AUTHORIZE_URL
    assert calls[2][2]["allow_redirects"] is True


@pytest.mark.asyncio
async def test_missing_profile_directory_is_reported_before_any_request(tmp_path):
    factory, calls, _ = make_factory()

    with pytest.raises(SilentReauthError) as error:
        await silent_reauthorize(
            tmp_path / "absent",
            cookie_jar_reader=lambda _: object(),
            session_factory=factory,
        )

    assert error.value.code == "profile_missing"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "csrf, expected_code",
    [
        ({"csrfToken": ""}, "csrf_failed"),
        ({}, "csrf_failed"),
        (ValueError("not json"), "csrf_failed"),
    ],
)
async def test_unusable_csrf_response_stops_before_sign_in(profile, csrf, expected_code):
    factory, calls, _ = make_factory(csrf=csrf)

    with pytest.raises(SilentReauthError) as error:
        await silent_reauthorize(
            profile,
            cookie_jar_reader=lambda _: object(),
            session_factory=factory,
        )

    assert error.value.code == expected_code
    assert [call[0] for call in calls] == ["get"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signin",
    [{}, {"url": None}, {"url": "/relative/path"}, ValueError("not json")],
)
async def test_sign_in_without_an_authorize_url_is_rejected(profile, signin):
    factory, calls, _ = make_factory(signin=signin)

    with pytest.raises(SilentReauthError) as error:
        await silent_reauthorize(
            profile,
            cookie_jar_reader=lambda _: object(),
            session_factory=factory,
        )

    assert error.value.code == "signin_failed"
    assert [call[0] for call in calls] == ["get", "post"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cookies, expected_code",
    [
        ([], "session_cookie_missing"),
        (
            [
                make_cookie(
                    NEW_ST, name="__Secure-next-auth.session-token", domain="other.test"
                )
            ],
            "session_cookie_missing",
        ),
        (
            [make_cookie(NEW_ST, name="__Host-next-auth.csrf-token", domain="labs.google")],
            "session_cookie_missing",
        ),
        (
            [
                make_cookie(
                    "short", name="__Secure-next-auth.session-token", domain="labs.google"
                )
            ],
            "session_cookie_short",
        ),
    ],
)
async def test_missing_or_short_session_cookie_never_leaks_its_value(
    profile, cookies, expected_code
):
    factory, _, _ = make_factory(cookies=cookies)

    with pytest.raises(SilentReauthError) as error:
        await silent_reauthorize(
            profile,
            cookie_jar_reader=lambda _: object(),
            session_factory=factory,
        )

    assert error.value.code == expected_code
    assert "short" not in str(error.value).replace("shorter", "")
    assert NEW_ST not in str(error.value)


@pytest.mark.asyncio
async def test_longest_matching_cookie_wins_over_a_stale_chunk(profile):
    stale = "eyJ" + "s" * 200
    factory, _, _ = make_factory(
        cookies=[
            make_cookie(
                stale, name="__Secure-next-auth.session-token", domain="labs.google"
            ),
            make_cookie(
                NEW_ST, name="__Secure-next-auth.session-token", domain=".labs.google"
            ),
        ]
    )

    session_token = await silent_reauthorize(
        profile,
        cookie_jar_reader=lambda _: object(),
        session_factory=factory,
    )

    assert session_token == NEW_ST
