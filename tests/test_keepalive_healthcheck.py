"""Healthcheck report gating: alert-only outside heartbeat, freshness-aware."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


HEALTHCHECK = (
    Path(__file__).parents[1] / "scripts" / "keepalive_healthcheck.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("keepalive_healthcheck", HEALTHCHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


healthcheck = _load_module()

NOW = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)  # 非心跳时刻
HEARTBEAT_NOW = datetime(2026, 8, 21, 12, 7, tzinfo=timezone.utc)


class FakeRecord:
    def __init__(self, email):
        self.email = email


ACTIVE = [(1, "a@example.com", 1, "", 100), (2, "b@example.com", 1, "", 200)]


def decide(dead=(), rows=(), now=NOW, force=False):
    return healthcheck.decide_report(
        ACTIVE,
        list(dead),
        300,
        list(rows),
        now=now,
        force=force,
    )


def test_all_healthy_outside_heartbeat_stays_silent():
    should_send, *_ = decide()
    assert should_send is False


def test_all_healthy_at_heartbeat_sends_all_clear():
    should_send, severity, title, description, fields = decide(now=HEARTBEAT_NOW)
    assert should_send is True
    assert severity == "warning"
    assert "全部存活" in title
    assert "无需人工介入" in description


def test_force_report_sends_outside_heartbeat():
    should_send, *_ = decide(force=True)
    assert should_send is True


def test_dead_account_is_critical_and_listed():
    should_send, severity, title, description, _ = decide(
        dead=[(3, "dead@example.com", 0, "ST_REVOKED", 0)],
    )
    assert should_send is True
    assert severity == "critical"
    assert "dead@example.com" in description


def test_unhealthy_lifecycle_is_critical_and_listed():
    rows = [(FakeRecord("stale@example.com"), "UNHEALTHY", "last success is overdue")]
    should_send, severity, _, description, _ = decide(rows=rows)
    assert should_send is True
    assert severity == "critical"
    assert "stale@example.com" in description
    assert "保活超期" in description


def test_probe_error_is_warning_not_critical():
    rows = [(FakeRecord("flaky@example.com"), "PROBE_ERROR", "runtime probe failed code=network")]
    should_send, severity, _, description, _ = decide(rows=rows)
    assert should_send is True
    assert severity == "warning"
    assert "flaky@example.com" in description


def test_healthy_rows_do_not_trigger_report():
    rows = [
        (FakeRecord("a@example.com"), "HEALTHY", "persisted success is within cadence grace"),
        (FakeRecord("b@example.com"), "HEALTHY", "persisted success is within cadence grace"),
    ]
    should_send, *_ = decide(rows=rows)
    assert should_send is False
