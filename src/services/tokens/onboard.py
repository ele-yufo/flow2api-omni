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
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.account_identity import (  # noqa: E402
    VerifiedAccountSnapshot,
    normalize_account_email,
)
from src.core.models import Token  # noqa: E402
from src.core.repositories.token_lifecycle_repository import (  # noqa: E402
    PublishOutcome,
    TokenLifecycleRepository,
)
from src.core.token_states import TOKEN_REASON_ONBOARDING_PENDING  # noqa: E402
from src.services.keepalive.profile import (  # noqa: E402
    ProfileLease,
    ProfileLeaseBusyError,
    acquire_profile_lease,
    acquire_profile_path_lease,
    canonical_profile_path,
    read_session_token,
)
from src.services.tokens.account_identity import (  # noqa: E402
    inspect_account_identity,
)
from src.services.tokens.project_pool import ensure_project_pool  # noqa: E402
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
    "try_readonly_validate",
    "onboard_new",
    "onboard_existing",
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

    ``build_browser_command`` (argv construction, including proxy validation
    via ``validate_proxy_server``) runs inside this same try block: a
    malformed ``runtime.proxy`` (e.g. embedded userinfo) raises a raw
    ``ValueError`` there, which must surface as ``OnboardError("browser_launch")``
    like any other launch failure rather than escaping unwrapped. The error
    message never echoes the proxy string, since it may carry credentials.
    """
    if launcher is None:
        launcher = subprocess.Popen

    env = os.environ.copy()
    env["DISPLAY"] = display

    try:
        command = build_browser_command(runtime, profile_path, flow_url)
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


# ---------------------------------------------------------------------------
# onboard_new / onboard_existing: end-user onboarding flows (Task 3)
#
# Both flows always publish with runtime_mode="persistent" (spec: 只发
# persistent) and keepalive_enabled=True. Any verification/publish failure
# must leave the DB and the profile directory exactly as they were before the
# call started (spec: 验证不过 = 什么都不改) -- see ``_compensate_new_account``
# and ``_restore_keepalive_state`` below.
# ---------------------------------------------------------------------------


async def try_readonly_validate(
    profile_path: Path, flow_client: Any
) -> Optional[VerifiedAccountSnapshot]:
    """Validate a profile's saved session without launching Chrome.

    Reuses ``verify_profile`` as-is: a still-valid cookie yields a snapshot,
    any ``OnboardError`` (missing cookie, rejected session, expired grant,
    ...) is swallowed and reported as ``None`` so the caller can fall back to
    an interactive re-login instead of treating it as fatal.
    """
    try:
        return await verify_profile(profile_path, flow_client)
    except OnboardError:
        return None


def _require_email_matches(
    snapshot: VerifiedAccountSnapshot, expected_normalized_email: str
) -> None:
    """Reject a login whose account does not match the expected identity."""
    if normalize_account_email(snapshot.email) != expected_normalized_email:
        raise OnboardError(
            "identity_mismatch",
            "logged-in account email does not match the expected account",
        )


async def _insert_onboarding_token(db: Any, snapshot: VerifiedAccountSnapshot) -> int:
    """INSERT the token + lifecycle skeleton using the real ST from the browser.

    ``tokens.st`` is ``NOT NULL UNIQUE`` -- this must only ever be called with
    a real, verified ST, never a placeholder. ``Database.add_token`` performs
    the tokens/token_stats/token_lifecycle inserts atomically.
    """
    return await db.add_token(
        Token(
            st=snapshot.st,
            email=snapshot.email,
            name=snapshot.name or "",
            is_active=False,
            ban_reason=TOKEN_REASON_ONBOARDING_PENDING,
        )
    )


async def _run_project_pool(
    db: Any, flow_client: Any, token_id: int, pool_size: int
) -> None:
    """Provision the active project pool, translating failures to a stable code."""
    try:
        token = await db.get_token(token_id)
        await ensure_project_pool(db, flow_client, token, pool_size)
    except Exception as error:
        raise OnboardError("project_pool_failed", str(error)) from error


async def _publish_account(
    db: Any,
    token_id: int,
    snapshot: VerifiedAccountSnapshot,
    observed_at: Any,
    business_enabled: bool,
) -> PublishOutcome:
    """Publish through ``TokenLifecycleRepository``, always persistent + keepalive on."""
    repo = TokenLifecycleRepository(db)
    try:
        return await repo.publish_verified_account(
            token_id=token_id,
            snapshot=snapshot,
            runtime_mode="persistent",
            keepalive_enabled=True,
            business_enabled=business_enabled,
            observed_at=observed_at,
        )
    except Exception as error:
        raise OnboardError("publish_failed", str(error)) from error


async def _compensate_new_account(
    db: Any, token_id: Optional[int], profile_path: Optional[Path]
) -> None:
    """Undo a failed new-account onboard: drop the placeholder row + profile dir.

    Best-effort: a failed ``delete_token`` here leaves a stray
    ``onboarding_pending`` row rather than raising over the top of the
    original failure, since the original error is the one the caller needs.
    """
    if token_id is not None:
        try:
            await db.delete_token(token_id)
        except Exception:
            pass
    if profile_path is not None:
        shutil.rmtree(profile_path, ignore_errors=True)


async def onboard_new(
    *,
    email: str,
    runtime: SetupRuntime,
    display: str,
    db: Any,
    flow_client: Any,
    pool_size: int = 4,
    observed_at: Any,
    business_enabled: bool = True,
) -> PublishOutcome:
    """New-account onboarding: temp-profile login -> INSERT -> pool -> rename -> publish.

    Login happens in a throwaway ``.onboarding/<random>`` profile so the real
    ST can be read before any DB row is created. Only after identity and
    project-pool provisioning both succeed is the profile renamed to its
    canonical ``<profile_base>/<token_id>`` path and the account published. A
    failure at any step compensates by deleting the placeholder token row and
    removing the profile directory (temp or renamed, whichever exists).
    """
    global_lease = acquire_onboard_global_lease(runtime.profile_base)
    temp_profile = Path(runtime.profile_base) / ".onboarding" / secrets.token_hex(16)
    token_id: Optional[int] = None
    profile_path: Optional[Path] = temp_profile
    try:
        temp_profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        launch_chrome(runtime, temp_profile, display, FLOW_ROOT_URL)
        snapshot = await verify_profile(temp_profile, flow_client)
        _require_email_matches(snapshot, normalize_account_email(email))

        token_id = await _insert_onboarding_token(db, snapshot)
        await _run_project_pool(db, flow_client, token_id, pool_size)

        final_profile = canonical_profile_path(runtime.profile_base, token_id)
        os.rename(temp_profile, final_profile)
        profile_path = final_profile

        return await _publish_account(db, token_id, snapshot, observed_at, business_enabled)
    except Exception:
        await _compensate_new_account(db, token_id, profile_path)
        raise
    finally:
        global_lease.release()


async def _pause_keepalive_if_enabled(db: Any, token_id: int) -> bool:
    """Disable keepalive so the sidecar releases the profile lease in time.

    Returns whether it was previously enabled, so a failed onboard can
    restore the exact prior state (spec: 验证不过 = 什么都不改).
    """
    lifecycle = await db.get_token_lifecycle(token_id)
    was_enabled = bool(lifecycle and lifecycle.keepalive_enabled)
    if was_enabled:
        await db.set_token_desired_state(token_id, keepalive_enabled=False)
    return was_enabled


async def _restore_keepalive_state(
    db: Any, token_id: int, previous_keepalive: bool
) -> None:
    """Best-effort restore of the pre-onboard keepalive flag after a failure."""
    if not previous_keepalive:
        return
    try:
        await db.set_token_desired_state(token_id, keepalive_enabled=True)
    except Exception:
        pass


def _poll_profile_lease(
    base_dir: Union[Path, str], token_id: int, timeout_seconds: int
) -> ProfileLease:
    """Poll ``acquire_profile_lease`` every second until the sidecar releases it.

    The real upper bound on sidecar release is 15s reconcile + 20s shutdown
    timeout = 35s; ``timeout_seconds`` defaults to 40s to clear that with margin.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return acquire_profile_lease(base_dir, token_id)
        except ProfileLeaseBusyError as error:
            if time.monotonic() >= deadline:
                raise OnboardError(
                    "profile_busy", f"profile lease busy for token {token_id}"
                ) from error
            time.sleep(1)


