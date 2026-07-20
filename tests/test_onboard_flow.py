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
    """Minimal Popen-like stub exposing ``pid``/``wait``/``returncode``.

    ``waited`` flips to ``True`` only once ``wait()`` actually returns a real
    exit code (never on a ``TimeoutExpired`` -- the process is still alive
    then). Tests use it to simulate the OS reaping/recycling a PID after a
    real exit, so a fake ``os.getpgid`` can raise ``ProcessLookupError`` for
    any call made *after* that point -- exactly what a real ``os.getpgid``
    would do on an already-reaped PID.
    """

    def __init__(self, *, pid: int = 12345, returncode: int | None = None,
                 wait_result: int | None = 0,
                 wait_raises: Exception | None = None):
        self.pid = pid
        self.returncode = returncode
        self._wait_result = wait_result
        self._wait_raises = wait_raises
        self.terminated = False
        self.killed = False
        self.waited = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_raises is not None:
            raise self._wait_raises
        self.waited = True
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

    ``os.getpgid`` is stubbed to raise ``ProcessLookupError`` for any call
    made *after* ``proc.wait()`` has already returned (``proc.waited`` is
    ``True`` by then), simulating a real OS having reaped/recycled the PID.
    This is the exact regression this test guards: the old code called
    ``os.getpgid(proc.pid)`` *inside* the crash-cleanup helper, i.e. after
    ``wait()`` already ran, which made cleanup a silent no-op (no ``killpg``
    call at all, so the assertions below would fail). The fix captures the
    pgid immediately after launch, while the process is still alive, so this
    fake only ever sees a pre-wait call and succeeds.
    """
    killed: dict[str, Any] = {"pgid": None, "sig": None}
    proc = _FakeProc(returncode=13, wait_result=13)

    def fake_getpgid(pid):
        if proc.waited:
            raise ProcessLookupError(f"no such process: {pid}")
        return 1

    monkeypatch.setattr(
        "src.services.tokens.onboard.subprocess.Popen", lambda *a, **kw: proc
    )
    monkeypatch.setattr(
        "src.services.tokens.onboard.os.getpgid", fake_getpgid
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


# ---------------------------------------------------------------------------
# onboard_new / onboard_existing / try_readonly_validate (Task 3 flows)
#
# These compose launch_chrome/verify_profile (monkeypatched at the module
# level they are called from) with a real Database on a tmp_path SQLite file,
# so the INSERT/rename/publish/compensation behavior is exercised for real.
# ---------------------------------------------------------------------------

from src.core.database import Database
from src.core.models import Token
from src.services.tokens.onboard import onboard_existing, onboard_new, try_readonly_validate

OLD_TOKEN_EMAIL = "old@example.com"
NEW_TOKEN_EMAIL = "new@example.com"


def _make_db(tmp_path: Path) -> Database:
    db = Database(db_path=str(tmp_path / "flow.db"))
    asyncio.run(db.init_db())
    return db


def _make_old_token(db: Database, tmp_path: Path, *, keepalive_enabled: bool = False) -> int:
    token_id = asyncio.run(
        db.add_token(
            Token(
                st="placeholder-" + "x" * 1100,
                email=OLD_TOKEN_EMAIL,
                name="Old",
                is_active=True,
            )
        )
    )
    if keepalive_enabled:
        asyncio.run(db.set_token_desired_state(token_id, keepalive_enabled=True))
    (tmp_path / str(token_id)).mkdir(parents=True)
    return token_id


async def _async_none(*_args, **_kwargs):
    return None


def test_new_token_uses_temp_profile_then_rename(tmp_path, monkeypatch):
    """新号: temp profile 登录 -> INSERT token -> rename 到 base/<id>, DB 无 placeholder 残留。"""
    db = _make_db(tmp_path)
    runtime = _fake_runtime(tmp_path)

    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)

    async def fake_verify(profile_path, flow_client, **kw):
        return _make_snapshot("x" * 1100, email=NEW_TOKEN_EMAIL)

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify)
    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", _async_none)

    outcome = asyncio.run(
        onboard_new(
            email=NEW_TOKEN_EMAIL,
            runtime=runtime,
            display=":11",
            db=db,
            flow_client=object(),
            pool_size=4,
            observed_at=datetime.now(timezone.utc),
        )
    )

    assert outcome.keepalive_enabled is True
    assert outcome.runtime_mode == "persistent"

    onboarding_dir = tmp_path / ".onboarding"
    assert list(onboarding_dir.glob("*")) == []  # temp profile 已清空

    tokens = asyncio.run(db.get_all_tokens())
    assert len(tokens) == 1
    assert tokens[0].st == "x" * 1100  # 真实 ST 落库,无 placeholder
    assert (tmp_path / str(tokens[0].id)).is_dir()  # 已 rename 到 base/<id>


def test_new_token_failure_cleans_temp_profile_and_no_db_row(tmp_path, monkeypatch):
    """新号 verify 失败 -> temp profile rm + 无 token 行残留。"""
    db = _make_db(tmp_path)
    runtime = _fake_runtime(tmp_path)

    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)

    async def fake_verify_fail(profile_path, flow_client, **kw):
        raise OnboardError("cookie_missing")

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify_fail)
    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", _async_none)

    with pytest.raises(OnboardError):
        asyncio.run(
            onboard_new(
                email=NEW_TOKEN_EMAIL,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
            )
        )

    onboarding_dir = tmp_path / ".onboarding"
    assert list(onboarding_dir.glob("*")) == []
    assert asyncio.run(db.get_all_tokens()) == []


def test_new_token_identity_mismatch_cleans_up(tmp_path, monkeypatch):
    """登录后邮箱与请求邮箱不符 -> identity_mismatch, 清 temp profile, 无 token 行。"""
    db = _make_db(tmp_path)
    runtime = _fake_runtime(tmp_path)

    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)

    async def fake_verify(profile_path, flow_client, **kw):
        return _make_snapshot("x" * 1100, email="someone-else@example.com")

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify)

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            onboard_new(
                email=NEW_TOKEN_EMAIL,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
            )
        )
    assert exc.value.code == "identity_mismatch"
    assert list((tmp_path / ".onboarding").glob("*")) == []
    assert asyncio.run(db.get_all_tokens()) == []


def test_new_token_project_pool_failure_cleans_up(tmp_path, monkeypatch):
    """项目池 provisioning 失败 -> 删 token 行 + rm temp profile,编码为 project_pool_failed。"""
    db = _make_db(tmp_path)
    runtime = _fake_runtime(tmp_path)

    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)

    async def fake_verify(profile_path, flow_client, **kw):
        return _make_snapshot("x" * 1100, email=NEW_TOKEN_EMAIL)

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify)

    async def fake_pool_fail(*_a, **_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", fake_pool_fail)

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            onboard_new(
                email=NEW_TOKEN_EMAIL,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
            )
        )
    assert exc.value.code == "project_pool_failed"
    assert asyncio.run(db.get_all_tokens()) == []


def test_new_token_publish_failure_cleans_up_renamed_profile(tmp_path, monkeypatch):
    """rename 之后 publish 失败 -> 删 token 行 + rm 已 rename 的 final profile。"""
    db = _make_db(tmp_path)
    runtime = _fake_runtime(tmp_path)

    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)

    async def fake_verify(profile_path, flow_client, **kw):
        return _make_snapshot("x" * 1100, email=NEW_TOKEN_EMAIL)

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify)
    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", _async_none)

    class _FailingRepo:
        def __init__(self, _db):
            pass

        async def publish_verified_account(self, **_kw):
            raise RuntimeError("db exploded")

    monkeypatch.setattr(
        "src.services.tokens.onboard.TokenLifecycleRepository", _FailingRepo
    )

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            onboard_new(
                email=NEW_TOKEN_EMAIL,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
            )
        )
    assert exc.value.code == "publish_failed"
    assert asyncio.run(db.get_all_tokens()) == []
    assert list((tmp_path / ".onboarding").glob("*")) == []
    # No leftover numeric profile directory anywhere under tmp_path either.
    assert not any(p.is_dir() and p.name.isdigit() for p in tmp_path.iterdir())


def test_old_token_readonly_validate_skips_login(tmp_path, monkeypatch):
    """旧号 profile 活着 -> 只读验证通过 -> 免登录发布,profile 未被替换。"""
    db = _make_db(tmp_path)
    token_id = _make_old_token(db, tmp_path)
    runtime = _fake_runtime(tmp_path)
    profile_path = tmp_path / str(token_id)

    async def fake_verify(profile_path_arg, flow_client, **kw):
        assert profile_path_arg == profile_path
        return _make_snapshot("x" * 1100, email=OLD_TOKEN_EMAIL)

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify)
    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", _async_none)

    launched = {"n": 0}

    def fake_launch(*a, **kw):
        launched["n"] += 1

    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", fake_launch)

    outcome = asyncio.run(
        onboard_existing(
            token_id=token_id,
            runtime=runtime,
            display=":11",
            db=db,
            flow_client=object(),
            pool_size=4,
            observed_at=datetime.now(timezone.utc),
        )
    )

    assert outcome.keepalive_enabled is True
    assert launched["n"] == 0  # 免登录
    assert profile_path.is_dir()  # 原 profile 未被 archive/替换


def test_existing_account_project_pool_uses_verified_snapshot_st_not_stale_db_st(
    tmp_path, monkeypatch
):
    """项目池 provisioning 必须用刚验证的 ``snapshot.st``,不能用尚未落库的旧 DB ST。

    ``_publish_account``(把 ``snapshot.st`` 写回 ``tokens`` 表)在
    ``_run_project_pool`` 之后才跑,所以 provisioning 阶段 ``db.get_token`` 读到
    的仍是登录前的旧 ST。如果直接把那行转给 ``ensure_project_pool``/
    ``FlowClient``,provisioning 就会用一个已经失效的会话 —— 这在今天是潜伏的
    (现有账号都已有满池,创建循环从不触发),但会在第一个需要新建项目的账号上
    暴露出来。
    """
    db = _make_db(tmp_path)
    token_id = _make_old_token(db, tmp_path)
    stale_db_st = asyncio.run(db.get_token(token_id)).st
    fresh_st = "y" * 1100
    assert fresh_st != stale_db_st  # sanity: the two STs must actually differ

    async def fake_verify(profile_path_arg, flow_client, **kw):
        return _make_snapshot(fresh_st, email=OLD_TOKEN_EMAIL)

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify)

    captured = {}

    async def capture_pool(_db, _flow_client, token, pool_size):
        captured["st"] = token.st
        captured["token_id"] = token.id
        return []

    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", capture_pool)

    outcome = asyncio.run(
        onboard_existing(
            token_id=token_id,
            runtime=_fake_runtime(tmp_path),
            display=":11",
            db=db,
            flow_client=object(),
            pool_size=4,
            observed_at=datetime.now(timezone.utc),
        )
    )

    assert captured["token_id"] == token_id
    assert captured["st"] == fresh_st  # not stale_db_st
    assert outcome.keepalive_enabled is True


def test_old_token_relogin_when_readonly_fails(tmp_path, monkeypatch):
    """旧号只读验证失败 -> 触发重登录,在同一个原 profile 里(非 temp)。"""
    db = _make_db(tmp_path)
    token_id = _make_old_token(db, tmp_path)
    runtime = _fake_runtime(tmp_path)
    profile_path = tmp_path / str(token_id)

    calls = {"n": 0}

    async def fake_verify(profile_path_arg, flow_client, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OnboardError("cookie_missing")
        return _make_snapshot("x" * 1100, email=OLD_TOKEN_EMAIL)

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify)
    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", _async_none)

    launched_paths = []

    def fake_launch(runtime_arg, profile_path_arg, display_arg, flow_url_arg, **kw):
        launched_paths.append(profile_path_arg)
        assert flow_url_arg == FLOW_URL

    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", fake_launch)

    outcome = asyncio.run(
        onboard_existing(
            token_id=token_id,
            runtime=runtime,
            display=":11",
            db=db,
            flow_client=object(),
            pool_size=4,
            observed_at=datetime.now(timezone.utc),
        )
    )
    assert outcome.keepalive_enabled is True
    assert launched_paths == [profile_path]  # 复登在原 profile,非 temp/rename


def test_old_token_keepalive_paused_and_restored_on_failure(tmp_path, monkeypatch):
    """旧号先停 keepalive; 若最终失败, 必须恢复原 keepalive 状态(验证不过=什么都不改)。"""
    db = _make_db(tmp_path)
    token_id = _make_old_token(db, tmp_path, keepalive_enabled=True)
    runtime = _fake_runtime(tmp_path)

    async def fake_verify_fail(profile_path, flow_client, **kw):
        raise OnboardError("session_rejected")

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify_fail)
    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)

    with pytest.raises(OnboardError):
        asyncio.run(
            onboard_existing(
                token_id=token_id,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
            )
        )

    lifecycle = asyncio.run(db.get_token_lifecycle(token_id))
    assert lifecycle.keepalive_enabled is True  # 恢复原状态
    assert (tmp_path / str(token_id)).is_dir()  # 原 profile 分毫未动


def test_old_token_identity_mismatch_restores_keepalive(tmp_path, monkeypatch):
    """旧号重登后邮箱不符预期账号 -> identity_mismatch, 恢复 keepalive。"""
    db = _make_db(tmp_path)
    token_id = _make_old_token(db, tmp_path, keepalive_enabled=True)
    runtime = _fake_runtime(tmp_path)

    async def fake_verify(profile_path, flow_client, **kw):
        return _make_snapshot("x" * 1100, email="wrong-account@example.com")

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify)
    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", _async_none)
    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            onboard_existing(
                token_id=token_id,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
            )
        )
    assert exc.value.code == "identity_mismatch"
    lifecycle = asyncio.run(db.get_token_lifecycle(token_id))
    assert lifecycle.keepalive_enabled is True


def test_old_token_not_found_raises(tmp_path):
    """不存在的 token_id -> OnboardError('not_found'),不触碰任何 profile。"""
    db = _make_db(tmp_path)
    runtime = _fake_runtime(tmp_path)
    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            onboard_existing(
                token_id=999,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
            )
        )
    assert exc.value.code == "not_found"


def test_old_token_lease_busy_raises_profile_busy(tmp_path, monkeypatch):
    """profile lease 一直抢不到 -> 超时后 OnboardError('profile_busy')。

    Uses ``lease_wait_seconds=0`` with the real ``time.monotonic``/``time.sleep``
    rather than monkeypatching the global ``time`` module: ``time`` is a
    process-wide singleton, so patching ``time.monotonic`` there (even via the
    onboard-module path) mutates it for every consumer in the process --
    including asyncio's own event-loop scheduling -- which was observed to
    hang the test run instead of raising promptly.
    """
    db = _make_db(tmp_path)
    token_id = _make_old_token(db, tmp_path)
    runtime = _fake_runtime(tmp_path)

    from src.services.keepalive.profile import ProfileLeaseBusyError

    def fake_acquire(base_dir, requested_token_id):
        raise ProfileLeaseBusyError(Path(base_dir) / str(requested_token_id))

    monkeypatch.setattr("src.services.tokens.onboard.acquire_profile_lease", fake_acquire)

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            onboard_existing(
                token_id=token_id,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
                lease_wait_seconds=0,
            )
        )
    assert exc.value.code == "profile_busy"


def test_old_token_lease_busy_restores_keepalive(tmp_path, monkeypatch):
    """keepalive 原本开启 + lease 一直抢不到超时 -> profile_busy, 且 keepalive 必须恢复为 True。

    Regression for the critical finding where ``_pause_keepalive_if_enabled``
    and ``_poll_profile_lease`` ran OUTSIDE the inner try/except that calls
    ``_restore_keepalive_state``: a lease-poll timeout propagated straight
    through the outer ``finally`` and left keepalive disabled forever. See
    ``test_old_token_lease_busy_raises_profile_busy`` above for why real
    ``time.monotonic``/``time.sleep`` (not a monkeypatched ``time`` module)
    are used to force the timeout deterministically.
    """
    db = _make_db(tmp_path)
    token_id = _make_old_token(db, tmp_path, keepalive_enabled=True)
    runtime = _fake_runtime(tmp_path)

    from src.services.keepalive.profile import ProfileLeaseBusyError

    def fake_acquire(base_dir, requested_token_id):
        raise ProfileLeaseBusyError(Path(base_dir) / str(requested_token_id))

    monkeypatch.setattr("src.services.tokens.onboard.acquire_profile_lease", fake_acquire)

    with pytest.raises(OnboardError) as exc:
        asyncio.run(
            onboard_existing(
                token_id=token_id,
                runtime=runtime,
                display=":11",
                db=db,
                flow_client=object(),
                pool_size=4,
                observed_at=datetime.now(timezone.utc),
                lease_wait_seconds=0,
            )
        )
    assert exc.value.code == "profile_busy"

    lifecycle = asyncio.run(db.get_token_lifecycle(token_id))
    assert lifecycle.keepalive_enabled is True  # 必须恢复,不能永久停在 False


def test_try_readonly_validate_returns_none_on_failure(tmp_path, monkeypatch):
    """底层 verify_profile 抛 OnboardError -> try_readonly_validate 吞掉返回 None。"""

    async def fake_verify_fail(profile_path, flow_client, **kw):
        raise OnboardError("cookie_missing")

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify_fail)
    result = asyncio.run(try_readonly_validate(tmp_path, object()))
    assert result is None


def test_try_readonly_validate_returns_snapshot_on_success(tmp_path, monkeypatch):
    """底层 verify_profile 成功 -> try_readonly_validate 原样返回 snapshot。"""

    async def fake_verify_ok(profile_path, flow_client, **kw):
        return _make_snapshot("x" * 1100, email=OLD_TOKEN_EMAIL)

    monkeypatch.setattr("src.services.tokens.onboard.verify_profile", fake_verify_ok)
    result = asyncio.run(try_readonly_validate(tmp_path, object()))
    assert result is not None
    assert result.email == OLD_TOKEN_EMAIL
