"""Dynamic keepalive supervisor tests without Chrome or network access."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.models import KeepaliveToken
from src.core.token_states import AccountLifecycleStatus, TierClassification
from src.services.keepalive.alerts import AlertKind
from src.services.keepalive.models import FailureCode, RefreshOutcome
from src.services.keepalive.scheduler import HUMAN_RETRY_SECONDS
from src.services.keepalive.supervisor import (
    KeepaliveSupervisor,
    ManagedAccountRunner,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def make_target(token_id: int = 23, **overrides) -> KeepaliveToken:
    values = {
        "id": token_id,
        "st": f"session-{token_id}",
        "email": f"user{token_id}@example.com",
        "is_active": True,
        "membership_confirmed_status": AccountLifecycleStatus.ACTIVE,
        "membership_candidate": TierClassification.UNKNOWN,
        "membership_candidate_count": 0,
        "keepalive_enabled": True,
        "runtime_mode": "warm",
        "profile_state": "ready",
        "verified_email": f"user{token_id}@example.com",
        "next_due_at": NOW - timedelta(seconds=1),
    }
    values.update(overrides)
    return KeepaliveToken(**values)


class MutableClock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeDatabase:
    def __init__(self, targets=None):
        self.targets = list(targets or [])
        self.list_error: Exception | None = None
        self.telemetry = []
        self.alert_states = []

    async def list_keepalive_enabled_tokens(self):
        if self.list_error is not None:
            raise self.list_error
        return list(self.targets)

    async def update_token_keepalive_telemetry(self, token_id, **kwargs):
        self.telemetry.append((token_id, kwargs))

    async def update_token_alert_state(self, token_id, **kwargs):
        self.alert_states.append((token_id, kwargs))


class FakeLease:
    def __init__(self, profile_path: Path):
        self.profile_path = profile_path
        self.release_count = 0

    @property
    def active(self) -> bool:
        return self.release_count == 0

    def release(self) -> None:
        self.release_count += 1


class LeaseFactory:
    def __init__(self, profile_base: Path):
        self.profile_base = profile_base
        self.leases = []
        self.calls = []

    def __call__(self, base_dir, token_id):
        assert Path(base_dir) == self.profile_base
        self.calls.append(token_id)
        lease = FakeLease(self.profile_base / str(token_id))
        self.leases.append(lease)
        return lease


class QueueRefresher:
    def __init__(self, *outcomes: RefreshOutcome):
        self.outcomes = list(outcomes)
        self.calls = []

    async def refresh(self, browser, target, profile, settle_seconds):
        self.calls.append((browser, target, profile, settle_seconds))
        if not self.outcomes:
            raise AssertionError("no refresh outcome queued")
        return self.outcomes.pop(0)


class BrowserHarness:
    def __init__(self):
        self.launches = []
        self.stops = []
        self.prepared = []

    def prepare(self, lease):
        self.prepared.append(lease)

    async def launch(self, profile, proxy, display, executable, *, headless):
        browser = SimpleNamespace(number=len(self.launches) + 1)
        self.launches.append(
            {
                "browser": browser,
                "profile": profile,
                "proxy": proxy,
                "display": display,
                "executable": executable,
                "headless": headless,
            }
        )
        return browser

    async def stop(self, browser):
        self.stops.append(browser)
        return True


def make_runner(
    tmp_path: Path,
    target: KeepaliveToken,
    refresher: QueueRefresher,
    *,
    db: FakeDatabase | None = None,
    clock: MutableClock | None = None,
    harness: BrowserHarness | None = None,
    lease_factory: LeaseFactory | None = None,
    alert_sender=None,
    launch_semaphore=None,
    refresh_semaphore=None,
    shutdown_timeout_seconds=20.0,
    attempt_timeout_seconds=300.0,
):
    profile_base = tmp_path / "profiles"
    profile_base.mkdir(exist_ok=True)
    (profile_base / str(target.id)).mkdir(exist_ok=True)
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    database = db or FakeDatabase()
    current_clock = clock or MutableClock()
    browser_harness = harness or BrowserHarness()
    leases = lease_factory or LeaseFactory(profile_base)
    runner = ManagedAccountRunner(
        target,
        database,
        profile_base=profile_base,
        proxy="http://127.0.0.1:7890",
        display=":10",
        browser_executable=executable,
        settle_seconds=4.5,
        refresher=refresher,
        browser_launcher=browser_harness.launch,
        browser_stopper=browser_harness.stop,
        profile_lease_acquirer=leases,
        profile_preparer=browser_harness.prepare,
        launch_semaphore=launch_semaphore,
        refresh_semaphore=refresh_semaphore,
        alert_sender=alert_sender,
        clock=current_clock,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
        attempt_timeout_seconds=attempt_timeout_seconds,
    )
    return runner, database, current_clock, browser_harness, leases


@pytest.mark.asyncio
async def test_warm_attempt_prepares_launches_headed_refreshes_and_drains(tmp_path):
    refresher = QueueRefresher(RefreshOutcome.success(credits=10))
    runner, db, _, harness, leases = make_runner(
        tmp_path, make_target(runtime_mode="warm"), refresher
    )

    outcome = await runner.run_if_due()

    assert outcome.ok is True
    assert len(harness.prepared) == 1
    assert harness.launches[0]["headless"] is False
    assert harness.launches[0]["proxy"] == "http://127.0.0.1:7890"
    assert harness.launches[0]["display"] == ":10"
    assert refresher.calls[0][3] == 4.5
    assert harness.stops == [harness.launches[0]["browser"]]
    assert leases.leases[0].release_count == 1
    assert runner.browser is None
    assert runner.profile_lease is None
    assert db.telemetry[0][1]["status"] == "success"
    assert db.telemetry[0][1]["next_due_at"] > NOW


@pytest.mark.asyncio
async def test_run_now_ignores_future_due_for_operational_gate(tmp_path):
    target = make_target(next_due_at=NOW + timedelta(days=1), runtime_mode="warm")
    runner, db, _, harness, leases = make_runner(
        tmp_path,
        target,
        QueueRefresher(RefreshOutcome.success(credits=7)),
    )

    outcome = await runner.run_now()

    assert outcome.ok is True
    assert len(harness.launches) == 1
    assert len(db.telemetry) == 1
    assert leases.leases[0].release_count == 1


@pytest.mark.asyncio
async def test_stop_cancels_hung_refresh_and_releases_browser_and_profile(tmp_path):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingRefresher:
        async def refresh(self, browser, target, profile, settle_seconds):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runner, _, _, harness, leases = make_runner(
        tmp_path,
        make_target(runtime_mode="persistent"),
        HangingRefresher(),
        shutdown_timeout_seconds=0.1,
    )
    active_run = asyncio.create_task(runner.run_now())
    await asyncio.wait_for(entered.wait(), timeout=1)

    await asyncio.wait_for(runner.stop(), timeout=0.5)

    with pytest.raises(asyncio.CancelledError):
        await active_run
    assert cancelled.is_set()
    assert harness.stops == [harness.launches[0]["browser"]]
    assert leases.leases[0].release_count == 1
    assert runner.browser is None
    assert runner.profile_lease is None


@pytest.mark.asyncio
async def test_persistent_retains_until_mode_update_then_drains(tmp_path):
    target = make_target(runtime_mode="persistent")
    refresher = QueueRefresher(RefreshOutcome.success(credits=10))
    runner, _, _, harness, leases = make_runner(tmp_path, target, refresher)

    await runner.run_if_due()

    assert runner.browser is harness.launches[0]["browser"]
    assert runner.profile_lease is leases.leases[0]
    assert harness.stops == []
    assert leases.leases[0].release_count == 0

    await runner.update_target(target.model_copy(update={"runtime_mode": "warm"}))

    assert runner.target.runtime_mode == "warm"
    assert runner.browser is None
    assert runner.profile_lease is None
    assert harness.stops == [harness.launches[0]["browser"]]
    assert leases.leases[0].release_count == 1


@pytest.mark.asyncio
async def test_restart_outcome_closes_persistent_browser_and_persists_typed_telemetry(
    tmp_path,
):
    failure = RefreshOutcome.failure(
        FailureCode.NAVIGATION,
        detail="page target closed",
        restart_browser=True,
    )
    runner, db, _, harness, leases = make_runner(
        tmp_path,
        make_target(runtime_mode="persistent", keepalive_failure_count=2),
        QueueRefresher(failure),
    )

    outcome = await runner.run_if_due()

    assert outcome is failure
    assert harness.stops == [harness.launches[0]["browser"]]
    assert leases.leases[0].release_count == 1
    telemetry = db.telemetry[0]
    assert telemetry[0] == 23
    assert telemetry[1]["status"] == "failure"
    assert telemetry[1]["error"] == "page target closed"
    assert telemetry[1]["error_code"] == "navigation"
    assert telemetry[1]["attempted_at"] == NOW
    assert telemetry[1]["next_due_at"] > NOW


@pytest.mark.asyncio
async def test_missing_profile_is_typed_human_outcome_without_creating_or_launching(
    tmp_path,
):
    target = make_target()
    profile_base = tmp_path / "profiles"
    profile_base.mkdir()
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    db = FakeDatabase()
    clock = MutableClock()
    harness = BrowserHarness()
    leases = LeaseFactory(profile_base)
    refresher = QueueRefresher(RefreshOutcome.success())
    runner = ManagedAccountRunner(
        target,
        db,
        profile_base=profile_base,
        browser_executable=executable,
        refresher=refresher,
        browser_launcher=harness.launch,
        browser_stopper=harness.stop,
        profile_lease_acquirer=leases,
        profile_preparer=harness.prepare,
        clock=clock,
    )

    outcome = await runner.run_if_due()

    assert outcome.code is FailureCode.PROFILE_MISSING
    assert outcome.human_action is True
    assert not (profile_base / "23").exists()
    assert harness.prepared == []
    assert harness.launches == []
    assert refresher.calls == []
    assert leases.leases[0].release_count == 1
    assert db.telemetry[0][1]["next_due_at"] == NOW + timedelta(
        seconds=HUMAN_RETRY_SECONDS
    )


@pytest.mark.asyncio
async def test_unprovisioned_profile_metadata_never_launches_existing_empty_directory(
    tmp_path,
):
    runner, _, _, harness, leases = make_runner(
        tmp_path,
        make_target(profile_state="unprovisioned"),
        QueueRefresher(RefreshOutcome.success()),
    )

    outcome = await runner.run_if_due()

    assert outcome.code is FailureCode.PROFILE_MISSING
    assert outcome.human_action is True
    assert harness.prepared == []
    assert harness.launches == []
    assert leases.leases[0].release_count == 1


@pytest.mark.asyncio
async def test_alert_failure_once_then_recovery(tmp_path):
    outcomes = (
        RefreshOutcome.failure(FailureCode.NETWORK, detail="proxy timeout"),
        RefreshOutcome.failure(FailureCode.NETWORK, detail="proxy timeout again"),
        RefreshOutcome.success(detail="healthy", credits=5),
    )
    events = []

    async def send_alert(target, event):
        events.append((target.id, event))

    runner, db, clock, _, _ = make_runner(
        tmp_path,
        make_target(runtime_mode="persistent"),
        QueueRefresher(*outcomes),
        alert_sender=send_alert,
    )

    await runner.run_if_due()
    clock.advance(61)
    await runner.run_if_due()
    clock.advance(121)
    await runner.run_if_due()

    assert [event.kind for _, event in events] == [
        AlertKind.FAILURE,
        AlertKind.RECOVERY,
    ]
    assert events[0][1].episode == 1
    assert events[1][1].episode == 1
    assert [state[1]["alerted"] for state in db.alert_states] == [True, True, False]
    assert db.alert_states[-1][1]["alert_code"] is None


@pytest.mark.asyncio
async def test_alert_sender_failure_persists_retryable_state_and_retries(tmp_path):
    attempts = []

    async def flaky_sender(_target, event):
        attempts.append(event)
        return len(attempts) > 1

    runner, db, clock, _, _ = make_runner(
        tmp_path,
        make_target(runtime_mode="persistent"),
        QueueRefresher(
            RefreshOutcome.failure(FailureCode.CREDITS, detail="bad response"),
            RefreshOutcome.failure(FailureCode.CREDITS, detail="bad response again"),
        ),
        alert_sender=flaky_sender,
    )

    await runner.run_if_due()
    assert db.alert_states[-1][1] == {
        "alert_code": "credits",
        "episode": 1,
        "alerted": False,
        "alerted_at": None,
    }

    clock.advance(61)
    await runner.run_if_due()

    assert len(attempts) == 2
    assert attempts[0].episode == attempts[1].episode == 1
    assert db.alert_states[-1][1]["alerted"] is True


class FakeManagedRunner:
    def __init__(self, target, log, **_dependencies):
        self.target = target
        self.log = log
        self.stop_count = 0
        self.update_count = 0

    @property
    def token_id(self):
        return self.target.id

    @property
    def next_due_at(self):
        return self.target.next_due_at

    def is_due(self):
        return True

    async def run_if_due(self):
        self.log.append(("run", self.target.id))
        return RefreshOutcome.success()

    async def update_target(self, target):
        self.target = target
        self.update_count += 1

    async def stop(self):
        self.stop_count += 1


@pytest.mark.asyncio
async def test_reconcile_adds_updates_and_removes_without_restart():
    db = FakeDatabase([make_target(2)])
    log = []
    supervisor = KeepaliveSupervisor(
        db,
        runner_factory=lambda target, **deps: FakeManagedRunner(target, log, **deps),
        clock=lambda: NOW,
    )

    assert await supervisor.reconcile() is True
    first = supervisor.runners[2]
    assert set(supervisor.runners) == {2}

    db.targets = [
        make_target(2, runtime_mode="persistent", remark="changed"),
        make_target(3),
    ]
    await supervisor.reconcile()
    assert set(supervisor.runners) == {2, 3}
    assert supervisor.runners[2] is first
    assert first.update_count == 1
    assert first.target.remark == "changed"

    removed = supervisor.runners[3]
    db.targets = [make_target(2)]
    await supervisor.reconcile()
    assert set(supervisor.runners) == {2}
    assert removed.stop_count == 1


@pytest.mark.asyncio
async def test_transient_database_failure_preserves_last_known_runners():
    db = FakeDatabase([make_target(7)])
    supervisor = KeepaliveSupervisor(
        db,
        runner_factory=lambda target, **deps: FakeManagedRunner(target, [], **deps),
    )
    await supervisor.reconcile()
    known = supervisor.runners[7]

    db.list_error = RuntimeError("database temporarily unavailable")

    assert await supervisor.reconcile() is False
    assert supervisor.runners == {7: known}
    assert known.stop_count == 0


@pytest.mark.asyncio
async def test_business_disabled_retired_target_remains_managed():
    target = make_target(
        8,
        is_active=False,
        membership_confirmed_status=AccountLifecycleStatus.RETIRED,
    )
    db = FakeDatabase([target])
    supervisor = KeepaliveSupervisor(
        db,
        runner_factory=lambda target, **deps: FakeManagedRunner(target, [], **deps),
    )

    await supervisor.reconcile()

    assert set(supervisor.runners) == {8}
    assert supervisor.runners[8].target.is_active is False
    assert (
        supervisor.runners[8].target.membership_confirmed_status
        is AccountLifecycleStatus.RETIRED
    )


@pytest.mark.asyncio
async def test_run_due_once_uses_stable_due_time_then_token_id_order():
    log = []
    targets = [
        make_target(9, next_due_at=NOW - timedelta(seconds=2)),
        make_target(3, next_due_at=NOW - timedelta(seconds=5)),
        make_target(2, next_due_at=NOW - timedelta(seconds=5)),
    ]
    supervisor = KeepaliveSupervisor(
        FakeDatabase(targets),
        runner_factory=lambda target, **deps: FakeManagedRunner(target, log, **deps),
        clock=lambda: NOW,
    )
    await supervisor.reconcile()

    await supervisor.run_due_once()

    assert log == [("run", 2), ("run", 3), ("run", 9)]


@pytest.mark.asyncio
async def test_global_launch_and_refresh_concurrency_default_to_one(tmp_path):
    profile_base = tmp_path / "profiles"
    profile_base.mkdir()
    for token_id in (1, 2):
        (profile_base / str(token_id)).mkdir()
    executable = tmp_path / "chrome"
    executable.write_text("binary", encoding="utf-8")
    db = FakeDatabase([make_target(1), make_target(2)])
    launch_active = 0
    launch_max = 0
    refresh_active = 0
    refresh_max = 0

    async def launcher(profile, proxy, display, executable, *, headless):
        nonlocal launch_active, launch_max
        assert headless is False
        launch_active += 1
        launch_max = max(launch_max, launch_active)
        await asyncio.sleep(0.01)
        launch_active -= 1
        return SimpleNamespace(profile=profile)

    class ConcurrentRefresher:
        async def refresh(self, browser, target, profile, settle_seconds):
            nonlocal refresh_active, refresh_max
            refresh_active += 1
            refresh_max = max(refresh_max, refresh_active)
            await asyncio.sleep(0.01)
            refresh_active -= 1
            return RefreshOutcome.success(credits=target.id)

    lease_factory = LeaseFactory(profile_base)
    supervisor = KeepaliveSupervisor(
        db,
        profile_base=profile_base,
        browser_executable=executable,
        refresher_factory=lambda _target: ConcurrentRefresher(),
        browser_launcher=launcher,
        browser_stopper=lambda _browser: None,
        profile_lease_acquirer=lease_factory,
        profile_preparer=lambda _lease: None,
        clock=lambda: NOW,
    )
    await supervisor.reconcile()

    await supervisor.run_due_once()

    assert launch_max == 1
    assert refresh_max == 1


@pytest.mark.asyncio
async def test_graceful_stop_drains_every_runner_and_is_idempotent():
    db = FakeDatabase([make_target(1), make_target(2)])
    supervisor = KeepaliveSupervisor(
        db,
        runner_factory=lambda target, **deps: FakeManagedRunner(target, [], **deps),
    )
    await supervisor.reconcile()
    runners = list(supervisor.runners.values())

    await supervisor.stop()
    await supervisor.stop()

    assert supervisor.runners == {}
    assert [runner.stop_count for runner in runners] == [1, 1]


class HungRefresher:
    """Refresher whose refresh never returns (2026-08-21 daemon freeze shape)."""

    def __init__(self):
        self.calls = 0

    async def refresh(self, browser, target, profile, settle_seconds):
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_attempt_timeout_degrades_to_network_failure_and_restarts_browser(tmp_path):
    refresher = HungRefresher()
    runner, db, _, harness, leases = make_runner(
        tmp_path,
        make_target(runtime_mode="persistent"),
        refresher,
        attempt_timeout_seconds=0.1,
    )

    outcome = await asyncio.wait_for(runner.run_now(), timeout=5)

    assert outcome.ok is False
    assert outcome.code is FailureCode.NETWORK
    assert outcome.restart_browser is True
    assert "exceeded" in outcome.detail
    # 遥测照常落库：失败可见、按普通退避重试，而不是无声悬挂。
    assert db.telemetry[0][1]["status"] == "failure"
    assert db.telemetry[0][1]["error_code"] == "network"
    # restart_browser 语义：悬挂浏览器被销毁、lease 释放，下一轮重建。
    assert harness.stops == [harness.launches[0]["browser"]]
    assert leases.leases[0].release_count == 1
    assert runner.browser is None


@pytest.mark.asyncio
async def test_run_forever_watchdog_exits_on_wedged_reconcile():
    class WedgedDatabase(FakeDatabase):
        async def list_keepalive_enabled_tokens(self):
            await asyncio.Event().wait()

    supervisor = KeepaliveSupervisor(
        WedgedDatabase(),
        runner_factory=lambda target, **deps: FakeManagedRunner(target, [], **deps),
        reconcile_timeout_seconds=0.05,
        reconcile_interval_seconds=0.01,
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(supervisor.run_forever(), timeout=5)


@pytest.mark.asyncio
async def test_run_forever_watchdog_exits_on_wedged_cycle():
    class HungRunner(FakeManagedRunner):
        async def run_if_due(self):
            await asyncio.Event().wait()

    db = FakeDatabase([make_target(5)])
    supervisor = KeepaliveSupervisor(
        db,
        runner_factory=lambda target, **deps: HungRunner(target, [], **deps),
        cycle_timeout_seconds=0.05,
        reconcile_interval_seconds=0.01,
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(supervisor.run_forever(), timeout=5)


@pytest.mark.asyncio
async def test_run_forever_keeps_cycling_within_watchdog_budget():
    log = []
    db = FakeDatabase([make_target(6, next_due_at=NOW + timedelta(days=1))])
    supervisor = KeepaliveSupervisor(
        db,
        runner_factory=lambda target, **deps: FakeManagedRunner(target, log, **deps),
        reconcile_interval_seconds=0.01,
        cycle_timeout_seconds=5,
        reconcile_timeout_seconds=5,
    )

    async def run_briefly():
        task = asyncio.create_task(supervisor.run_forever())
        await asyncio.sleep(0.1)
        await supervisor.stop()
        await task

    await asyncio.wait_for(run_briefly(), timeout=5)
    # 循环正常推进、重复执行，看门狗在宽裕预算下不误伤。
    assert len(log) > 1


@pytest.mark.asyncio
async def test_queued_attempt_does_not_consume_attempt_budget(tmp_path):
    """全池同时到期时，信号量排队等待不计入 attempt 预算（review MEDIUM 修复）。"""
    refresh_semaphore = asyncio.Semaphore(1)

    class SlowRefresher:
        def __init__(self, delay):
            self.delay = delay

        async def refresh(self, browser, target, profile, settle_seconds):
            await asyncio.sleep(self.delay)
            return RefreshOutcome.success(credits=target.id)

    # runner A 慢（0.3s），runner B 预算只有 0.15s 且排在后面：
    # 旧实现 B 会在排队中被误判超时；预算从拿到信号量起算后两者都应成功。
    runner_a, _, _, _, _ = make_runner(
        tmp_path,
        make_target(31),
        SlowRefresher(0.3),
        refresh_semaphore=refresh_semaphore,
        attempt_timeout_seconds=5.0,
    )
    runner_b, _, _, _, _ = make_runner(
        tmp_path,
        make_target(32),
        SlowRefresher(0.0),
        refresh_semaphore=refresh_semaphore,
        attempt_timeout_seconds=0.15,
    )

    outcome_a, outcome_b = await asyncio.gather(
        runner_a.run_now(), runner_b.run_now()
    )

    assert outcome_a.ok is True
    assert outcome_b.ok is True


@pytest.mark.asyncio
async def test_wedged_profile_lease_acquisition_degrades_to_profile_busy(tmp_path):
    """同步 lease 获取 wedge 不再冻结事件循环，限时降级为 PROFILE_BUSY。"""

    def hung_lease_acquirer(_base, _token_id):
        import time

        time.sleep(3)  # 超时后线程短暂残留，进程退出时由 executor join 收尾

    runner, db, _, _, _ = make_runner(
        tmp_path,
        make_target(runtime_mode="warm"),
        QueueRefresher(RefreshOutcome.success()),
        lease_factory=hung_lease_acquirer,
        attempt_timeout_seconds=0.1,
    )

    outcome = await asyncio.wait_for(runner.run_now(), timeout=5)

    assert outcome.ok is False
    assert outcome.code is FailureCode.PROFILE_BUSY
    assert "exceeded" in outcome.detail
