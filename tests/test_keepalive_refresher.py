"""Browser-backed keepalive refresher tests with no real browser or network."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.models import KeepaliveToken
from src.core.token_states import AccountLifecycleStatus, TierClassification
from src.services.keepalive.models import FailureCode
from src.services.keepalive.profile import (
    SessionTokenNotFoundError,
    SessionTokenTooShortError,
)
from src.services.keepalive.refresher import (
    KeepaliveRefresher,
    classify_browser_launch_failure,
    launch_keepalive_browser,
    safe_stop_browser,
)
from src.services.tokens.account_identity import VerifiedAccountSnapshot


LONG_ST = "eyJ" + "s" * 1100
ACCESS_TOKEN = "browser-access-token"
PROJECT_ID = "project-id-must-not-leak"
OBSERVED_AT = datetime(2026, 7, 19, 8, 30, tzinfo=timezone.utc)
EXPIRY = datetime(2026, 7, 20, 10, 11, 12, tzinfo=timezone.utc)


def make_target(**overrides) -> KeepaliveToken:
    values = {
        "id": 23,
        "st": "old-session-token",
        "email": "Ruby@Example.com",
        "at": "old-access-token",
        "at_expires": datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc),
        "current_project_id": PROJECT_ID,
        "membership_confirmed_status": AccountLifecycleStatus.ACTIVE,
        "membership_candidate": TierClassification.UNKNOWN,
        "membership_candidate_count": 0,
        "keepalive_enabled": True,
        "runtime_mode": "persistent",
        "profile_state": "ready",
        "verified_email": "ruby@example.com",
    }
    values.update(overrides)
    return KeepaliveToken(**values)


def session_body(
    *,
    access_token: str | None = ACCESS_TOKEN,
    email: str | None = "  ruby@example.COM ",
    expires: object = "2026-07-20T10:11:12Z",
) -> str:
    user = {"name": "Ruby"}
    if email is not None:
        user["email"] = email
    payload = {"expires": expires, "user": user}
    if access_token is not None:
        payload["access_token"] = access_token
    return json.dumps(payload)


class FakeTab:
    def __init__(self, *, body: str | None = None, ready=True, error=None):
        self.body = body
        self.ready = ready
        self.error = error
        self.evaluations = []

    async def evaluate(self, expression, *, return_by_value):
        self.evaluations.append((expression, return_by_value))
        if self.error is not None:
            raise self.error
        if expression == "document.readyState":
            return "complete" if self.ready else "loading"
        if expression == "document.body.innerText":
            return self.body
        raise AssertionError(f"unexpected expression: {expression}")


class FakeBrowser:
    def __init__(self, flow_tab=None, session_tab=None, *, error=None):
        self.flow_tab = flow_tab or FakeTab()
        self.session_tab = session_tab or FakeTab(body=session_body())
        self.error = error
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.flow_tab if len(self.urls) == 1 else self.session_tab


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    path = tmp_path / "profile"
    path.mkdir()
    return path


def make_refresher(
    *,
    body: str | None = None,
    credits_result=None,
    credits_error=None,
    cookie_reader=None,
    db_error=None,
    network_error_classifier=None,
    browser_error_classifier=None,
):
    db = SimpleNamespace(apply_verified_account_snapshot=AsyncMock())
    if db_error is not None:
        db.apply_verified_account_snapshot.side_effect = db_error
    flow_client = SimpleNamespace(get_credits=AsyncMock())
    if credits_error is not None:
        flow_client.get_credits.side_effect = credits_error
    else:
        flow_client.get_credits.return_value = credits_result or {
            "credits": 960,
            "userPaygateTier": "PAYGATE_TIER_ONE",
        }
    cookie = cookie_reader or Mock(return_value=LONG_ST)
    sleep = AsyncMock()
    kwargs = {
        "db": db,
        "flow_client": flow_client,
        "cookie_reader": cookie,
        "sleep": sleep,
        "clock": lambda: OBSERVED_AT,
        "ready_timeout_seconds": 2,
        "ready_poll_seconds": 1,
        "session_settle_seconds": 0,
    }
    if network_error_classifier is not None:
        kwargs["network_error_classifier"] = network_error_classifier
    if browser_error_classifier is not None:
        kwargs["browser_error_classifier"] = browser_error_classifier
    refresher = KeepaliveRefresher(**kwargs)
    browser = FakeBrowser(session_tab=FakeTab(body=body or session_body()))
    return refresher, browser, db, flow_client, cookie, sleep


@pytest.mark.asyncio
async def test_refresh_success_uses_browser_identity_and_applies_one_atomic_snapshot(profile):
    refresher, browser, db, flow_client, cookie, sleep = make_refresher()
    target = make_target()

    outcome = await refresher.refresh(browser, target, profile, settle_seconds=4.5)

    assert outcome.ok is True
    assert outcome.expiry == EXPIRY
    assert outcome.credits == 960
    assert browser.urls == [
        f"https://labs.google/fx/tools/flow/project/{PROJECT_ID}",
        "https://labs.google/fx/api/auth/session",
    ]
    cookie.assert_called_once_with(profile)
    flow_client.get_credits.assert_awaited_once_with(ACCESS_TOKEN)
    db.apply_verified_account_snapshot.assert_awaited_once()
    token_id, snapshot = db.apply_verified_account_snapshot.await_args.args
    assert token_id == target.id
    assert isinstance(snapshot, VerifiedAccountSnapshot)
    assert snapshot.email == "ruby@example.COM"
    assert snapshot.normalized_email == "ruby@example.com"
    assert snapshot.name == "Ruby"
    assert snapshot.st == LONG_ST
    assert snapshot.at == ACCESS_TOKEN
    assert snapshot.at_expires == EXPIRY
    assert snapshot.credits == 960
    assert snapshot.user_paygate_tier == "PAYGATE_TIER_ONE"
    assert db.apply_verified_account_snapshot.await_args.kwargs == {
        "observed_at": OBSERVED_AT
    }
    sleep.assert_any_await(4.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_expiry", [None, "not-a-date", "2026-07-18T01:00:00Z"])
async def test_refresh_succeeds_with_missing_invalid_or_stale_expiry(profile, raw_expiry):
    body = session_body(expires=raw_expiry)
    refresher, browser, db, *_ = make_refresher(body=body)
    target = make_target(at_expires=datetime(2026, 8, 1, tzinfo=timezone.utc))

    outcome = await refresher.refresh(browser, target, profile, settle_seconds=0)

    assert outcome.ok is True
    assert outcome.credits == 960
    if raw_expiry == "2026-07-18T01:00:00Z":
        assert outcome.expiry == datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)
    else:
        assert outcome.expiry is None
    db.apply_verified_account_snapshot.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        make_target(email="other@example.com", verified_email="ruby@example.com"),
        make_target(email="ruby@example.com", verified_email="other@example.com"),
    ],
)
async def test_identity_mismatch_stops_before_cookie_credits_or_database(profile, target):
    refresher, browser, db, flow_client, cookie, _ = make_refresher()

    outcome = await refresher.refresh(browser, target, profile, settle_seconds=0)

    assert outcome.ok is False
    assert outcome.code is FailureCode.IDENTITY_MISMATCH
    assert outcome.human_action is True
    assert PROJECT_ID not in outcome.detail
    assert ACCESS_TOKEN not in outcome.detail
    cookie.assert_not_called()
    flow_client.get_credits.assert_not_awaited()
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_profile_is_human_action_without_browser_work(tmp_path):
    missing_profile = tmp_path / "missing"
    refresher, browser, db, flow_client, cookie, _ = make_refresher()

    outcome = await refresher.refresh(
        browser, make_target(), missing_profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.PROFILE_MISSING
    assert outcome.human_action is True
    assert browser.urls == []
    cookie.assert_not_called()
    flow_client.get_credits.assert_not_awaited()
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, "", "not-json", session_body(access_token=None)])
async def test_missing_or_invalid_session_body_maps_to_session_body(profile, body):
    refresher, browser, db, flow_client, cookie, _ = make_refresher()
    browser.session_tab.body = body

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.SESSION_BODY
    assert outcome.restart_browser is False
    cookie.assert_not_called()
    flow_client.get_credits.assert_not_awaited()
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_session_rejection_and_missing_email_require_human_action(profile):
    for body in (
        json.dumps({"error": "401 UNAUTHENTICATED"}),
        session_body(email=None),
    ):
        refresher, browser, db, flow_client, cookie, _ = make_refresher(body=body)

        outcome = await refresher.refresh(
            browser, make_target(), profile, settle_seconds=0
        )

        assert outcome.code is FailureCode.SESSION_REJECTED
        assert outcome.human_action is True
        cookie.assert_not_called()
        flow_client.get_credits.assert_not_awaited()
        db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie_value", [None, "", "undefined", "x" * 99])
async def test_missing_or_short_cookie_values_require_human_action(profile, cookie_value):
    refresher, browser, db, flow_client, _, _ = make_refresher(
        cookie_reader=Mock(return_value=cookie_value)
    )

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.COOKIE_MISSING
    assert outcome.human_action is True
    flow_client.get_credits.assert_not_awaited()
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cookie_error",
    [
        SessionTokenNotFoundError("secret cookie absent"),
        SessionTokenTooShortError("secret cookie too short"),
        PermissionError("cookie decrypt failed with secret material"),
    ],
)
async def test_cookie_errors_are_sanitized_and_require_human_action(profile, cookie_error):
    cookie_reader = Mock(side_effect=cookie_error)
    refresher, browser, db, flow_client, _, _ = make_refresher(
        cookie_reader=cookie_reader
    )

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.COOKIE_MISSING
    assert outcome.human_action is True
    assert "secret" not in outcome.detail
    flow_client.get_credits.assert_not_awaited()
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_locked_cookie_store_maps_to_profile_busy(profile):
    refresher, browser, db, flow_client, _, _ = make_refresher(
        cookie_reader=Mock(side_effect=RuntimeError("database is locked"))
    )

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.PROFILE_BUSY
    assert outcome.human_action is False
    flow_client.get_credits.assert_not_awaited()
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_credits_401_maps_to_grant_expired_without_secret_detail(profile):
    refresher, browser, db, _, _, _ = make_refresher(
        credits_error=RuntimeError(f"401 UNAUTHENTICATED token={ACCESS_TOKEN}")
    )

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.GRANT_EXPIRED
    assert outcome.human_action is True
    assert ACCESS_TOKEN not in outcome.detail
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_credits_network_error_uses_injected_classifier(profile):
    network_error = LookupError(f"transport failed with {ACCESS_TOKEN}")
    classifier = Mock(return_value=True)
    refresher, browser, db, _, _, _ = make_refresher(
        credits_error=network_error,
        network_error_classifier=classifier,
    )

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.NETWORK
    assert outcome.restart_browser is False
    assert ACCESS_TOKEN not in outcome.detail
    classifier.assert_called_with(network_error)
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("credits_result", [None, {}, {"credits": True}, {"credits": "960"}, {"credits": -1}])
async def test_invalid_credits_payload_maps_to_credits(profile, credits_result):
    refresher, browser, db, *_ = make_refresher(credits_result={"credits": 1})
    refresher._flow_client.get_credits.return_value = credits_result

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.CREDITS
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_navigation_failure_requests_browser_restart(profile):
    browser_error = RuntimeError(f"page crashed at project {PROJECT_ID}")
    refresher, _, db, flow_client, cookie, _ = make_refresher()
    browser = FakeBrowser(error=browser_error)

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.NAVIGATION
    assert outcome.restart_browser is True
    assert PROJECT_ID not in outcome.detail
    cookie.assert_not_called()
    flow_client.get_credits.assert_not_awaited()
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_wait_is_bounded_even_when_evaluate_hangs(profile):
    class HangingReadyTab(FakeTab):
        async def evaluate(self, expression, *, return_by_value):
            if expression == "document.readyState":
                await asyncio.Event().wait()
            return await super().evaluate(
                expression, return_by_value=return_by_value
            )

    db = SimpleNamespace(apply_verified_account_snapshot=AsyncMock())
    flow_client = SimpleNamespace(
        get_credits=AsyncMock(
            return_value={
                "credits": 960,
                "userPaygateTier": "PAYGATE_TIER_ONE",
            }
        )
    )
    refresher = KeepaliveRefresher(
        db,
        flow_client,
        cookie_reader=Mock(return_value=LONG_ST),
        ready_timeout_seconds=0.01,
        ready_poll_seconds=0.005,
        session_settle_seconds=0,
        clock=lambda: OBSERVED_AT,
    )
    browser = FakeBrowser(
        flow_tab=HangingReadyTab(),
        session_tab=FakeTab(body=session_body()),
    )

    outcome = await asyncio.wait_for(
        refresher.refresh(browser, make_target(), profile, settle_seconds=0),
        timeout=0.2,
    )

    assert outcome.ok is True


@pytest.mark.asyncio
async def test_session_evaluate_browser_failure_requests_restart(profile):
    refresher, browser, db, flow_client, cookie, _ = make_refresher()
    browser.session_tab.error = RuntimeError("target closed")

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.SESSION_BODY
    assert outcome.restart_browser is True
    cookie.assert_not_called()
    flow_client.get_credits.assert_not_awaited()
    db.apply_verified_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_rejection_is_sanitized_internal_failure(profile):
    db_error = ValueError(f"snapshot collision st={LONG_ST} at={ACCESS_TOKEN}")
    refresher, browser, db, *_ = make_refresher(db_error=db_error)

    outcome = await refresher.refresh(
        browser, make_target(), profile, settle_seconds=0
    )

    assert outcome.code is FailureCode.INTERNAL
    assert LONG_ST not in outcome.detail
    assert ACCESS_TOKEN not in outcome.detail
    db.apply_verified_account_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_safe_stop_browser_handles_sync_async_and_errors():
    sync_browser = SimpleNamespace(stop=Mock(return_value=None))
    async_stop = AsyncMock(return_value=None)
    async_browser = SimpleNamespace(stop=async_stop)
    failed_browser = SimpleNamespace(stop=Mock(side_effect=RuntimeError("closed")))

    assert await safe_stop_browser(sync_browser) is True
    assert await safe_stop_browser(async_browser) is True
    assert await safe_stop_browser(failed_browser) is False
    assert await safe_stop_browser(None) is True
    sync_browser.stop.assert_called_once_with()
    async_stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_launch_uses_lazy_module_headed_default_and_no_exposed_debug_port(
    tmp_path, monkeypatch
):
    from src.services.keepalive import refresher as module

    profile = tmp_path / "profile"
    profile.mkdir()
    executable = tmp_path / "chrome"
    executable.write_text("binary")
    browser = object()
    fake_nodriver = SimpleNamespace(start=AsyncMock(return_value=browser))
    module_loader = Mock(return_value=fake_nodriver)
    patcher = Mock()
    monkeypatch.setattr(module, "_load_nodriver", module_loader)
    monkeypatch.setattr(module, "_load_runtime_patcher", lambda: patcher)
    monkeypatch.setenv("DISPLAY", ":old")

    result = await launch_keepalive_browser(
        profile,
        "http://127.0.0.1:7890",
        ":11",
        executable,
    )

    assert result is browser
    module_loader.assert_called_once_with()
    patcher.assert_called_once_with(browser)
    kwargs = fake_nodriver.start.await_args.kwargs
    assert kwargs["headless"] is False
    assert kwargs["user_data_dir"] == str(profile.resolve())
    assert kwargs["browser_executable_path"] == str(executable.resolve())
    assert kwargs["sandbox"] is False
    assert "host" not in kwargs
    assert "port" not in kwargs
    assert "--proxy-server=http://127.0.0.1:7890" in kwargs["browser_args"]
    assert not any(
        argument.startswith("--remote-debugging") for argument in kwargs["browser_args"]
    )
    assert "--remote-allow-origins=*" not in kwargs["browser_args"]
    assert inspect.iscoroutinefunction(launch_keepalive_browser)


@pytest.mark.asyncio
async def test_launch_rejects_proxy_userinfo_before_nodriver_receives_browser_args(
    tmp_path, monkeypatch
):
    from src.services.keepalive import refresher as module

    profile = tmp_path / "profile"
    profile.mkdir()
    executable = tmp_path / "chrome"
    executable.write_text("binary")
    module_loader = Mock()
    monkeypatch.setattr(module, "_load_nodriver", module_loader)
    secret_proxy = "http://proxy-user:proxy-password@127.0.0.1:7890"

    with pytest.raises(ValueError, match="must not include userinfo") as error:
        await launch_keepalive_browser(profile, secret_proxy, ":11", executable)

    module_loader.assert_not_called()
    assert "proxy-user" not in str(error.value)
    assert "proxy-password" not in str(error.value)


@pytest.mark.asyncio
async def test_launch_retries_root_sandbox_failure_with_explicit_no_sandbox(
    tmp_path, monkeypatch
):
    from src.services.keepalive import refresher as module

    profile = tmp_path / "profile"
    profile.mkdir()
    executable = tmp_path / "chrome"
    executable.write_text("binary")
    browser = object()
    fake_nodriver = SimpleNamespace(
        start=AsyncMock(
            side_effect=[RuntimeError("root requires no_sandbox"), browser]
        )
    )
    monkeypatch.setattr(module, "_load_nodriver", lambda: fake_nodriver)
    monkeypatch.setattr(module, "_load_runtime_patcher", lambda: Mock())

    result = await launch_keepalive_browser(profile, None, None, executable)

    assert result is browser
    assert fake_nodriver.start.await_count == 2
    first_kwargs = fake_nodriver.start.await_args_list[0].kwargs
    second_kwargs = fake_nodriver.start.await_args_list[1].kwargs
    assert first_kwargs["sandbox"] is False
    assert second_kwargs["sandbox"] is True
    assert "--no-sandbox" in second_kwargs["browser_args"]
    assert "host" not in second_kwargs
    assert "port" not in second_kwargs


def test_browser_launch_failure_mapping_is_typed_and_injectable():
    error = RuntimeError("chrome process exited")
    outcome = classify_browser_launch_failure(
        error, network_error_classifier=lambda candidate: False
    )
    network = classify_browser_launch_failure(
        error, network_error_classifier=lambda candidate: candidate is error
    )

    assert outcome.code is FailureCode.BROWSER_LAUNCH
    assert outcome.restart_browser is True
    assert network.code is FailureCode.NETWORK
    assert network.restart_browser is False
