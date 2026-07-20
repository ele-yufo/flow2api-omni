"""Pure account identity values shared by core persistence and services."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True, slots=True)
class VerifiedAccountSnapshot:
    """Credential and metadata snapshot obtained from verified Google endpoints."""

    email: str
    normalized_email: str
    name: str
    st: str
    at: str
    at_expires: Optional[datetime]
    credits: int
    user_paygate_tier: Optional[str]


@dataclass(frozen=True, slots=True)
class VerifiedSnapshotResult:
    """Observable lifecycle outcome of one atomic verified snapshot write."""

    token_id: int
    membership_status: str
    pool_transition: Optional[str]


def normalize_account_email(email: str) -> str:
    """Normalize identity comparisons without Gmail alias or dot rewriting."""
    return str(email or "").strip().casefold()
