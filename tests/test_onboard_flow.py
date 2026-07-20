"""Onboard.py browser orchestration core tests.

Scope (per spec §6.0/§6.5): global onboard lease serialization, foreground Chrome
launch with process-group cleanup on timeout/crash, and profile verification
(cookie ST read + account identity inspection). Compositional flows
(``onboard_new``/``onboard_existing``) and publishing are covered elsewhere.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core.account_identity import VerifiedAccountSnapshot
from src.services.tokens.onboard import (
    OnboardError,
    acquire_onboard_global_lease,
    launch_chrome,
    verify_profile,
)


def _fake_runtime(tmp_path: Path):
    """Build a minimal ``SetupRuntime`` whose proxy/browser paths are inert."""
    from scripts.setup_keepalive_profile import SetupRuntime

    return SetupRuntime(
        profile_base=tmp_path,
        proxy="",
        browser_executable=tmp_path / "chrome",
    )


FLOW_URL = "https://labs.google/fx/tools/flow"


# ---------------------------------------------------------------------------
# Global onboard lease
# ---------------------------------------------------------------------------


def test_global_onboard_lease_serializes(tmp_path):
    """Only one global onboard lease may be held at a time per base directory."""
    lease1 = acquire_onboard_global_lease(tmp_path)
    assert lease1 is not None
    with pytest.raises(OnboardError) as exc:
        acquire_onboard_global_lease(tmp_path)
    assert exc.value.code == "onboard_busy"
    lease1.release()
    # Released → a second acquire must succeed.
    lease2 = acquire_onboard_global_lease(tmp_path)
    assert lease2 is not None
    lease2.release()


# ---------------------------------------------------------------------------
# launch_chrome: timeout, process group, build_browser_command reuse
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal Popen-like stub exposing ``pid``/``wait``/``returncode``."""

    def __init__(self, *, pid: int = 12345, returncode: int | None = None,
                 wait_result: int | None = 0,
                 wait_raises: Exception | None = None):
        self.pid = pid
        self.returncode = returncode
        self._wait_result = wait_result
        self._wait_raises = wait_raises
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_raises is not None:
            raise self._wait_raises
        return self._wait_result or 0


