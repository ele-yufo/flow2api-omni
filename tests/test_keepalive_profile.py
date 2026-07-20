"""Safety tests for Chrome keepalive profile ownership and cookie access."""

from __future__ import annotations

import multiprocessing
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services.keepalive.profile import (
    MIN_SESSION_TOKEN_LENGTH,
    ProcessSnapshot,
    ProfileBusyError,
    ProfileLeaseBusyError,
    ProfileLockUncertainError,
    SessionTokenNotFoundError,
    SessionTokenTooShortError,
    SingletonLockState,
    acquire_profile_lease,
    acquire_profile_path_lease,
    canonical_profile_path,
    inspect_singleton_lock,
    prepare_profile,
    read_proc_cmdline,
    read_proc_start_ticks,
    read_session_token,
    verify_process_ownership,
)


def _hold_profile_lease(base_dir: str, ready, release) -> None:
    with acquire_profile_lease(base_dir, 23):
        ready.send(True)
        release.recv()


def _make_profile(base_dir: Path, token_id: int = 23) -> Path:
    profile = base_dir / str(token_id)
    profile.mkdir(parents=True)
    return profile.resolve()


def _make_singleton_artifacts(profile: Path, pid: int) -> None:
    os.symlink(f"{socket.gethostname()}-{pid}", profile / "SingletonLock")
    os.symlink("cookie-secret", profile / "SingletonCookie")
    os.symlink("/tmp/nonexistent-chrome-socket", profile / "SingletonSocket")


def test_canonical_profile_path_is_constrained_to_base(tmp_path):
    base_dir = tmp_path / "profiles"
    base_dir.mkdir()

    assert canonical_profile_path(base_dir, 23) == (base_dir / "23").resolve()
    assert canonical_profile_path(base_dir, "23") == (base_dir / "23").resolve()


@pytest.mark.parametrize(
    "token_id",
    [
        None,
        True,
        False,
        0,
        -1,
        1.5,
        "",
        "0",
        "-1",
        "+1",
        "01",
        " 23",
        "23 ",
        "../23",
        "23/../../outside",
        "/tmp/profile",
        "abc",
    ],
)
def test_canonical_profile_path_rejects_invalid_token_ids(tmp_path, token_id):
    base_dir = tmp_path / "profiles"
    base_dir.mkdir()

    with pytest.raises((TypeError, ValueError)):
        canonical_profile_path(base_dir, token_id)


def test_canonical_profile_path_rejects_symlink_escape(tmp_path):
    base_dir = tmp_path / "profiles"
    outside_dir = tmp_path / "outside"
    base_dir.mkdir()
    outside_dir.mkdir()
    os.symlink(outside_dir, base_dir / "23")

    with pytest.raises(ValueError, match="outside configured base"):
        canonical_profile_path(base_dir, 23)


def test_profile_lease_is_nonblocking_across_processes(tmp_path):
    base_dir = tmp_path / "profiles"
    base_dir.mkdir()
    context = multiprocessing.get_context("fork")
    parent_ready, child_ready = context.Pipe()
    parent_release, child_release = context.Pipe()
    process = context.Process(
        target=_hold_profile_lease,
        args=(str(base_dir), child_ready, child_release),
    )
    process.start()
    try:
        assert parent_ready.recv() is True
        with pytest.raises(ProfileLeaseBusyError):
            acquire_profile_lease(base_dir, 23)
    finally:
        parent_release.send(True)
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0

    with acquire_profile_lease(base_dir, 23) as lease:
        assert lease.profile_path == (base_dir / "23").resolve()
        assert lease.lock_path.parent == (base_dir / ".flow2api-locks").resolve()


def test_profile_path_lease_supports_safe_service_owned_profile_paths(tmp_path):
    base_dir = tmp_path / "profiles"
    profile = base_dir / ".onboarding" / "job-safe-1"
    profile.mkdir(parents=True)

    with acquire_profile_path_lease(
        base_dir,
        profile,
        "onboarding-job-safe-1",
    ) as lease:
        assert lease.profile_path == profile.resolve()
        assert (
            lease.lock_path
            == (base_dir / ".flow2api-locks" / "onboarding-job-safe-1.lock").resolve()
        )
        with pytest.raises(ProfileLeaseBusyError):
            acquire_profile_path_lease(
                base_dir,
                profile,
                "onboarding-job-safe-1",
            )

    outside = tmp_path / "outside-profile"
    outside.mkdir()
    with pytest.raises(ValueError, match="outside configured base"):
        acquire_profile_path_lease(base_dir, outside, "outside")
    with pytest.raises(ValueError, match="safe canonical characters"):
        acquire_profile_path_lease(base_dir, profile, "../unsafe")