async def _revalidate_or_relogin(
    profile_path: Path, flow_client: Any, runtime: SetupRuntime, display: str
) -> VerifiedAccountSnapshot:
    """Try the read-only path first; only launch Chrome if that path fails."""
    snapshot = await try_readonly_validate(profile_path, flow_client)
    if snapshot is not None:
        return snapshot
    launch_chrome(runtime, profile_path, display, FLOW_ROOT_URL)
    return await verify_profile(profile_path, flow_client)


async def onboard_existing(
    *,
    token_id: int,
    runtime: SetupRuntime,
    display: str,
    db: Any,
    flow_client: Any,
    pool_size: int = 4,
    observed_at: Any,
    business_enabled: bool = True,
    lease_wait_seconds: int = 40,
) -> PublishOutcome:
    """Existing-account onboarding: readonly-validate first, re-login only if needed.

    Re-login (when required) happens IN the account's own canonical profile --
    never archived, renamed, or replaced (spec: 旧号不 archive/不覆盖). Keepalive
    is paused before the profile lease is acquired so the sidecar has time to
    release its lock. From that pause onward -- including a profile-lease-poll
    timeout, which previously escaped uncompensated -- every exit path restores
    the pre-call keepalive flag (spec: 验证不过 = 什么都不改) except a successful
    publish, which already sets ``keepalive_enabled=True`` itself and must not
    have that overwritten by a restore of the (possibly ``False``) prior value.
    """
    global_lease = acquire_onboard_global_lease(runtime.profile_base)
    try:
        token = await db.get_token(token_id)
        if token is None:
            raise OnboardError("not_found", f"token {token_id} not found")
        expected_email = normalize_account_email(token.email)

        previous_keepalive = await _pause_keepalive_if_enabled(db, token_id)
        return await _onboard_existing_under_lease(
            token_id=token_id,
            runtime=runtime,
            display=display,
            db=db,
            flow_client=flow_client,
            pool_size=pool_size,
            observed_at=observed_at,
            business_enabled=business_enabled,
            lease_wait_seconds=lease_wait_seconds,
            expected_email=expected_email,
            previous_keepalive=previous_keepalive,
        )
    finally:
        global_lease.release()


