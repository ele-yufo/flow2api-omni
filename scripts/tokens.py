#!/usr/bin/env python3
"""tokens CLI — an Agent's tool for managing flow2api keepalive accounts.

Design constraints (see docs/superpowers/specs for the full spec):
- JSON-only output. Nothing human-readable is ever printed; every line is one
  JSON object an Agent can parse. Errors are ``{"error": {"code", "message",
  "detail"}}``.
- Stable exit codes (see ``ExitCode``) so a calling Agent can branch on
  ``returncode`` without parsing stderr.
- Every write subcommand supports ``--dry-run`` (preview only, no writes).
- Never reads or prints ST/AT credentials; credential export stays in the
  existing admin HTTP API.
- ``keepalive`` only ever sets ``runtime_mode="persistent"`` — there is no
  ``--mode`` flag (the fleet no longer runs "warm" mode operationally).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.setup_keepalive_profile import resolve_display, resolve_runtime  # noqa: E402
from src.core.database import Database  # noqa: E402
from src.core.token_states import TOKEN_REASON_MANUAL_DISABLED  # noqa: E402
from src.services.tokens.onboard import (  # noqa: E402
    DEFAULT_LOGIN_TIMEOUT_SECONDS,
    OnboardError,
    onboard_existing,
    onboard_new,
)


class ExitCode(IntEnum):
    """Stable process exit codes an Agent can branch on without stderr."""

    OK = 0
    ARG_ERROR = 2
    NOT_FOUND = 3
    CONFLICT = 4
    VALIDATION_FAILED = 5
    PUBLISH_FAILED = 6
    BUSY = 7
    INTERNAL = 70


def emit_json(obj) -> None:
    """Print one JSON object as a single stdout line."""
    print(json.dumps(obj, default=str, ensure_ascii=False))


def emit_error(
    code: str,
    message: str,
    detail: dict | None = None,
    exit_code: ExitCode = ExitCode.INTERNAL,
) -> int:
    """Print the standard JSON error envelope and return the matching exit code."""
    emit_json({"error": {"code": code, "message": message, "detail": detail or {}}})
    return int(exit_code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokens", description="Flow2API token keepalive management (Agent CLI, JSON-only output)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser(
        "status",
        help="show keepalive-enabled tokens' health (JSON)",
        description="Show keepalive-enabled tokens' health as JSON. Never prints credentials.",
    )
    p_status.add_argument("--token-id", type=int, default=None, help="filter to one token id")

    p_onboard = sub.add_parser(
        "onboard",
        help="onboard a new account or re-login an existing one (foreground XRDP login)",
        description="Onboard/relogin a token via a foreground XRDP Chrome login. Emits phased JSON.",
    )
    grp = p_onboard.add_mutually_exclusive_group(required=True)
    grp.add_argument("--email", help="onboard a brand-new account with this expected email")
    grp.add_argument("--token-id", type=int, help="re-login an existing token by id")
    p_onboard.add_argument("--display", default=None, help="X display for foreground Chrome (default: $DISPLAY)")
    p_onboard.add_argument("--dry-run", action="store_true", help="preview only, no browser launch or writes")

    p_enable = sub.add_parser("enable", help="enable business pool (JSON)")
    p_enable.add_argument("--token-id", type=int, required=True)
    p_enable.add_argument("--dry-run", action="store_true", help="preview only, no writes")

    p_disable = sub.add_parser(
        "disable", help="disable business pool; keepalive is left untouched (JSON)"
    )
    p_disable.add_argument("--token-id", type=int, required=True)
    p_disable.add_argument("--dry-run", action="store_true", help="preview only, no writes")

    p_keep = sub.add_parser(
        "keepalive", help="turn keepalive on/off; always runtime_mode=persistent (JSON)"
    )
    p_keep.add_argument("--token-id", type=int, required=True)
    p_keep.add_argument("state", choices=["on", "off"])
    p_keep.add_argument("--dry-run", action="store_true", help="preview only, no writes")

    return parser


def _status_row(record, policy) -> dict:
    """Project one keepalive-enabled ``TelemetryRecord`` into the Agent-facing shape.

    Deliberately excludes ST/AT: ``read_telemetry`` never selects them, and this
    projection only forwards the credential-free fields it returns.
    """
    from scripts.keepalive_patrol import classify_telemetry

    health, health_reason = classify_telemetry(record, policy=policy)
    return {
        "token_id": record.token_id,
        "email": record.email,
        "business_enabled": record.business_enabled,
        "ban_reason": record.ban_reason,
        "runtime_mode": record.runtime_mode,
        "profile_state": record.profile_state,
        "membership_status": record.membership_status,
        "last_attempt_at": record.last_attempt_at,
        "last_success_at": record.last_success_at,
        "last_status": record.last_status,
        "failure_count": record.failure_count,
        "next_due_at": record.next_due_at,
        "last_failure_code": record.last_failure_code,
        "health": health,
        "health_reason": health_reason,
    }


async def _cmd_status(args, db) -> int:
    """Report keepalive-enabled tokens' health as JSON (never business-disabled-only tokens).

    Scope matches ``keepalive_patrol.read_telemetry``: only rows with
    ``keepalive_enabled = 1`` are reported, since that is what "keepalive health"
    means. A business-disabled-but-keepalive-enabled token still appears (its
    ``business_enabled`` field reflects the ban).
    """
    from scripts.keepalive_patrol import build_cadence_policy, read_telemetry
    from src.core.config import config

    policy = build_cadence_policy(
        config.keepalive_browser_interval_seconds,
        config.keepalive_browser_retired_interval_seconds,
    )
    records = read_telemetry(db.db_path)
    token_id_filter = getattr(args, "token_id", None)
    if token_id_filter is not None:
        records = [record for record in records if record.token_id == token_id_filter]
    emit_json({"tokens": [_status_row(record, policy) for record in records]})
    return int(ExitCode.OK)


# Only codes needing a exit code other than the VALIDATION_FAILED default are
# listed here. Every other real OnboardError code (browser_launch,
# login_timeout, browser_crashed, cookie_missing, identity_mismatch,
# project_pool_failed, and the propagated inspect_account_identity codes
# invalid_account/session_rejected/session_error/grant_expired/credits_error)
# means the login/session could not be verified as-is -- re-running onboard
# with the same inputs will not help without human intervention, which is
# exactly what exit 5 signals to the calling Agent.
_EXIT_BY_ONBOARD_CODE = {
    "onboard_busy": ExitCode.BUSY,
    "profile_busy": ExitCode.BUSY,
    "not_found": ExitCode.NOT_FOUND,
    "publish_failed": ExitCode.PUBLISH_FAILED,
}


def _onboard_target(args):
    """Return whichever of --email/--token-id identifies the onboard target."""
    return args.email if args.token_id is None else args.token_id


async def _cmd_onboard(args, db, flow_client, runtime, display) -> int:
    """Onboard a new account (--email) or re-login an existing one (--token-id).

    Emits phased JSON: ``awaiting_login`` (foreground XRDP Chrome is about to
    open, blocking up to ``DEFAULT_LOGIN_TIMEOUT_SECONDS``) then either
    ``published`` (success) or ``failed`` (an ``OnboardError``, mapped to a
    stable exit code via ``_EXIT_BY_ONBOARD_CODE``).
    """
    target = _onboard_target(args)
    if args.dry_run:
        action = "onboard_existing" if args.token_id is not None else "onboard_new"
        emit_json({
            "dry_run": True,
            "would_do": [{
                "action": action, "target": target, "display": display, "runtime_mode": "persistent",
            }],
        })
        return int(ExitCode.OK)

    observed_at = datetime.now(timezone.utc)
    emit_json({
        "phase": "awaiting_login", "target": target, "display": display,
        "timeout_seconds": DEFAULT_LOGIN_TIMEOUT_SECONDS,
        "message": "Log in to Google + Flow on the XRDP display, then close Chrome.",
    })
    try:
        if args.token_id is not None:
            outcome = await onboard_existing(
                token_id=args.token_id, runtime=runtime, display=display,
                db=db, flow_client=flow_client, observed_at=observed_at,
            )
        else:
            outcome = await onboard_new(
                email=args.email, runtime=runtime, display=display,
                db=db, flow_client=flow_client, observed_at=observed_at,
            )
    except OnboardError as error:
        exit_code = _EXIT_BY_ONBOARD_CODE.get(error.code, ExitCode.VALIDATION_FAILED)
        emit_json({"phase": "failed", "error": {"code": error.code, "message": str(error)}})
        return int(exit_code)

    emit_json({
        "phase": "published", "token_id": outcome.token_id,
        "membership_status": outcome.membership_status, "pool_transition": outcome.pool_transition,
        "business_active": outcome.business_active, "ban_reason": outcome.ban_reason,
        "keepalive_enabled": outcome.keepalive_enabled, "runtime_mode": outcome.runtime_mode,
        "profile_state": outcome.profile_state,
    })
    return int(ExitCode.OK)


async def _cmd_enable(args, db) -> int:
    """Re-enable the business pool for one token, clearing any prior ban."""
    if args.dry_run:
        emit_json({"dry_run": True, "would_do": [{"action": "enable_token", "token_id": args.token_id}]})
        return int(ExitCode.OK)
    token = await db.get_token(args.token_id)
    if token is None:
        return emit_error("not_found", f"token {args.token_id} not found", exit_code=ExitCode.NOT_FOUND)

    from src.services.token_manager import TokenManager

    await TokenManager(db, None).enable_token(args.token_id)
    emit_json({"token_id": args.token_id, "business_active": True, "ban_reason": None})
    return int(ExitCode.OK)


async def _cmd_disable(args, db) -> int:
    """Disable the business pool for one token. Keepalive is left untouched."""
    if args.dry_run:
        emit_json({
            "dry_run": True,
            "would_do": [{
                "action": "disable_token", "token_id": args.token_id,
                "reason": TOKEN_REASON_MANUAL_DISABLED,
            }],
        })
        return int(ExitCode.OK)
    token = await db.get_token(args.token_id)
    if token is None:
        return emit_error("not_found", f"token {args.token_id} not found", exit_code=ExitCode.NOT_FOUND)

    from src.services.token_manager import TokenManager

    await TokenManager(db, None).disable_token(args.token_id)
    emit_json({
        "token_id": args.token_id, "business_active": False, "ban_reason": TOKEN_REASON_MANUAL_DISABLED,
    })
    return int(ExitCode.OK)


async def _cmd_keepalive(args, db) -> int:
    """Turn keepalive on/off for one token. Always ``runtime_mode="persistent"``."""
    keepalive_enabled = args.state == "on"
    if args.dry_run:
        emit_json({
            "dry_run": True,
            "would_do": [{
                "action": "set_keepalive", "token_id": args.token_id,
                "keepalive_enabled": keepalive_enabled, "runtime_mode": "persistent",
            }],
        })
        return int(ExitCode.OK)
    token = await db.get_token(args.token_id)
    if token is None:
        return emit_error("not_found", f"token {args.token_id} not found", exit_code=ExitCode.NOT_FOUND)

    await db.set_token_desired_state(
        args.token_id, keepalive_enabled=keepalive_enabled, runtime_mode="persistent"
    )
    emit_json({
        "token_id": args.token_id, "keepalive_enabled": keepalive_enabled, "runtime_mode": "persistent",
    })
    return int(ExitCode.OK)


async def _dispatch(args) -> int:
    """Route one parsed subcommand to its handler, constructing shared deps.

    ``Database()`` is inert to construct (no file I/O until a query actually
    runs), so building it unconditionally is safe even for ``--dry-run`` calls
    that never touch it. ``config``/``FlowClient``/``ProxyManager`` are loaded
    lazily here (mirrors ``setup_keepalive_profile._load_runtime_dependencies``)
    to keep the module importable without a fully configured environment.
    """
    db = Database()
    if args.command == "status":
        return await _cmd_status(args, db)
    if args.command == "enable":
        return await _cmd_enable(args, db)
    if args.command == "disable":
        return await _cmd_disable(args, db)
    if args.command == "keepalive":
        return await _cmd_keepalive(args, db)

    from src.core.config import config
    from src.services.flow_client import FlowClient
    from src.services.proxy_manager import ProxyManager

    runtime = resolve_runtime(config, os.environ)
    display = resolve_display(args.display, os.environ)
    flow_client = FlowClient(ProxyManager(db), db)
    return await _cmd_onboard(args, db, flow_client, runtime, display)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        return emit_error(
            "internal", f"{type(error).__name__}: {error}", exit_code=ExitCode.INTERNAL
        )


if __name__ == "__main__":
    raise SystemExit(main())
