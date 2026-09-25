#!/usr/bin/env python3
"""Stop a managed captcha Chrome left outside flow2api.service's cgroup.

Chrome launches in a user app scope, so systemd cannot kill it after the API
process exceeds TimeoutStopSec. Run only from flow2api.service ExecStopPost.
"""

from __future__ import annotations

import os
import argparse
import shlex
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import config  # noqa: E402
from src.services.keepalive.profile import (  # noqa: E402
    ProcessSnapshot,
    SingletonLockState,
    inspect_singleton_lock,
    read_process_snapshot,
)


def is_managed_captcha_chrome(process: ProcessSnapshot, profile: Path) -> bool:
    args = process.cmdline
    if len(args) == 1:
        try:
            args = tuple(shlex.split(args[0]))
        except ValueError:
            return False
    return (
        bool(args)
        and Path(args[0]).name in {"chrome", "google-chrome", "google-chrome-stable"}
        and f"--user-data-dir={profile}" in args
        and "--remote-allow-origins=*" in args
        and any(arg.startswith("--remote-debugging-port=") for arg in args)
    )


def stop_orphan_captcha_chrome(profile: Path) -> bool:
    inspection = inspect_singleton_lock(profile)
    if inspection.state is not SingletonLockState.BUSY or inspection.pid is None:
        return False
    process = read_process_snapshot(inspection.pid)
    if process is None or not is_managed_captcha_chrome(process, inspection.profile_path):
        return False

    # Check the lock and PID identity again immediately before signalling.
    current_lock = inspect_singleton_lock(profile)
    current_process = read_process_snapshot(process.pid)
    if (
        current_lock.state is not SingletonLockState.BUSY
        or current_lock.pid != process.pid
        or current_lock.lock_inode != inspection.lock_inode
        or current_process is None
        or current_process.start_ticks != process.start_ticks
    ):
        return False

    os.kill(process.pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.1)
        current_process = read_process_snapshot(process.pid)
        if current_process is None or current_process.start_ticks != process.start_ticks:
            return True

    current_process = read_process_snapshot(process.pid)
    if current_process is not None and current_process.start_ticks == process.start_ticks:
        os.kill(process.pid, signal.SIGKILL)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminate", action="store_true")
    args = parser.parse_args()
    if config.captcha_persistent_profile_enabled:
        profile_path = Path(config.captcha_persistent_profile_path).expanduser().resolve()
        if args.terminate:
            stopped = stop_orphan_captcha_chrome(profile_path)
            print("captcha_chrome_stopped" if stopped else "captcha_chrome_not_owned")
        else:
            inspection = inspect_singleton_lock(profile_path)
            print(f"captcha_chrome_profile_state={inspection.state.value}")
