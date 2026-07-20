"""Pure account lifecycle policy tests for keepalive observations."""

from dataclasses import FrozenInstanceError

import pytest

from src.core.account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    PAYGATE_TIER_ONE,
    PAYGATE_TIER_TWO,
)
from src.core.token_states import (
    TOKEN_REASON_429_RATE_LIMIT,
    TOKEN_REASON_CONSECUTIVE_ERRORS,
    TOKEN_REASON_GRANT_EXPIRED,
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_MEMBERSHIP_EXPIRED,
    TOKEN_REASON_ONBOARDING_PENDING,
    TOKEN_REASON_ST_REVOKED,
    AccountLifecycleState,
    AccountLifecycleStatus,
    TierClassification,
)
from src.services.tokens.lifecycle import (
    apply_account_tier_observation,
    classify_account_tier,
)


def test_token_reason_constants_preserve_existing_values():
    assert TOKEN_REASON_429_RATE_LIMIT == "429_rate_limit"
    assert TOKEN_REASON_ST_REVOKED == "ST_REVOKED"
    assert TOKEN_REASON_GRANT_EXPIRED == "GRANT_EXPIRED"
    assert TOKEN_REASON_MEMBERSHIP_EXPIRED == "membership_expired"
    assert TOKEN_REASON_MANUAL_DISABLED == "manual_disabled"
    assert TOKEN_REASON_CONSECUTIVE_ERRORS == "consecutive_errors"
    assert TOKEN_REASON_ONBOARDING_PENDING == "onboarding_pending"


@pytest.mark.parametrize(
    ("raw_tier", "expected"),
    [
        (PAYGATE_TIER_ONE, TierClassification.PAID),
        (PAYGATE_TIER_TWO, TierClassification.PAID),
        (PAYGATE_TIER_NOT_PAID, TierClassification.FREE),
        (None, TierClassification.UNKNOWN),
        ("", TierClassification.UNKNOWN),
        ("PAYGATE_TIER_THREE", TierClassification.UNKNOWN),
        (" PAYGATE_TIER_NOT_PAID ", TierClassification.UNKNOWN),
        ("paygate_tier_not_paid", TierClassification.UNKNOWN),
    ],
)
def test_classify_account_tier_is_exact(raw_tier, expected):
    assert classify_account_tier(raw_tier) is expected


def test_two_consecutive_free_observations_retire_active_account():
    active = AccountLifecycleState()

    candidate = apply_account_tier_observation(active, PAYGATE_TIER_NOT_PAID)
    retired = apply_account_tier_observation(candidate, PAYGATE_TIER_NOT_PAID)

    assert active == AccountLifecycleState(
        confirmed_status=AccountLifecycleStatus.ACTIVE,
        candidate=TierClassification.UNKNOWN,
        candidate_count=0,
    )
    assert candidate == AccountLifecycleState(
        confirmed_status=AccountLifecycleStatus.ACTIVE,
        candidate=TierClassification.FREE,
        candidate_count=1,
    )
    assert retired == AccountLifecycleState(
        confirmed_status=AccountLifecycleStatus.RETIRED,
        candidate=TierClassification.UNKNOWN,
        candidate_count=0,
    )


@pytest.mark.parametrize("paid_tier", [PAYGATE_TIER_ONE, PAYGATE_TIER_TWO])
def test_two_consecutive_paid_observations_restore_retired_account(paid_tier):
    retired = AccountLifecycleState(confirmed_status=AccountLifecycleStatus.RETIRED)

    candidate = apply_account_tier_observation(retired, paid_tier)
    restored = apply_account_tier_observation(candidate, paid_tier)

    assert candidate == AccountLifecycleState(
        confirmed_status=AccountLifecycleStatus.RETIRED,
        candidate=TierClassification.PAID,
        candidate_count=1,
    )
    assert restored == AccountLifecycleState(
        confirmed_status=AccountLifecycleStatus.ACTIVE,
        candidate=TierClassification.UNKNOWN,
        candidate_count=0,
    )


