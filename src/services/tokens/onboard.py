"""Onboarding orchestration primitives: global onboard lease, foreground Chrome
launch with process-group cleanup on timeout/crash, and profile verification.

This module is the browser-facing core composed by the higher-level
``onboard_new`` / ``onboard_existing`` flows in Task 3. It deliberately keeps
the surface small and synchronous (verification aside) so it can be unit-tested
without a real XRDP display or Chrome binary.

The pure browser-argv helpers (``SetupRuntime``, ``build_browser_command``) are
imported from ``scripts/setup_keepalive_profile.py`` rather than reimplemented,
so the onboarding argv stays identical to the verified setup helper.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.account_identity import VerifiedAccountSnapshot  # noqa: E402
from src.services.keepalive.profile import (  # noqa: E402
    ProfileLease,
    ProfileLeaseBusyError,
    acquire_profile_path_lease,
    read_session_token,
)
from src.services.tokens.account_identity import (  # noqa: E402
    inspect_account_identity,
)
from scripts.setup_keepalive_profile import (  # noqa: E402
    SetupRuntime,
    build_browser_command,
)

__all__ = [
    "DEFAULT_LOGIN_TIMEOUT_SECONDS",
    "FLOW_ROOT_URL",
    "OnboardError",
    "acquire_onboard_global_lease",
    "launch_chrome",
    "verify_profile",
]

ONBOARD_GLOBAL_LOCK_NAME = "onboarding-global"
DEFAULT_LOGIN_TIMEOUT_SECONDS = 1800  # 30 minutes per spec §6.5
FLOW_ROOT_URL = "https://labs.google/fx/tools/flow"

# Type alias kept loose to avoid hard-dependency on a specific launcher class.
Launcher = Callable[..., Any]


class OnboardError(Exception):
    """Raised when an onboarding step fails with a stable, Agent-facing code.

    Codes (per spec §9.1):
    - ``onboard_busy``: another onboard holds the global lease.
    - ``browser_launch``: Chrome could not be started at all.
    - ``login_timeout``: Chrome was not closed within the configured timeout.
    - ``browser_crashed``: Chrome exited with a non-zero return code.
    - ``cookie_missing``: the profile cookie could not be read.
    - ``session_body`` / propagated inspect code: identity inspection failed.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def acquire_onboard_global_lease(base_dir: Union[Path, str]) -> ProfileLease:
    """Acquire the global onboarding lease.

    flocks ``<base_dir>/.flow2api-locks/onboarding-global.lock`` so two onboards
    cannot open Chrome on the same XRDP display at the same time (spec §6.0/§7.3
    cross-talk guard). ``base_dir`` is used both as the flock root and as the
    validated profile path: the lock is per-display, not per-token, so it lives
    next to the per-token locks but carries a fixed name.
    """
    try:
        return acquire_profile_path_lease(
            base_dir, base_dir, ONBOARD_GLOBAL_LOCK_NAME
        )
    except ProfileLeaseBusyError as error:
        raise OnboardError(
            "onboard_busy", "another onboard session is running"
        ) from error


def _kill_process_group(proc: Any) -> None:
    """SIGTERM → 5s grace → SIGKILL on the whole Chrome process group.

    ``start_new_session=True`` makes Chrome its own session/group leader, so
    ``os.getpgid(proc.pid)`` resolves to the Chrome group and ``os.killpg``
    reaches every renderer/GPU child it forked. This mitigates the residual
    process-tree bug documented in handoff §7.8.

    Best-effort cleanup only: if the PID has already been reaped (the crash
    branch calls this after ``proc.wait()`` already returned) the OS may have
    recycled it to an unrelated process by the time we signal it, so a
    ``PermissionError`` from an unowned pgid is treated the same as an
    already-gone process rather than escaping as a raw OS error.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5 if sig == signal.SIGTERM else 2)
            return
        except subprocess.TimeoutExpired:
            continue


def _wait_for_chrome_exit(proc: Any, timeout_seconds: int) -> None:
    """Block for Chrome to exit; classify timeout/crash and clean up the group.

    On timeout the process is still alive, so the group kill targets a live,
    correctly-identified pgid. On a non-zero exit the process has already been
    reaped by ``wait()``; cleanup there is defensive (§6.5) and best-effort.
    """
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        raise OnboardError(
            "login_timeout", f"Chrome not closed within {timeout_seconds}s"
        )

    returncode = getattr(proc, "returncode", None)
    if not isinstance(returncode, int) or returncode != 0:
        _kill_process_group(proc)
        raise OnboardError("browser_crashed", f"Chrome exited with code {returncode}")


def launch_chrome(
    runtime: SetupRuntime,
    profile_path: Path,
    display: str,
    flow_url: str,
    *,
    timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    launcher: Optional[Launcher] = None,
) -> None:
    """Launch Chrome in the foreground and block until the user closes it.

    Chrome is started with ``start_new_session=True`` so the entire process
    tree can be reaped on timeout or crash. ``launcher`` defaults to
    ``subprocess.Popen`` looked up at call time (so tests may monkeypatch
    ``subprocess.Popen`` on this module). On timeout the whole group receives
    SIGTERM→SIGKILL; non-zero exit raises ``browser_crashed``.
    """
    if launcher is None:
        launcher = subprocess.Popen

    env = os.environ.copy()
    env["DISPLAY"] = display
    command = build_browser_command(runtime, profile_path, flow_url)

    try:
        proc = launcher(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as error:
        raise OnboardError(
            "browser_launch",
            f"Chrome launch failed ({type(error).__name__})",
        ) from error

    _wait_for_chrome_exit(proc, timeout_seconds)


async def verify_profile(
    profile_path: Path,
    flow_client: Any,
    *,
    session_reader: Callable[[Path], str] = read_session_token,
    identity_inspector: Callable[
        [Any, str], Awaitable[VerifiedAccountSnapshot]
    ] = inspect_account_identity,
) -> VerifiedAccountSnapshot:
    """Read the profile cookie ST and run the full identity inspection.

    Failures are translated into ``OnboardError``:
    - ``cookie_missing``: cookie unreadable / absent / too short.
    - propagated inspector code (e.g. ``session_rejected``, ``grant_expired``):
      identity inspection raised an ``AccountIdentityError`` with its own code.
    - ``session_body``: fallback when the inspector raises without a code.
    """
    try:
        st = session_reader(profile_path)
    except Exception as error:
        raise OnboardError(
            "cookie_missing",
            f"ST unreadable ({type(error).__name__})",
        ) from error

    try:
        snapshot = await identity_inspector(flow_client, st)
    except Exception as error:
        code = getattr(error, "code", None) or "session_body"
        raise OnboardError(code, str(error) or code) from error

    return snapshot