async def _onboard_existing_under_lease(
    *,
    token_id: int,
    runtime: SetupRuntime,
    display: str,
    db: Any,
    flow_client: Any,
    pool_size: int,
    observed_at: Any,
    business_enabled: bool,
    lease_wait_seconds: int,
    expected_email: str,
    previous_keepalive: bool,
) -> PublishOutcome:
    """Run the lease-guarded body of :func:`onboard_existing`.

    Keepalive was already paused by the caller, so every exit path here -- including
    a profile-lease-poll timeout -- restores the pre-call flag, except a successful
    publish (which sets ``keepalive_enabled=True`` itself and must not be reverted).
    """
    published = False
    lease: Optional[ProfileLease] = None
    try:
        profile_path = canonical_profile_path(runtime.profile_base, token_id)
        lease = _poll_profile_lease(runtime.profile_base, token_id, lease_wait_seconds)
        snapshot = await _revalidate_or_relogin(
            profile_path, flow_client, runtime, display
        )
        _require_email_matches(snapshot, expected_email)
        await _run_project_pool(db, flow_client, token_id, pool_size)
        outcome = await _publish_account(
            db, token_id, snapshot, observed_at, business_enabled
        )
        published = True
        return outcome
    finally:
        if lease is not None:
            lease.release()
        if not published:
            await _restore_keepalive_state(db, token_id, previous_keepalive)