def test_different_paid_tiers_are_consecutive_paid_observations():
    state = AccountLifecycleState(confirmed_status=AccountLifecycleStatus.RETIRED)

    state = apply_account_tier_observation(state, PAYGATE_TIER_ONE)
    state = apply_account_tier_observation(state, PAYGATE_TIER_TWO)

    assert state.confirmed_status is AccountLifecycleStatus.ACTIVE
    assert state.candidate is TierClassification.UNKNOWN
    assert state.candidate_count == 0


def test_paid_observation_resets_pending_free_candidate():
    state = AccountLifecycleState()
    state = apply_account_tier_observation(state, PAYGATE_TIER_NOT_PAID)

    reset = apply_account_tier_observation(state, PAYGATE_TIER_ONE)
    next_free = apply_account_tier_observation(reset, PAYGATE_TIER_NOT_PAID)

    assert reset == AccountLifecycleState()
    assert next_free.candidate is TierClassification.FREE
    assert next_free.candidate_count == 1
    assert next_free.confirmed_status is AccountLifecycleStatus.ACTIVE


def test_free_observation_resets_pending_paid_candidate():
    retired = AccountLifecycleState(confirmed_status=AccountLifecycleStatus.RETIRED)
    candidate = apply_account_tier_observation(retired, PAYGATE_TIER_TWO)

    reset = apply_account_tier_observation(candidate, PAYGATE_TIER_NOT_PAID)
    next_paid = apply_account_tier_observation(reset, PAYGATE_TIER_ONE)

    assert reset == retired
    assert next_paid.candidate is TierClassification.PAID
    assert next_paid.candidate_count == 1
    assert next_paid.confirmed_status is AccountLifecycleStatus.RETIRED


@pytest.mark.parametrize("unknown_tier", [None, "", "unexpected", " PAYGATE_TIER_ONE"])
def test_unknown_observation_leaves_state_and_counter_unchanged(unknown_tier):
    pending_free = apply_account_tier_observation(
        AccountLifecycleState(), PAYGATE_TIER_NOT_PAID
    )

    unchanged = apply_account_tier_observation(pending_free, unknown_tier)

    assert unchanged is pending_free


def test_unknown_between_matching_observations_does_not_reset_counter():
    state = AccountLifecycleState()
    state = apply_account_tier_observation(state, PAYGATE_TIER_NOT_PAID)
    state = apply_account_tier_observation(state, None)
    state = apply_account_tier_observation(state, PAYGATE_TIER_NOT_PAID)

    assert state.confirmed_status is AccountLifecycleStatus.RETIRED


def test_observation_matching_confirmed_state_keeps_state_clear():
    active = AccountLifecycleState()
    retired = AccountLifecycleState(confirmed_status=AccountLifecycleStatus.RETIRED)

    assert apply_account_tier_observation(active, PAYGATE_TIER_ONE) is active
    assert apply_account_tier_observation(retired, PAYGATE_TIER_NOT_PAID) is retired


def test_lifecycle_state_is_immutable():
    state = AccountLifecycleState()

    with pytest.raises(FrozenInstanceError):
        state.candidate_count = 1


@pytest.mark.parametrize(
    "state",
    [
        AccountLifecycleState,
    ],
)
def test_lifecycle_state_rejects_inconsistent_candidate_state(state):
    with pytest.raises(ValueError):
        state(candidate=TierClassification.FREE, candidate_count=0)
    with pytest.raises(ValueError):
        state(candidate=TierClassification.UNKNOWN, candidate_count=1)
    with pytest.raises(ValueError):
        state(candidate=TierClassification.PAID, candidate_count=1)
    with pytest.raises(ValueError):
        state(
            confirmed_status=AccountLifecycleStatus.RETIRED,
            candidate=TierClassification.FREE,
            candidate_count=1,
        )
