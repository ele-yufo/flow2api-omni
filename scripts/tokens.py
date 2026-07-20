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
import sys
from enum import IntEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.token_states import TOKEN_REASON_MANUAL_DISABLED  # noqa: E402


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


async def _cmd_onboard(args, db, flow_client, runtime, display) -> int:
    raise NotImplementedError


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
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_dispatch(args))
    except NotImplementedError:
        return emit_error(
            "not_implemented", f"command '{args.command}' not yet implemented", exit_code=ExitCode.INTERNAL
        )


if __name__ == "__main__":
    raise SystemExit(main())
