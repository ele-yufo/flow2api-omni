#!/usr/bin/env python3
"""Read-only reporter for persisted browser keepalive lifecycle telemetry.

Health uses the configured active and retired browser intervals. Scheduling grace
is half of the applicable interval, clamped to 5 minutes through 1 hour. Both
``last_keepalive_success_at`` and ``next_due_at`` must remain within those bounds,
so a stopped sidecar cannot report its last success forever.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "flow.db"
MINIMUM_GRACE_SECONDS = 300
MAXIMUM_GRACE_SECONDS = 3600
_SUCCESS_STATUSES = frozenset({"success", "ok", "alive"})
_PROBE_FAILURE_CODES = frozenset(
    {
        "network",
        "network_error",
        "browser_launch",
        "navigation",
        "session_body",
        "credits",
        "internal",
        "profile_busy",
    }
)
_CRITICAL_FAILURE_CODES = frozenset(
    {
        "profile_missing",
        "identity_mismatch",
        "cookie_missing",
        "session_rejected",
        "grant_expired",
    }
)


class TelemetryRecord(NamedTuple):
    token_id: int
    email: str
    business_enabled: bool
    ban_reason: str | None
    runtime_mode: str
    profile_state: str
    membership_status: str
    last_attempt_at: str | None
    last_success_at: str | None
    last_status: str | None
    failure_count: int
    next_due_at: str | None
    last_failure_code: str | None


class CadencePolicy(NamedTuple):
    active: timedelta
    active_grace: timedelta
    retired: timedelta
    retired_grace: timedelta


def _interval_seconds(raw_value: object, name: str) -> int:
    if isinstance(raw_value, bool):
        raise TypeError(f"{name} must be a positive integer")
    value = int(raw_value)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _grace_for_interval(interval_seconds: int) -> timedelta:
    grace_seconds = max(MINIMUM_GRACE_SECONDS, interval_seconds / 2)
    return timedelta(seconds=min(MAXIMUM_GRACE_SECONDS, grace_seconds))


def build_cadence_policy(
    active_interval_seconds: object,
    retired_interval_seconds: object,
) -> CadencePolicy:
    active_seconds = _interval_seconds(active_interval_seconds, "active interval")
    retired_seconds = _interval_seconds(retired_interval_seconds, "retired interval")
    return CadencePolicy(
        active=timedelta(seconds=active_seconds),
        active_grace=_grace_for_interval(active_seconds),
        retired=timedelta(seconds=retired_seconds),
        retired_grace=_grace_for_interval(retired_seconds),
    )


def _readonly_uri(db_path: Path | str) -> str:
    path = Path(db_path).expanduser().resolve(strict=True)
    return f"{path.as_uri()}?mode=ro"


def read_telemetry(db_path: Path | str) -> list[TelemetryRecord]:
    """Read enabled lifecycle rows without selecting credentials or error details."""

    connection = sqlite3.connect(_readonly_uri(db_path), uri=True)
    try:
        rows = connection.execute(
            """
            SELECT
                t.id,
                t.email,
                t.is_active,
                t.ban_reason,
                l.runtime_mode,
                l.profile_state,
                l.membership_confirmed_status,
                l.last_keepalive_at,
                l.last_keepalive_success_at,
                l.last_keepalive_status,
                l.keepalive_failure_count,
                l.next_due_at,
                l.last_failure_code
            FROM tokens AS t
            JOIN token_lifecycle AS l ON l.token_id = t.id
            WHERE l.keepalive_enabled = 1
            ORDER BY t.id
            """
        ).fetchall()
    finally:
        connection.close()

    return [
        TelemetryRecord(
            token_id=int(row[0]),
            email=str(row[1] or ""),
            business_enabled=bool(row[2]),
            ban_reason=str(row[3]) if row[3] is not None else None,
            runtime_mode=str(row[4] or "unknown"),
            profile_state=str(row[5] or "unknown"),
            membership_status=str(row[6] or "unknown"),
            last_attempt_at=str(row[7]) if row[7] is not None else None,
            last_success_at=str(row[8]) if row[8] is not None else None,
            last_status=str(row[9]) if row[9] is not None else None,
            failure_count=int(row[10] or 0),
            next_due_at=str(row[11]) if row[11] is not None else None,
            last_failure_code=str(row[12]) if row[12] is not None else None,
        )
        for row in rows
    ]


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(raw_value: str | None) -> datetime | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def _cadence_and_grace(
    record: TelemetryRecord,
    policy: CadencePolicy,
) -> tuple[timedelta, timedelta]:
    if str(record.membership_status).strip().casefold() == "retired":
        return policy.retired, policy.retired_grace
    return policy.active, policy.active_grace


def classify_telemetry(
    record: TelemetryRecord,
    *,
    policy: CadencePolicy,
    now: datetime | None = None,
) -> tuple[str, str]:
    current_time = _as_utc(now or datetime.now(timezone.utc))
    status = str(record.last_status or "").strip().casefold()
    code = str(record.last_failure_code or "").strip().casefold()
    if status in _SUCCESS_STATUSES:
        try:
            last_success = _parse_timestamp(record.last_success_at)
            next_due = _parse_timestamp(record.next_due_at)
        except (TypeError, ValueError, OverflowError):
            return "PROBE_ERROR", "invalid persisted telemetry timestamp"
        if last_success is None:
            return "PROBE_ERROR", "successful state has no success timestamp"
        cadence, grace = _cadence_and_grace(record, policy)
        if current_time > last_success + cadence + grace:
            return "UNHEALTHY", "last success is overdue beyond cadence grace"
        if next_due is not None and current_time > next_due + grace:
            return "UNHEALTHY", "next scheduled attempt is overdue beyond cadence grace"
        return "HEALTHY", "persisted success is within cadence grace"
    if not status:
        return "UNHEALTHY", "no completed keepalive attempt is persisted"
    if code in _CRITICAL_FAILURE_CODES:
        return "UNHEALTHY", f"critical failure code={code}"
    if code in _PROBE_FAILURE_CODES or not code:
        return "PROBE_ERROR", f"runtime probe failed code={code or 'unknown'}"
    return "PROBE_ERROR", "unrecognized failure code"


def sanitize_email(email: str) -> str:
    local, separator, domain = str(email or "").strip().partition("@")
    if not separator or not local or not domain:
        return "hidden"
    return f"{local[0]}***@{domain.casefold()}"


def _safe_timestamp_display(raw_value: str | None) -> str:
    try:
        parsed = _parse_timestamp(raw_value)
    except (TypeError, ValueError, OverflowError):
        return "invalid"
    return parsed.isoformat() if parsed is not None else "never"


def report(
    records: list[TelemetryRecord],
    *,
    policy: CadencePolicy,
    now: datetime | None = None,
) -> int:
    current_time = _as_utc(now or datetime.now(timezone.utc))
    if not records:
        print("[patrol] UNHEALTHY no keepalive-enabled accounts are persisted")
        return 1

    unhealthy = 0
    probe_errors = 0
    for record in records:
        classification, reason = classify_telemetry(
            record,
            policy=policy,
            now=current_time,
        )
        if classification == "UNHEALTHY":
            unhealthy += 1
        elif classification == "PROBE_ERROR":
            probe_errors += 1
        business_state = "enabled" if record.business_enabled else "disabled"
        print(
            f"[patrol] id={record.token_id} email={sanitize_email(record.email)} "
            f"state={classification} business={business_state} "
            f"membership={record.membership_status} runtime={record.runtime_mode} "
            f"profile={record.profile_state} failures={record.failure_count} "
            f"last_attempt={_safe_timestamp_display(record.last_attempt_at)} "
            f"last_success={_safe_timestamp_display(record.last_success_at)} "
            f"next_due={_safe_timestamp_display(record.next_due_at)} reason={reason}"
        )

    print(
        f"[patrol] summary enabled={len(records)} healthy={len(records) - unhealthy - probe_errors} "
        f"unhealthy={unhealthy} probe_errors={probe_errors}"
    )
    if probe_errors:
        return 2
    if unhealthy:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report persisted keepalive telemetry without network or database writes"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite database path (opened read-only)",
    )
    return parser


def _load_runtime_config():
    from src.core.config import config

    return config


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    active_interval_seconds: int | None = None,
    retired_interval_seconds: int | None = None,
    config_object=None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        cadence_config = config_object
        if cadence_config is None and (
            active_interval_seconds is None or retired_interval_seconds is None
        ):
            cadence_config = _load_runtime_config()
        active_seconds = (
            cadence_config.keepalive_browser_interval_seconds
            if active_interval_seconds is None
            else active_interval_seconds
        )
        retired_seconds = (
            cadence_config.keepalive_browser_retired_interval_seconds
            if retired_interval_seconds is None
            else retired_interval_seconds
        )
        policy = build_cadence_policy(active_seconds, retired_seconds)
        records = read_telemetry(args.db)
        return report(records, policy=policy, now=now)
    except Exception as error:
        print(f"[patrol] PROBE_ERROR telemetry unavailable ({type(error).__name__})")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
