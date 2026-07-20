"""Pure keepalive due-time, staggering, and retry policy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .models import RefreshOutcome, RuntimeMode

ACTIVE_INTERVAL_SECONDS = 1200
RETIRED_INTERVAL_SECONDS = 43200
INITIAL_DELAY_SECONDS = 120
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 1800
HUMAN_RETRY_SECONDS = 21600

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class SchedulerPolicy:
    """Validated scheduling constants, configurable without global state."""

    active_interval_seconds: int = ACTIVE_INTERVAL_SECONDS
    retired_interval_seconds: int = RETIRED_INTERVAL_SECONDS
    initial_delay_seconds: int = INITIAL_DELAY_SECONDS
    retry_base_seconds: int = RETRY_BASE_SECONDS
    retry_max_seconds: int = RETRY_MAX_SECONDS
    human_retry_seconds: int = HUMAN_RETRY_SECONDS

    def __post_init__(self) -> None:
        values = (
            self.active_interval_seconds,
            self.retired_interval_seconds,
            self.retry_base_seconds,
            self.retry_max_seconds,
            self.human_retry_seconds,
        )
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("positive scheduler durations must be positive integers")
        if isinstance(self.initial_delay_seconds, bool) or self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be a non-negative integer")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds must not be below retry_base_seconds")


DEFAULT_POLICY = SchedulerPolicy()


@dataclass(frozen=True, slots=True)
class ScheduleState:
    """Persistable scheduling inputs for one keepalive account."""

    token_id: int
    runtime_mode: RuntimeMode
    retired: bool = False
    next_due_at: Optional[datetime] = None
    failure_count: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.token_id, bool) or not isinstance(self.token_id, int):
            raise TypeError("token_id must be an integer")
        if self.token_id <= 0:
            raise ValueError("token_id must be positive")
        if not isinstance(self.runtime_mode, RuntimeMode):
            raise TypeError("runtime_mode must be a RuntimeMode")
        if not isinstance(self.retired, bool):
            raise TypeError("retired must be a bool")
        if self.next_due_at is not None and not isinstance(self.next_due_at, datetime):
            raise TypeError("next_due_at must be a datetime or None")
        if isinstance(self.failure_count, bool) or not isinstance(self.failure_count, int):
            raise TypeError("failure_count must be an integer")
        if self.failure_count < 0:
            raise ValueError("failure_count must be non-negative")


@dataclass(frozen=True, slots=True)
class DueDecision:
    """Pure decision consumed by a supervisor without performing any I/O."""

    due: bool
    due_at: datetime
    seconds_until_due: float
    launch_browser: bool
    keep_browser_running: bool


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock values must be datetimes")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def stable_stagger_seconds(token_id: int, interval_seconds: int) -> int:
    """Return a process-independent phase offset derived only from token ID."""

    if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 0:
        raise ValueError("token_id must be a non-negative integer")
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds <= 0
    ):
        raise ValueError("interval_seconds must be a positive integer")
    mixed_id = (token_id * 2_654_435_761) & 0xFFFFFFFF
    return mixed_id % interval_seconds


def retry_delay_seconds(
    failure_count: int,
    *,
    policy: SchedulerPolicy = DEFAULT_POLICY,
) -> int:
    """Return capped exponential delay for a count including the current failure."""

    if isinstance(failure_count, bool) or not isinstance(failure_count, int):
        raise TypeError("failure_count must be an integer")
    if failure_count < 1:
        raise ValueError("failure_count must include the current failure")
    exponent = min(failure_count - 1, 62)
    uncapped = policy.retry_base_seconds * (2**exponent)
    return min(policy.retry_max_seconds, uncapped)


def _periodic_due_at(token_id: int, now: datetime, interval_seconds: int) -> datetime:
    current = _as_utc(now)
    phase = stable_stagger_seconds(token_id, interval_seconds)
    timestamp = current.timestamp()
    cycle = math.floor((timestamp - phase) / interval_seconds) + 1
    due_timestamp = phase + (cycle * interval_seconds)
    return datetime.fromtimestamp(due_timestamp, tz=timezone.utc)


def initial_due_at(
    token_id: int,
    started_at: datetime,
    *,
    policy: SchedulerPolicy = DEFAULT_POLICY,
) -> datetime:
    """Preserve the 120-second startup delay, then spread the initial sweep."""

    start = _as_utc(started_at)
    stagger = stable_stagger_seconds(token_id, policy.active_interval_seconds)
    return start + timedelta(seconds=policy.initial_delay_seconds + stagger)


def next_due_at(
    token_id: int,
    retired: bool,
    outcome: RefreshOutcome,
    failure_count: int,
    now: datetime,
    *,
    policy: SchedulerPolicy = DEFAULT_POLICY,
) -> datetime:
    """Compute the persisted next due time after one completed refresh attempt."""

    current = _as_utc(now)
    if not isinstance(outcome, RefreshOutcome):
        raise TypeError("outcome must be a RefreshOutcome")
    if outcome.ok:
        interval = (
            policy.retired_interval_seconds
            if retired
            else policy.active_interval_seconds
        )
        return _periodic_due_at(token_id, current, interval)
    if outcome.human_action:
        return current + timedelta(seconds=policy.human_retry_seconds)
    delay = retry_delay_seconds(failure_count, policy=policy)
    return current + timedelta(seconds=delay)


def evaluate_due(
    state: ScheduleState,
    *,
    now: datetime,
    started_at: datetime,
    browser_running: bool,
    policy: SchedulerPolicy = DEFAULT_POLICY,
) -> DueDecision:
    """Evaluate due status and browser lifetime actions from explicit inputs."""

    if not isinstance(state, ScheduleState):
        raise TypeError("state must be a ScheduleState")
    if not isinstance(browser_running, bool):
        raise TypeError("browser_running must be a bool")
    current = _as_utc(now)
    due_at = (
        initial_due_at(state.token_id, started_at, policy=policy)
        if state.next_due_at is None
        else _as_utc(state.next_due_at)
    )
    seconds_until_due = max(0.0, (due_at - current).total_seconds())
    due = seconds_until_due == 0.0
    return DueDecision(
        due=due,
        due_at=due_at,
        seconds_until_due=seconds_until_due,
        launch_browser=due and not browser_running,
        keep_browser_running=state.runtime_mode is RuntimeMode.PERSISTENT,
    )


class KeepaliveScheduler:
    """Clock-injected convenience wrapper around the pure scheduling functions."""

    def __init__(
        self,
        clock: Clock,
        *,
        started_at: Optional[datetime] = None,
        policy: SchedulerPolicy = DEFAULT_POLICY,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._started_at = _as_utc(started_at if started_at is not None else clock())
        self._policy = policy

    def evaluate_due(
        self,
        state: ScheduleState,
        *,
        browser_running: bool,
    ) -> DueDecision:
        """Evaluate one account using the injected clock exactly once."""

        return evaluate_due(
            state,
            now=self._clock(),
            started_at=self._started_at,
            browser_running=browser_running,
            policy=self._policy,
        )

    def next_due_at(
        self,
        state: ScheduleState,
        outcome: RefreshOutcome,
    ) -> datetime:
        """Compute a persisted due time and include the current failure in its streak."""

        failure_count = 0 if outcome.ok else state.failure_count + 1
        return next_due_at(
            state.token_id,
            state.retired,
            outcome,
            failure_count,
            self._clock(),
            policy=self._policy,
        )
