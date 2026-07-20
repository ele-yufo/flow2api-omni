"""Pure keepalive incident and recovery alert transition policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .models import FailureCode, RefreshOutcome


class AlertKind(str, Enum):
    """Notification type requested by an incident transition."""

    FAILURE = "failure"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class AlertState:
    """Minimal persistable state needed to deduplicate incident alerts."""

    active_code: Optional[FailureCode] = None
    episode: int = 0
    alerted: bool = False

    def __post_init__(self) -> None:
        if self.active_code is not None and not isinstance(
            self.active_code, FailureCode
        ):
            raise TypeError("active_code must be a FailureCode or None")
        if isinstance(self.episode, bool) or not isinstance(self.episode, int):
            raise TypeError("episode must be an integer")
        if self.episode < 0:
            raise ValueError("episode must be non-negative")
        if not isinstance(self.alerted, bool):
            raise TypeError("alerted must be a bool")
        if self.active_code is not None and self.episode == 0:
            raise ValueError("an active incident must have a positive episode")
        if self.active_code is None and self.alerted:
            raise ValueError("alerted cannot be true without an active incident")


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """Side-effect-free notification request emitted by a transition."""

    kind: AlertKind
    code: FailureCode
    episode: int
    detail: str = ""
    previous_code: Optional[FailureCode] = None


@dataclass(frozen=True, slots=True)
class AlertTransition:
    """Explicit persistence input, output, and optional notification request."""

    previous: AlertState
    current: AlertState
    event: Optional[AlertEvent]


def _new_incident(
    state: AlertState,
    outcome: RefreshOutcome,
) -> AlertTransition:
    code = outcome.code
    if code is None:
        raise ValueError("failed outcome must carry a failure code")
    episode = state.episode + 1
    current = AlertState(active_code=code, episode=episode, alerted=True)
    event = AlertEvent(
        kind=AlertKind.FAILURE,
        code=code,
        episode=episode,
        detail=outcome.detail,
        previous_code=state.active_code,
    )
    return AlertTransition(previous=state, current=current, event=event)


def _continuing_incident(
    state: AlertState,
    outcome: RefreshOutcome,
) -> AlertTransition:
    if state.alerted:
        return AlertTransition(previous=state, current=state, event=None)
    code = state.active_code
    if code is None:
        raise ValueError("continuing incident requires an active failure code")
    current = AlertState(active_code=code, episode=state.episode, alerted=True)
    event = AlertEvent(
        kind=AlertKind.FAILURE,
        code=code,
        episode=state.episode,
        detail=outcome.detail,
    )
    return AlertTransition(previous=state, current=current, event=event)


def _recover(state: AlertState, outcome: RefreshOutcome) -> AlertTransition:
    code = state.active_code
    if code is None:
        return AlertTransition(previous=state, current=state, event=None)
    current = AlertState(episode=state.episode)
    event = None
    if state.alerted:
        event = AlertEvent(
            kind=AlertKind.RECOVERY,
            code=code,
            episode=state.episode,
            detail=outcome.detail,
        )
    return AlertTransition(previous=state, current=current, event=event)


def evaluate_alert_transition(
    state: AlertState,
    outcome: RefreshOutcome,
) -> AlertTransition:
    """Alert once per code/episode and recover only an alerted incident."""

    if not isinstance(state, AlertState):
        raise TypeError("state must be an AlertState")
    if not isinstance(outcome, RefreshOutcome):
        raise TypeError("outcome must be a RefreshOutcome")
    if outcome.ok:
        return _recover(state, outcome)
    if outcome.code is state.active_code:
        return _continuing_incident(state, outcome)
    return _new_incident(state, outcome)
