#!/usr/bin/env python3
"""Safely provision and verify a configured keepalive Chrome profile."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Awaitable, Callable, Mapping, NamedTuple
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.database import Database  # noqa: E402
from src.services.keepalive.profile import (  # noqa: E402
    ProfileLeaseBusyError,
    SingletonLockState,
    acquire_profile_lease,
    canonical_profile_path,
    inspect_singleton_lock,
    read_session_token,
    validate_proxy_server,
)
from src.services.proxy_manager import ProxyManager  # noqa: E402
from src.services.tokens.account_identity import (  # noqa: E402
    inspect_account_identity,
    normalize_account_email,
)

_TOKEN_ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")
_DISPLAY_PATTERN = re.compile(r":[0-9]+(?:\.[0-9]+)?\Z")
FLOW_BASE_URL = "https://labs.google/fx/tools/flow"


class SetupSafetyError(RuntimeError):
    """Raised before launch when profile ownership cannot be proven safe."""


class SetupValidationError(RuntimeError):
    """Raised after Chrome exits when the resulting profile is not valid."""


class SetupRuntime(NamedTuple):
    profile_base: Path
    proxy: str
    browser_executable: Path


class SetupResult(NamedTuple):
    token_id: int
    email: str
    credits: int
    user_paygate_tier: str | None


def canonical_token_id(raw_value: object) -> int:
    """Parse only canonical positive ASCII decimal token IDs."""

    value = str(raw_value)
    if not value.isascii() or _TOKEN_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("token ID must use canonical positive decimal form")
    return int(value)


def _argparse_token_id(raw_value: str) -> int:
    try:
        return canonical_token_id(raw_value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def resolve_display(explicit_display: str | None, environ: Mapping[str, str]) -> str:
    """Require an explicit display argument or the caller's visible DISPLAY."""

    display = explicit_display if explicit_display is not None else environ.get("DISPLAY")
    value = str(display or "").strip()
    if _DISPLAY_PATTERN.fullmatch(value) is None:
        raise ValueError("display must be an X display such as :11 or :11.0")
    return value


def _load_runtime_dependencies():
    from src.core.config import config
    from src.services.flow_client import FlowClient

    return config, FlowClient