def test_active_dangling_singleton_lock_reports_busy_without_cleanup(tmp_path):
    base_dir = tmp_path / "profiles"
    profile = _make_profile(base_dir)
    pid = 43123
    _make_singleton_artifacts(profile, pid)
    process_reader = lambda requested_pid: ProcessSnapshot(
        pid=requested_pid,
        start_ticks=900,
        cmdline=("/usr/bin/google-chrome", f"--user-data-dir={profile}"),
    )

    inspection = inspect_singleton_lock(profile, process_reader=process_reader)

    assert inspection.state is SingletonLockState.BUSY
    assert inspection.pid == pid
    assert inspection.busy is True
    assert os.path.lexists(profile / "SingletonLock")
    with acquire_profile_lease(base_dir, 23) as lease:
        with pytest.raises(ProfileBusyError) as error:
            prepare_profile(lease, process_reader=process_reader)
    assert error.value.pid == pid
    assert os.path.lexists(profile / "SingletonLock")
    assert os.path.lexists(profile / "SingletonCookie")
    assert os.path.lexists(profile / "SingletonSocket")


def test_dead_pid_proves_stale_lock_and_removes_only_singleton_artifacts(tmp_path):
    base_dir = tmp_path / "profiles"
    profile = _make_profile(base_dir)
    unrelated = profile / "Preferences"
    unrelated.write_text("keep", encoding="utf-8")
    _make_singleton_artifacts(profile, 43124)

    with acquire_profile_lease(base_dir, 23) as lease:
        report = prepare_profile(lease, process_reader=lambda _pid: None)

    assert report.inspection.state is SingletonLockState.STALE
    assert report.removed == (
        "SingletonCookie",
        "SingletonSocket",
        "SingletonLock",
    )
    assert not os.path.lexists(profile / "SingletonLock")
    assert not os.path.lexists(profile / "SingletonCookie")
    assert not os.path.lexists(profile / "SingletonSocket")
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_live_reused_pid_for_wrong_profile_is_stale_and_safe_to_clean(tmp_path):
    base_dir = tmp_path / "profiles"
    profile = _make_profile(base_dir)
    other_profile = _make_profile(base_dir, token_id=24)
    pid = 43125
    _make_singleton_artifacts(profile, pid)
    process_reader = lambda requested_pid: ProcessSnapshot(
        pid=requested_pid,
        start_ticks=901,
        cmdline=("/usr/bin/google-chrome", f"--user-data-dir={other_profile}"),
    )

    with acquire_profile_lease(base_dir, 23) as lease:
        report = prepare_profile(lease, process_reader=process_reader)

    assert report.inspection.state is SingletonLockState.STALE
    assert report.inspection.reason == "pid_not_owned_by_profile"
    assert report.removed[-1] == "SingletonLock"


def test_foreign_host_lock_is_not_proven_stale_and_is_never_removed(tmp_path):
    base_dir = tmp_path / "profiles"
    profile = _make_profile(base_dir)
    os.symlink("some-other-host-43126", profile / "SingletonLock")
    os.symlink("cookie-secret", profile / "SingletonCookie")

    inspection = inspect_singleton_lock(
        profile,
        process_reader=lambda _pid: pytest.fail("foreign PID must not be inspected"),
    )

    assert inspection.state is SingletonLockState.UNSAFE
    with acquire_profile_lease(base_dir, 23) as lease:
        with pytest.raises(ProfileLockUncertainError):
            prepare_profile(
                lease,
                process_reader=lambda _pid: pytest.fail(
                    "foreign PID must not be inspected"
                ),
            )
    assert os.path.lexists(profile / "SingletonLock")
    assert os.path.lexists(profile / "SingletonCookie")


def test_prepare_profile_does_not_remove_orphan_artifacts_without_stale_lock(tmp_path):
    base_dir = tmp_path / "profiles"
    profile = _make_profile(base_dir)
    os.symlink("cookie-secret", profile / "SingletonCookie")
    os.symlink("/tmp/nonexistent-chrome-socket", profile / "SingletonSocket")

    with acquire_profile_lease(base_dir, 23) as lease:
        report = prepare_profile(lease, process_reader=lambda _pid: None)

    assert report.inspection.state is SingletonLockState.ABSENT
    assert report.removed == ()
    assert os.path.lexists(profile / "SingletonCookie")
    assert os.path.lexists(profile / "SingletonSocket")


def test_proc_helpers_parse_cmdline_and_start_ticks(tmp_path):
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "321"
    process_dir.mkdir(parents=True)
    profile = (tmp_path / "profiles" / "321").resolve()
    cmdline = ("/usr/bin/google-chrome", f"--user-data-dir={profile}")
    (process_dir / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in cmdline) + b"\0")
    stat_fields = ["S"] + ["0"] * 18 + ["98765", "0", "0"]
    (process_dir / "stat").write_text(
        f"321 (chrome worker (test)) {' '.join(stat_fields)}\n",
        encoding="utf-8",
    )

    assert read_proc_cmdline(321, proc_root=proc_root) == cmdline
    assert read_proc_start_ticks(321, proc_root=proc_root) == 98765


