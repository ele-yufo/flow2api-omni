"""Compatibility exports for the core account lifecycle policy."""

from ...core.account_lifecycle import (
    REQUIRED_CONSECUTIVE_OBSERVATIONS,
    apply_account_tier_observation,
    classify_account_tier,
    transition_account_lifecycle,
)

__all__ = [
    "REQUIRED_CONSECUTIVE_OBSERVATIONS",
    "apply_account_tier_observation",
    "classify_account_tier",
    "transition_account_lifecycle",
]
