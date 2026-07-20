"""Pure keepalive model and scheduling policy tests."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.services.keepalive.models import FailureCode, RefreshOutcome, RuntimeMode
from src.services.keepalive.scheduler import (
    ACTIVE_INTERVAL_SECONDS,
    HUMAN_RETRY_SECONDS,
    INITIAL_DELAY_SECONDS,
    RETIRED_INTERVAL_SECONDS,
    RETRY_BASE_SECONDS,
    RETRY_MAX_SECONDS,
    KeepaliveScheduler,
    ScheduleState,
    evaluate_due,
    next_due_at,
    retry_delay_seconds,
    stable_stagger_seconds,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def test_runtime_mode_and_failure_codes_are_stable_string_enums():
    assert {mode.value for mode in RuntimeMode} == {"persistent", "warm"}
    assert {code.value for code in FailureCode} == {
        "profile_missing",
        "profile_busy",
        "identity_mismatch",
        "browser_launch",
        "navigation",
        "session_body",
        "cookie_missing",
        "session_rejected",
        "grant_expired",
        "credits",
        "network",
        "internal",
    }


def test_refresh_outcome_is_immutable_and_validates_success_failure_shape():
    success = RefreshOutcome.success(
        detail="credits validation passed",
        expiry=NOW + timedelta(hours=1),
        credits=0,
    )
    failure = RefreshOutcome.failure(
        FailureCode.NETWORK,
        detail="proxy unavailable",
        restart_browser=True,
    )

    assert success.ok is True
    assert success.code is None
    assert success.credits == 0
    assert failure.ok is False
    assert failure.code is FailureCode.NETWORK
    assert failure.restart_browser is True

    with pytest.raises(FrozenInstanceError):
        failure.detail = "changed"
    with pytest.raises(ValueError, match="successful outcome"):
        RefreshOutcome(ok=True, code=FailureCode.INTERNAL)
    with pytest.raises(ValueError, match="failed outcome"):
        RefreshOutcome(ok=False)
    with pytest.raises(ValueError, match="credits"):
        RefreshOutcome.success(credits=-1)


def test_success_accepts_unchanged_or_stale_expiry_after_credits_validation():
    stale_expiry = NOW - timedelta(hours=3)
    outcome = RefreshOutcome.success(
        detail="get_credits returned successfully",
        expiry=stale_expiry,
        credits=25,
    )

    due_at = next_due_at(
        token_id=23,
        retired=False,
        outcome=outcome,
        failure_count=0,
        now=NOW,
    )

    assert outcome.expiry == stale_expiry
    assert due_at > NOW
    assert (due_at - NOW).total_seconds() <= ACTIVE_INTERVAL_SECONDS


def test_stagger_is_stable_bounded_and_token_specific():
    first = stable_stagger_seconds(23, ACTIVE_INTERVAL_SECONDS)

    assert first == stable_stagger_seconds(23, ACTIVE_INTERVAL_SECONDS)
    assert 0 <= first < ACTIVE_INTERVAL_SECONDS
    assert len(
        {
            stable_stagger_seconds(token_id, ACTIVE_INTERVAL_SECONDS)
            for token_id in range(20, 30)
        }
    ) > 1

    with pytest.raises(ValueError, match="token_id"):
        stable_stagger_seconds(-1, ACTIVE_INTERVAL_SECONDS)
    with pytest.raises(ValueError, match="interval_seconds"):
        stable_stagger_seconds(1, 0)


@pytest.mark.parametrize(
    ("failure_count", "expected"),
    [
        (1, RETRY_BASE_SECONDS),
        (2, RETRY_BASE_SECONDS * 2),
        (3, RETRY_BASE_SECONDS * 4),
        (5, RETRY_BASE_SECONDS * 16),
        (6, RETRY_MAX_SECONDS),
        (50, RETRY_MAX_SECONDS),
    ],
)
def test_retry_backoff_is_exponential_and_capped(failure_count, expected):
    assert retry_delay_seconds(failure_count) == expected


def test_retry_backoff_rejects_missing_current_failure():
    with pytest.raises(ValueError, match="failure_count"):
        retry_delay_seconds(0)


def test_failure_uses_backoff_while_human_action_uses_slower_retry():
    retryable = RefreshOutcome.failure(FailureCode.NETWORK)
    human = RefreshOutcome.failure(
        FailureCode.PROFILE_MISSING,
        human_action=True,
    )

    retry_due = next_due_at(
        token_id=23,
        retired=False,
        outcome=retryable,
        failure_count=3,
        now=NOW,
    )
    human_due = next_due_at(
        token_id=23,
        retired=False,
        outcome=human,
        failure_count=3,
        now=NOW,
    )

    assert retry_due == NOW + timedelta(seconds=RETRY_BASE_SECONDS * 4)
    assert human_due == NOW + timedelta(seconds=HUMAN_RETRY_SECONDS)
    assert human_due > retry_due


def test_success_uses_active_or_retired_phase_without_lengthening_interval():
    success = RefreshOutcome.success(credits=100)
    active_first = next_due_at(23, False, success, 0, NOW)
    active_second = next_due_at(23, False, success, 0, active_first)
    retired_first = next_due_at(23, True, success, 0, NOW)
    retired_second = next_due_at(23, True, success, 0, retired_first)

    assert active_second - active_first == timedelta(seconds=ACTIVE_INTERVAL_SECONDS)
    assert retired_second - retired_first == timedelta(seconds=RETIRED_INTERVAL_SECONDS)
    assert NOW < active_first <= NOW + timedelta(seconds=ACTIVE_INTERVAL_SECONDS)
    assert NOW < retired_first <= NOW + timedelta(seconds=RETIRED_INTERVAL_SECONDS)


def test_initial_due_has_compatibility_delay_and_stable_stagger():
    state = ScheduleState(token_id=23, runtime_mode=RuntimeMode.WARM)

    decision = evaluate_due(
        state,
        now=NOW + timedelta(seconds=INITIAL_DELAY_SECONDS - 1),
        started_at=NOW,
        browser_running=False,
    )
    repeated = evaluate_due(
        state,
        now=decision.due_at,
        started_at=NOW,
        browser_running=False,
    )

    assert decision.due is False
    assert decision.due_at >= NOW + timedelta(seconds=INITIAL_DELAY_SECONDS)
    assert repeated.due is True
    assert repeated.due_at == decision.due_at


def test_persistent_and_warm_modes_share_due_time_but_differ_after_refresh():
    due_at = NOW - timedelta(seconds=1)
    persistent = ScheduleState(
        token_id=23,
        runtime_mode=RuntimeMode.PERSISTENT,
        next_due_at=due_at,
    )
    warm = ScheduleState(
        token_id=23,
        runtime_mode=RuntimeMode.WARM,
        next_due_at=due_at,
    )

    persistent_decision = evaluate_due(
        persistent,
        now=NOW,
        started_at=NOW - timedelta(hours=1),
        browser_running=True,
    )
    warm_decision = evaluate_due(
        warm,
        now=NOW,
        started_at=NOW - timedelta(hours=1),
        browser_running=False,
    )

    assert persistent_decision.due is True
    assert persistent_decision.launch_browser is False
    assert persistent_decision.keep_browser_running is True
    assert warm_decision.due is True
    assert warm_decision.launch_browser is True
    assert warm_decision.keep_browser_running is False


def test_not_due_never_requests_browser_launch():
    state = ScheduleState(
        token_id=23,
        runtime_mode=RuntimeMode.WARM,
        next_due_at=NOW + timedelta(minutes=10),
    )

    decision = evaluate_due(
        state,
        now=NOW,
        started_at=NOW - timedelta(hours=1),
        browser_running=False,
    )

    assert decision.due is False
    assert decision.launch_browser is False
    assert decision.seconds_until_due == 600.0


def test_scheduler_uses_injected_clock_only():
    current = {"now": NOW}
    scheduler = KeepaliveScheduler(clock=lambda: current["now"], started_at=NOW)
    state = ScheduleState(
        token_id=7,
        runtime_mode=RuntimeMode.WARM,
        next_due_at=NOW + timedelta(seconds=10),
    )

    assert scheduler.evaluate_due(state, browser_running=False).due is False
    current["now"] += timedelta(seconds=10)
    assert scheduler.evaluate_due(state, browser_running=False).due is True


def test_schedule_state_rejects_invalid_values():
    with pytest.raises(ValueError, match="token_id"):
        ScheduleState(token_id=-1, runtime_mode=RuntimeMode.WARM)
    with pytest.raises(ValueError, match="failure_count"):
        ScheduleState(
            token_id=1,
            runtime_mode=RuntimeMode.WARM,
            failure_count=-1,
        )