def test_verify_process_ownership_requires_exact_profile_and_start_ticks(tmp_path):
    profile = (tmp_path / "profiles" / "23").resolve()
    exact_cmdline = ("chrome", "--user-data-dir", str(profile))

    assert verify_process_ownership(
        500,
        profile,
        expected_start_ticks=1000,
        cmdline_reader=lambda _pid: exact_cmdline,
        start_ticks_reader=lambda _pid: 1000,
    )
    assert not verify_process_ownership(
        500,
        profile,
        expected_start_ticks=999,
        cmdline_reader=lambda _pid: exact_cmdline,
        start_ticks_reader=lambda _pid: 1000,
    )
    assert not verify_process_ownership(
        500,
        profile,
        cmdline_reader=lambda _pid: (
            "chrome",
            f"--user-data-dir={profile}-different",
        ),
        start_ticks_reader=lambda _pid: 1000,
    )
    assert verify_process_ownership(
        500,
        profile,
        cmdline_reader=lambda _pid: (
            "chrome",
            f"--user-data-dir={profile.parent / '..' / profile.parent.name / profile.name}",
        ),
        start_ticks_reader=lambda _pid: 1000,
    )


def test_prepare_profile_treats_noncanonical_equivalent_live_owner_as_busy(tmp_path):
    base_dir = tmp_path / "profiles"
    profile = _make_profile(base_dir)
    pid = 4321
    os.symlink(f"{socket.gethostname()}-{pid}", profile / "SingletonLock")
    noncanonical_profile = profile.parent / ".." / profile.parent.name / profile.name
    process = ProcessSnapshot(
        pid=pid,
        start_ticks=1000,
        cmdline=("chrome", f"--user-data-dir={noncanonical_profile}"),
    )

    with acquire_profile_lease(base_dir, 23) as lease:
        with pytest.raises(ProfileBusyError):
            prepare_profile(lease, process_reader=lambda _pid: process)

    assert os.path.lexists(profile / "SingletonLock")


def test_cookie_reader_selects_deterministically_without_logging_token(tmp_path, capsys):
    profile = _make_profile(tmp_path / "profiles")
    older = "a" * MIN_SESSION_TOKEN_LENGTH
    newer = "z" * (MIN_SESSION_TOKEN_LENGTH + 1)
    cookies = [
        SimpleNamespace(
            name="unrelated", value="secret-unrelated", domain="labs.google", path="/", expires=999
        ),
        SimpleNamespace(
            name="__Secure-next-auth.session-token",
            value=older,
            domain=".labs.google",
            path="/",
            expires=100,
            secure=True,
        ),
        SimpleNamespace(
            name="__Secure-next-auth.session-token",
            value=newer,
            domain="labs.google",
            path="/",
            expires=200,
            secure=True,
        ),
    ]
    calls = []

    def cookie_reader(**kwargs):
        calls.append(kwargs)
        return reversed(cookies)

    assert read_session_token(profile, cookie_reader=cookie_reader) == newer
    assert calls == [
        {
            "cookie_file": str(profile / "Default" / "Cookies"),
            "domain_name": "labs.google",
        }
    ]
    output = capsys.readouterr()
    assert newer not in output.out
    assert newer not in output.err


def test_cookie_reader_prefers_last_used_chrome_profile(tmp_path):
    profile = _make_profile(tmp_path / "profiles")
    (profile / "Default").mkdir()
    (profile / "Profile 1").mkdir()
    (profile / "Local State").write_text(
        '{"profile":{"last_used":"Profile 1"}}',
        encoding="utf-8",
    )
    active_token = "p" * MIN_SESSION_TOKEN_LENGTH
    calls = []

    def cookie_reader(**kwargs):
        calls.append(kwargs)
        if kwargs["cookie_file"] == str(profile / "Profile 1" / "Cookies"):
            return [
                SimpleNamespace(
                    name="__Secure-next-auth.session-token",
                    value=active_token,
                    domain="labs.google",
                    path="/",
                    expires=200,
                    secure=True,
                )
            ]
        return []

    assert read_session_token(profile, cookie_reader=cookie_reader) == active_token
    assert calls == [
        {
            "cookie_file": str(profile / "Profile 1" / "Cookies"),
            "domain_name": "labs.google",
        }
    ]


def test_cookie_reader_ignores_short_duplicate_when_valid_token_exists(tmp_path):
    profile = _make_profile(tmp_path / "profiles")
    valid = "v" * MIN_SESSION_TOKEN_LENGTH
    cookies = [
        SimpleNamespace(
            name="__Secure-next-auth.session-token",
            value="undefined",
            domain="labs.google",
            path="/",
            expires=999,
        ),
        SimpleNamespace(
            name="__Secure-next-auth.session-token",
            value=valid,
            domain="labs.google",
            path="/",
            expires=100,
        ),
    ]

    assert read_session_token(profile, cookie_reader=lambda **_kwargs: cookies) == valid


def test_cookie_reader_rejects_missing_or_short_tokens_without_disclosing_value(tmp_path):
    profile = _make_profile(tmp_path / "profiles")
    short_token = "sensitive-short-token"

    with pytest.raises(SessionTokenNotFoundError):
        read_session_token(profile, cookie_reader=lambda **_kwargs: [])
    with pytest.raises(SessionTokenTooShortError) as error:
        read_session_token(
            profile,
            cookie_reader=lambda **_kwargs: [
                SimpleNamespace(
                    name="__Secure-next-auth.session-token",
                    value=short_token,
                    domain="labs.google",
                    path="/",
                    expires=100,
                )
            ],
        )
    assert short_token not in str(error.value)