def resolve_runtime(config_object, environ: Mapping[str, str]) -> SetupRuntime:
    """Resolve host-controlled profile, proxy, and browser configuration."""

    profile_base_value = str(config_object.keepalive_browser_profile_base or "").strip()
    if not profile_base_value:
        raise ValueError("keepalive browser profile base is not configured")
    try:
        profile_base = Path(profile_base_value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("keepalive profile path configuration is invalid") from error
    proxy = str(config_object.keepalive_browser_proxy or "").strip()
    executable_value = str(
        environ.get("BROWSER_EXECUTABLE_PATH", "") or "/usr/bin/google-chrome-stable"
    ).strip()
    if not executable_value:
        raise ValueError("browser executable is not configured")
    try:
        browser_executable = Path(executable_value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("browser executable path configuration is invalid") from error
    return SetupRuntime(profile_base, proxy, browser_executable)


def build_browser_command(
    runtime: SetupRuntime,
    profile_path: Path,
    flow_url: str,
) -> list[str]:
    command = [
        str(runtime.browser_executable),
        f"--user-data-dir={profile_path}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-mode",
    ]
    proxy = validate_proxy_server(runtime.proxy)
    if proxy is not None:
        command.append(f"--proxy-server={proxy}")
    command.append(flow_url)
    return command


def _flow_url(project_id: object) -> str:
    value = str(project_id or "").strip()
    if not value:
        return FLOW_BASE_URL
    return f"{FLOW_BASE_URL}/project/{quote(value, safe='')}"


def _assert_lock_absent(profile_path: Path, phase: str) -> None:
    try:
        inspection = inspect_singleton_lock(profile_path)
    except (OSError, RuntimeError, ValueError) as error:
        raise SetupSafetyError(f"{phase}: profile lock inspection failed") from error
    if inspection.state is SingletonLockState.ABSENT:
        return
    raise SetupSafetyError(
        f"{phase}: SingletonLock is {inspection.state.value} ({inspection.reason}); "
        "close the exact profile owner and resolve the artifact manually"
    )


async def setup_profile(
    token_id: int,
    *,
    display: str,
    runtime: SetupRuntime,
    db: object,
    flow_client: object,
    launcher: Callable[..., object] = subprocess.run,
    session_reader: Callable[[Path], str] = read_session_token,
    identity_inspector: Callable[[object, str], Awaitable[object]] = inspect_account_identity,
) -> SetupResult:
    """Launch Chrome in the foreground under an exclusive profile lease and verify it."""

    canonical_id = canonical_token_id(token_id)
    visible_display = resolve_display(display, {})
    try:
        profile_path = canonical_profile_path(runtime.profile_base, canonical_id)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SetupSafetyError("profile path validation failed") from error

    if not runtime.browser_executable.is_file() or not os.access(
        runtime.browser_executable, os.X_OK
    ):
        raise SetupSafetyError("configured browser executable is missing or not executable")

    token = await db.get_token(canonical_id)
    if token is None:
        raise SetupValidationError(f"token ID {canonical_id} does not exist")
    expected_email = normalize_account_email(getattr(token, "email", ""))
    if not expected_email:
        raise SetupValidationError("token account identity is missing")

    lifecycle = await db.get_token_lifecycle(canonical_id)
    bound_email = normalize_account_email(
        getattr(lifecycle, "verified_email", "") if lifecycle is not None else ""
    )
    if bound_email and bound_email != expected_email:
        raise SetupValidationError("stored profile binding does not match token identity")

    try:
        lease = acquire_profile_lease(runtime.profile_base, canonical_id)
    except ProfileLeaseBusyError as error:
        raise SetupSafetyError("service lease is busy for this profile") from error
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SetupSafetyError(
            f"service lease could not be acquired ({type(error).__name__})"
        ) from error

    try:
        try:
            profile_path.mkdir(mode=0o700, parents=True, exist_ok=True)
            profile_path.chmod(0o700)
            _assert_lock_absent(profile_path, "before launch")
        except SetupSafetyError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise SetupSafetyError("profile preparation failed") from error

        environment = os.environ.copy()
        environment["DISPLAY"] = visible_display
        command = build_browser_command(runtime, profile_path, _flow_url(token.current_project_id))
        try:
            completed = launcher(
                command,
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as error:
            raise SetupValidationError(
                f"Chrome launch failed ({type(error).__name__})"
            ) from error
        return_code = getattr(completed, "returncode", None)
        if not isinstance(return_code, int) or return_code != 0:
            raise SetupValidationError(
                f"Chrome exited unsuccessfully (return code {return_code})"
            )

        _assert_lock_absent(profile_path, "after exit")
        try:
            session_token = session_reader(profile_path)
        except Exception as error:
            raise SetupValidationError(
                f"browser cookie session is unreadable ({type(error).__name__})"
            ) from error

        try:
            identity = await identity_inspector(flow_client, session_token)
        except Exception as error:
            raise SetupValidationError(
                f"browser session identity verification failed ({type(error).__name__})"
            ) from error

        observed_email = normalize_account_email(getattr(identity, "normalized_email", ""))
        if not observed_email:
            observed_email = normalize_account_email(getattr(identity, "email", ""))
        if observed_email != expected_email or (bound_email and observed_email != bound_email):
            raise SetupValidationError("browser session identity mismatch")

        credits = getattr(identity, "credits", None)
        if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
            raise SetupValidationError("browser session returned invalid credits")
        tier = getattr(identity, "user_paygate_tier", None)
        normalized_tier = str(tier).strip() if isinstance(tier, str) and tier.strip() else None
        return SetupResult(canonical_id, observed_email, credits, normalized_tier)
    finally:
        lease.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open a configured keepalive profile for login and verify its identity"
    )
    parser.add_argument("token_id", type=_argparse_token_id)
    parser.add_argument(
        "display",
        nargs="?",
        help="visible X display; defaults to the caller's DISPLAY",
    )
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_object, flow_client_class = _load_runtime_dependencies()
    except Exception as error:
        print(
            "[keepalive-setup] ERROR runtime initialization failed "
            f"({type(error).__name__})",
            file=sys.stderr,
        )
        return 1

    try:
        display = resolve_display(args.display, os.environ)
        runtime = resolve_runtime(config_object, os.environ)
        db = Database()
        flow_client = flow_client_class(ProxyManager(db), db)
        print(
            "[keepalive-setup] opening configured profile "
            f"id={args.token_id} display={display}"
        )
        print("[keepalive-setup] log in to the expected Google account, open Flow, then close Chrome completely")
        result = await setup_profile(
            args.token_id,
            display=display,
            runtime=runtime,
            db=db,
            flow_client=flow_client,
        )
    except (ValueError, SetupSafetyError, SetupValidationError) as error:
        print(f"[keepalive-setup] ERROR {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"[keepalive-setup] ERROR operational failure ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1

    print(
        "[keepalive-setup] VERIFIED "
        f"id={result.token_id} email={result.email} credits={result.credits} "
        f"tier={result.user_paygate_tier or 'unknown'}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