def test_launch_chrome_timeout_kills_process_group(tmp_path, monkeypatch):
    """Timeout → killpg called on the whole group, OnboardError('login_timeout')."""
    killed: dict[str, Any] = {"pgid": None, "sig": None}

    proc = _FakeProc(
        wait_raises=subprocess.TimeoutExpired(cmd="chrome", timeout=1),
    )

    def fake_popen(*args, **kwargs):
        return proc

    def fake_killpg(pgid, sig):
        killed["pgid"] = pgid
        killed["sig"] = sig

    monkeypatch.setattr(
        "src.services.tokens.onboard.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr("src.services.tokens.onboard.os.killpg", fake_killpg)
    monkeypatch.setattr("src.services.tokens.onboard.os.getpgid", lambda pid: 99999)

    with pytest.raises(OnboardError) as exc:
        launch_chrome(
            runtime=_fake_runtime(tmp_path),
            profile_path=tmp_path / "p",
            display=":11",
            flow_url=FLOW_URL,
            timeout_seconds=1,
        )
    assert exc.value.code == "login_timeout"
    assert killed["pgid"] == 99999
    assert killed["sig"] == signal.SIGKILL


def test_launch_chrome_uses_build_browser_command_with_explicit_default(
    tmp_path, monkeypatch
):
    """launch_chrome must delegate argv construction to build_browser_command
    (which emits ``--profile-directory=Default``); it must not hand-build argv."""
    captured: dict[str, Any] = {"argv": None, "env_display": None}

    proc = _FakeProc(returncode=0, wait_result=0)

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env_display"] = kwargs.get("env", {}).get("DISPLAY")
        captured["start_new_session"] = kwargs.get("start_new_session")
        return proc

    monkeypatch.setattr(
        "src.services.tokens.onboard.subprocess.Popen", fake_popen
    )
    monkeypatch.setattr(
        "src.services.tokens.onboard.os.getpgid", lambda pid: 1
    )

    launch_chrome(
        runtime=_fake_runtime(tmp_path),
        profile_path=tmp_path / "p",
        display=":11",
        flow_url=FLOW_URL,
    )

    argv = captured["argv"]
    assert argv is not None
    assert "--profile-directory=Default" in argv
    assert FLOW_URL in argv
    # Browser executable is the first argv element (SetupRuntime carries it).
    assert argv[0] == str(tmp_path / "chrome")
    # DISPLAY from the argument is propagated into the child env.
    assert captured["env_display"] == ":11"
    # Chrome runs as its own session leader so the group can be killed wholesale.
    assert captured["start_new_session"] is True


def test_launch_chrome_crash_raises_browser_crashed(tmp_path, monkeypatch):
    """Non-zero Chrome exit → OnboardError('browser_crashed').

    Per spec §6.5, a crash also triggers process-group cleanup (the exited
    process may still have orphaned children), so ``os.killpg`` must be
    stubbed here too -- never let a unit test invoke the real syscall.
    """
    killed: dict[str, Any] = {"pgid": None, "sig": None}
    proc = _FakeProc(returncode=13, wait_result=13)

    monkeypatch.setattr(
        "src.services.tokens.onboard.subprocess.Popen", lambda *a, **kw: proc
    )
    monkeypatch.setattr(
        "src.services.tokens.onboard.os.getpgid", lambda pid: 1
    )
    monkeypatch.setattr(
        "src.services.tokens.onboard.os.killpg",
        lambda pgid, sig: killed.update(pgid=pgid, sig=sig),
    )

    with pytest.raises(OnboardError) as exc:
        launch_chrome(
            runtime=_fake_runtime(tmp_path),
            profile_path=tmp_path / "p",
            display=":11",
            flow_url=FLOW_URL,
        )
    assert exc.value.code == "browser_crashed"
    assert killed["pgid"] == 1
    assert killed["sig"] == signal.SIGTERM


def test_launch_chrome_launch_failure_raises_browser_launch(tmp_path, monkeypatch):
    """Popen itself raising → OnboardError('browser_launch')."""

    def fake_popen(*args, **kwargs):
        raise OSError("display unreachable")

    monkeypatch.setattr(
        "src.services.tokens.onboard.subprocess.Popen", fake_popen
    )

    with pytest.raises(OnboardError) as exc:
        launch_chrome(
            runtime=_fake_runtime(tmp_path),
            profile_path=tmp_path / "p",
            display=":11",
            flow_url=FLOW_URL,
        )
    assert exc.value.code == "browser_launch"


def test_launch_chrome_bad_proxy_raises_browser_launch_without_leaking_credentials(
    tmp_path, monkeypatch
):
    """``build_browser_command`` runs inside the try block too.

    A proxy string with embedded userinfo (a plausible operator mistake, e.g.
    copy-pasting an authenticated-proxy URL straight into config) makes
    ``validate_proxy_server`` raise a raw ``ValueError`` *before* Chrome is
    ever launched. That must still surface as the stable
    ``OnboardError('browser_launch')`` code -- not an unwrapped ValueError --
    and the raised message must never echo the proxy string, since it may
    carry a password.
    """
    from scripts.setup_keepalive_profile import SetupRuntime

    secret_proxy = "http://user:pass@host:8080"
    runtime = SetupRuntime(
        profile_base=tmp_path,
        proxy=secret_proxy,
        browser_executable=tmp_path / "chrome",
    )

    def fake_popen(*args, **kwargs):
        raise AssertionError("Chrome must not be launched with an invalid proxy")

    monkeypatch.setattr(
        "src.services.tokens.onboard.subprocess.Popen", fake_popen
    )

    with pytest.raises(OnboardError) as exc:
        launch_chrome(
            runtime=runtime,
            profile_path=tmp_path / "p",
            display=":11",
            flow_url=FLOW_URL,
        )
    assert exc.value.code == "browser_launch"
    message = str(exc.value)
    assert "pass" not in message
    assert secret_proxy not in message


# ---------------------------------------------------------------------------
# verify_profile
# ---------------------------------------------------------------------------


def _make_snapshot(st: str, email: str = "a@b.com") -> VerifiedAccountSnapshot:
    return VerifiedAccountSnapshot(
        email=email,
        normalized_email=email.casefold(),
        name="A",
        st=st,
        at="at-token",
        at_expires=datetime(2030, 1, 1, tzinfo=timezone.utc),
        credits=100,
        user_paygate_tier="PAYGATE_TIER_ONE",
    )


def test_verify_profile_returns_snapshot(tmp_path):
    """Successful cookie read + identity inspect → VerifiedAccountSnapshot."""
    st = "x" * 1100

    def fake_read(profile_path):
        return st

    async def fake_inspect(flow_client, token):
        assert token == st
        return _make_snapshot(token)

    snapshot = asyncio.run(
        verify_profile(
            tmp_path,
            flow_client=object(),
            session_reader=fake_read,
            identity_inspector=fake_inspect,
        )
    )
    assert isinstance(snapshot, VerifiedAccountSnapshot)
    assert snapshot.st == st
    assert snapshot.credits == 100


def test_verify_profile_raises_cookie_missing_when_session_unreadable(tmp_path):
    """Cookie read failure short-circuits before any network call."""

    def fake_read(profile_path):
        raise FileNotFoundError("no cookie file")

    async def fake_inspect(flow_client, token):
        raise AssertionError("identity inspector must not run without a cookie")

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            verify_profile(
                tmp_path,
                flow_client=object(),
                session_reader=fake_read,
                identity_inspector=fake_inspect,
            )
        )
    assert exc.value.code == "cookie_missing"


def test_verify_profile_propagates_inspect_error_code(tmp_path):
    """An identity-inspection failure carries its classified ``code`` upward."""

    class FakeIdentityError(ValueError):
        def __init__(self, message, *, code):
            super().__init__(message)
            self.code = code

    def fake_read(profile_path):
        return "x" * 1100

    async def fake_inspect(flow_client, token):
        raise FakeIdentityError("session rejected", code="session_rejected")

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            verify_profile(
                tmp_path,
                flow_client=object(),
                session_reader=fake_read,
                identity_inspector=fake_inspect,
            )
        )
    assert exc.value.code == "session_rejected"


def test_verify_profile_defaults_to_session_body_when_inspector_raises_plain_error(
    tmp_path,
):
    """An inspector exception without a ``code`` attribute falls back to session_body."""

    def fake_read(profile_path):
        return "x" * 1100

    async def fake_inspect(flow_client, token):
        raise ValueError("unexpected payload")

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            verify_profile(
                tmp_path,
                flow_client=object(),
                session_reader=fake_read,
                identity_inspector=fake_inspect,
            )
        )
    assert exc.value.code == "session_body"
