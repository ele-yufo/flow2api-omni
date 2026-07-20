"""Pure account tier classification and lifecycle transition policy."""

from typing import Optional

from .account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    PAYGATE_TIER_ONE,
    PAYGATE_TIER_TWO,
)
from .token_states import (
    AccountLifecycleState,
    AccountLifecycleStatus,
    TierClassification,
)


REQUIRED_CONSECUTIVE_OBSERVATIONS = 2


def classify_account_tier(user_paygate_tier: Optional[str]) -> TierClassification:
    """Classify only canonical paygate tiers; missing and unknown values stay unknown."""
    if user_paygate_tier == PAYGATE_TIER_NOT_PAID:
        return TierClassification.FREE
    if user_paygate_tier in (PAYGATE_TIER_ONE, PAYGATE_TIER_TWO):
        return TierClassification.PAID
    return TierClassification.UNKNOWN


def transition_account_lifecycle(
    state: AccountLifecycleState,
    observation: TierClassification,
) -> AccountLifecycleState:
    """Apply one classified observation without mutating the input state."""
    if not isinstance(state, AccountLifecycleState):
        raise TypeError("state must be an AccountLifecycleState")
    if not isinstance(observation, TierClassification):
        raise TypeError("observation must be a TierClassification")
    if observation is TierClassification.UNKNOWN:
        return state

    transition_candidate = (
        TierClassification.FREE
        if state.confirmed_status is AccountLifecycleStatus.ACTIVE
        else TierClassification.PAID
    )
    if observation is not transition_candidate:
        if state.candidate is TierClassification.UNKNOWN:
            return state
        return AccountLifecycleState(confirmed_status=state.confirmed_status)

    next_count = state.candidate_count + 1 if state.candidate is observation else 1
    if next_count < REQUIRED_CONSECUTIVE_OBSERVATIONS:
        return AccountLifecycleState(
            confirmed_status=state.confirmed_status,
            candidate=observation,
            candidate_count=next_count,
        )

    next_status = (
        AccountLifecycleStatus.RETIRED
        if observation is TierClassification.FREE
        else AccountLifecycleStatus.ACTIVE
    )
    return AccountLifecycleState(confirmed_status=next_status)


def apply_account_tier_observation(
    state: AccountLifecycleState,
    user_paygate_tier: Optional[str],
) -> AccountLifecycleState:
    """Classify a raw paygate tier and apply it to an immutable lifecycle state."""
    return transition_account_lifecycle(state, classify_account_tier(user_paygate_tier))
