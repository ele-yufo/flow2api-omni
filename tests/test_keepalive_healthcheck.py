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
HEARTBEAT_NOW = datetime(2026, 8, 21, 12, 7, tzinfo=timezone.utc)  # 曾经的心跳时刻


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


def test_all_healthy_at_heartbeat_sends_all_clear(monkeypatch):
    monkeypatch.setattr(healthcheck, "HEARTBEAT_HOURS_UTC", frozenset({0, 12}))
    should_send, severity, title, description, fields = decide(now=HEARTBEAT_NOW)
    assert should_send is True
    assert severity == "warning"
    assert "全部存活" in title
    assert "无需人工介入" in description


def test_heartbeat_hours_default_disabled():
    assert healthcheck._heartbeat_hours("") == frozenset()
    assert healthcheck._heartbeat_hours("0, 12") == frozenset({0, 12})


def test_malformed_heartbeat_hours_never_kills_the_patrol():
    """值写错只能让心跳关掉,不能让巡检整体崩——崩了死号就没人报了。"""

    assert healthcheck._heartbeat_hours("0,12 UTC") == frozenset()
    assert healthcheck._heartbeat_hours("每天两次") == frozenset()
    assert healthcheck._heartbeat_hours("0,99,12") == frozenset({0, 12})


def test_heartbeat_disabled_stays_silent_at_former_heartbeat_hour():
    """心跳关闭后,原 00/12 UTC 时刻也不再投递(2026-09-06 默认)。"""

    assert healthcheck.HEARTBEAT_HOURS_UTC == frozenset()
    should_send, *_ = decide(now=HEARTBEAT_NOW)
    assert should_send is False


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
