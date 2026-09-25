from pathlib import Path
import signal

from src.services.keepalive.profile import ProcessSnapshot, SingletonLockInspection, SingletonLockState
from scripts import cleanup_captcha_chrome as cleanup

is_managed_captcha_chrome = cleanup.is_managed_captcha_chrome


def test_cleanup_only_targets_managed_chrome_for_exact_profile():
    profile = Path("/opt/flow2api-profiles/ultra")
    managed = ProcessSnapshot(
        pid=123, start_ticks=456,
        cmdline=(
            "/opt/google/chrome/chrome",
            "--remote-allow-origins=*",
            "--remote-debugging-port=12345",
            f"--user-data-dir={profile}",
        ),
    )
    assert is_managed_captcha_chrome(managed, profile)
    assert is_managed_captcha_chrome(
        ProcessSnapshot(123, 456, (" ".join(managed.cmdline),)), profile
    )
    assert not is_managed_captcha_chrome(managed, Path("/opt/flow2api-profiles/21"))
    assert not is_managed_captcha_chrome(
        ProcessSnapshot(123, 456, ("/opt/google/chrome/chrome", f"--user-data-dir={profile}")),
        profile,
    )


def test_stop_only_signals_same_live_lock_owner(monkeypatch):
    profile = Path("/opt/flow2api-profiles/ultra")
    owner = ProcessSnapshot(
        123, 456,
        (f"/opt/google/chrome/chrome --remote-allow-origins=* "
         f"--user-data-dir={profile} --remote-debugging-port=12345",),
    )
    lock = SingletonLockInspection(
        SingletonLockState.BUSY, profile, "profile_owned_by_live_pid",
        pid=123, lock_inode=789,
    )
    alive = True
    signalled = []

    def fake_kill(pid, sig):
        nonlocal alive
        signalled.append((pid, sig))
        alive = False

    monkeypatch.setattr(cleanup, "inspect_singleton_lock", lambda _profile: lock)
    monkeypatch.setattr(cleanup, "read_process_snapshot", lambda _pid: owner if alive else None)
    monkeypatch.setattr(cleanup.os, "kill", fake_kill)
    monkeypatch.setattr(cleanup.time, "sleep", lambda _seconds: None)

    assert cleanup.stop_orphan_captcha_chrome(profile)
    assert signalled == [(123, signal.SIGTERM)]


def test_stop_refuses_changed_lock(monkeypatch):
    profile = Path("/opt/flow2api-profiles/ultra")
    owner = ProcessSnapshot(
        123, 456,
        ("/opt/google/chrome/chrome", "--remote-allow-origins=*",
         f"--user-data-dir={profile}", "--remote-debugging-port=12345"),
    )
    old_lock = SingletonLockInspection(
        SingletonLockState.BUSY, profile, "profile_owned_by_live_pid",
        pid=123, lock_inode=789,
    )
    new_lock = SingletonLockInspection(
        SingletonLockState.BUSY, profile, "profile_owned_by_live_pid",
        pid=999, lock_inode=999,
    )
    inspections = iter((old_lock, new_lock))
    signalled = []
    monkeypatch.setattr(cleanup, "inspect_singleton_lock", lambda _profile: next(inspections))
    monkeypatch.setattr(cleanup, "read_process_snapshot", lambda _pid: owner)
    monkeypatch.setattr(cleanup.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    assert not cleanup.stop_orphan_captcha_chrome(profile)
    assert signalled == []
