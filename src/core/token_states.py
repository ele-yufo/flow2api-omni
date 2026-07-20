"""Immutable account lifecycle state and canonical token reason constants."""

from dataclasses import dataclass
from enum import Enum


TOKEN_REASON_429_RATE_LIMIT = "429_rate_limit"
TOKEN_REASON_ST_REVOKED = "ST_REVOKED"
TOKEN_REASON_GRANT_EXPIRED = "GRANT_EXPIRED"
TOKEN_REASON_MEMBERSHIP_EXPIRED = "membership_expired"
TOKEN_REASON_MANUAL_DISABLED = "manual_disabled"
TOKEN_REASON_CONSECUTIVE_ERRORS = "consecutive_errors"
TOKEN_REASON_ONBOARDING_PENDING = "onboarding_pending"


class TierClassification(str, Enum):
    """Lifecycle-relevant classification of an observed account tier."""

    PAID = "paid"
    FREE = "free"
    UNKNOWN = "unknown"


class AccountLifecycleStatus(str, Enum):
    """Confirmed account status used by the lifecycle policy."""

    ACTIVE = "active"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class AccountLifecycleState:
    """Immutable confirmed status plus an optional one-observation candidate."""

    confirmed_status: AccountLifecycleStatus = AccountLifecycleStatus.ACTIVE
    candidate: TierClassification = TierClassification.UNKNOWN
    candidate_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.confirmed_status, AccountLifecycleStatus):
            raise ValueError("confirmed_status must be an AccountLifecycleStatus")
        if not isinstance(self.candidate, TierClassification):
            raise ValueError("candidate must be a TierClassification")
        if self.candidate_count not in (0, 1):
            raise ValueError("candidate_count must be 0 or 1")

        if self.candidate is TierClassification.UNKNOWN:
            if self.candidate_count != 0:
                raise ValueError("an unknown candidate must have a zero count")
            return

        if self.candidate_count != 1:
            raise ValueError("a known candidate must have a count of one")

        expected_candidate = (
            TierClassification.FREE
            if self.confirmed_status is AccountLifecycleStatus.ACTIVE
            else TierClassification.PAID
        )
        if self.candidate is not expected_candidate:
            raise ValueError("candidate must oppose the confirmed lifecycle status")
