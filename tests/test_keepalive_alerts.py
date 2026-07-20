"""Pure keepalive incident transition policy tests."""

from dataclasses import FrozenInstanceError

import pytest

from src.services.keepalive.alerts import (
    AlertKind,
    AlertState,
    evaluate_alert_transition,
)
from src.services.keepalive.models import FailureCode, RefreshOutcome


def failed(
    code: FailureCode,
    detail: str = "failed",
    *,
    human_action: bool = False,
) -> RefreshOutcome:
    return RefreshOutcome.failure(
        code,
        detail=detail,
        human_action=human_action,
    )


def test_first_failure_opens_episode_and_emits_one_alert():
    previous = AlertState()

    transition = evaluate_alert_transition(
        previous,
        failed(FailureCode.NETWORK, "proxy timeout"),
    )

    assert transition.previous is previous
    assert transition.current == AlertState(
        active_code=FailureCode.NETWORK,
        episode=1,
        alerted=True,
    )
    assert transition.event is not None
    assert transition.event.kind is AlertKind.FAILURE
    assert transition.event.code is FailureCode.NETWORK
    assert transition.event.episode == 1
    assert transition.event.detail == "proxy timeout"


def test_unchanged_incident_does_not_repeat_alert():
    alerted = AlertState(
        active_code=FailureCode.NETWORK,
        episode=4,
        alerted=True,
    )

    transition = evaluate_alert_transition(
        alerted,
        failed(FailureCode.NETWORK, "different detail, same incident"),
    )

    assert transition.current is alerted
    assert transition.event is None


def test_unalerted_incident_requests_delivery_without_starting_new_episode():
    pending = AlertState(
        active_code=FailureCode.BROWSER_LAUNCH,
        episode=2,
        alerted=False,
    )

    transition = evaluate_alert_transition(
        pending,
        failed(FailureCode.BROWSER_LAUNCH, "chrome failed to start"),
    )

    assert transition.current == AlertState(
        active_code=FailureCode.BROWSER_LAUNCH,
        episode=2,
        alerted=True,
    )
    assert transition.event is not None
    assert transition.event.kind is AlertKind.FAILURE
    assert transition.event.episode == 2


def test_changed_failure_code_opens_and_alerts_new_episode():
    active = AlertState(
        active_code=FailureCode.NETWORK,
        episode=1,
        alerted=True,
    )

    transition = evaluate_alert_transition(
        active,
        failed(FailureCode.SESSION_REJECTED, "session endpoint returned 401"),
    )

    assert transition.current == AlertState(
        active_code=FailureCode.SESSION_REJECTED,
        episode=2,
        alerted=True,
    )
    assert transition.event is not None
    assert transition.event.kind is AlertKind.FAILURE
    assert transition.event.code is FailureCode.SESSION_REJECTED
    assert transition.event.previous_code is FailureCode.NETWORK


def test_success_emits_recovery_only_after_alerted_failure():
    active = AlertState(
        active_code=FailureCode.GRANT_EXPIRED,
        episode=3,
        alerted=True,
    )

    transition = evaluate_alert_transition(
        active,
        RefreshOutcome.success(detail="credits validation passed", credits=50),
    )

    assert transition.current == AlertState(episode=3)
    assert transition.event is not None
    assert transition.event.kind is AlertKind.RECOVERY
    assert transition.event.code is FailureCode.GRANT_EXPIRED
    assert transition.event.episode == 3
    assert transition.event.detail == "credits validation passed"


def test_success_silently_clears_failure_that_was_never_alerted():
    active = AlertState(
        active_code=FailureCode.PROFILE_BUSY,
        episode=1,
        alerted=False,
    )

    transition = evaluate_alert_transition(active, RefreshOutcome.success(credits=1))

    assert transition.current == AlertState(episode=1)
    assert transition.event is None


def test_repeated_success_after_recovery_does_not_repeat_recovery_alert():
    recovered = AlertState(episode=5)

    transition = evaluate_alert_transition(
        recovered,
        RefreshOutcome.success(detail="still healthy", credits=1),
    )

    assert transition.current is recovered
    assert transition.event is None


def test_same_code_after_recovery_is_a_new_episode_and_alerts_again():
    recovered = AlertState(episode=1)

    transition = evaluate_alert_transition(
        recovered,
        failed(FailureCode.NETWORK, "network failed again"),
    )

    assert transition.current == AlertState(
        active_code=FailureCode.NETWORK,
        episode=2,
        alerted=True,
    )
    assert transition.event is not None
    assert transition.event.kind is AlertKind.FAILURE


def test_alert_models_are_immutable_and_reject_inconsistent_persisted_state():
    state = AlertState(
        active_code=FailureCode.COOKIE_MISSING,
        episode=1,
        alerted=True,
    )

    with pytest.raises(FrozenInstanceError):
        state.episode = 2
    with pytest.raises(ValueError, match="episode"):
        AlertState(active_code=FailureCode.COOKIE_MISSING, episode=0)
    with pytest.raises(ValueError, match="alerted"):
        AlertState(active_code=None, episode=1, alerted=True)
    with pytest.raises(ValueError, match="episode"):
        AlertState(episode=-1)


def test_transition_contains_complete_persistence_input_and_output_state():
    persisted = AlertState(
        active_code=FailureCode.CREDITS,
        episode=8,
        alerted=True,
    )

    transition = evaluate_alert_transition(
        persisted,
        failed(FailureCode.INTERNAL, "unexpected response shape"),
    )

    assert transition.previous == persisted
    assert transition.current.active_code is FailureCode.INTERNAL
    assert transition.current.episode == 9
    assert transition.current.alerted is True
