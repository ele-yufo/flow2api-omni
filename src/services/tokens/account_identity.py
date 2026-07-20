"""Strict inspection of Google Flow account credentials and identity."""

from datetime import datetime
from typing import Optional, TYPE_CHECKING

from ...core.account_identity import (
    VerifiedAccountSnapshot,
    VerifiedSnapshotResult,
    normalize_account_email,
)
from ...core.cookie_extractor import MIN_ST_LEN

if TYPE_CHECKING:
    from ..flow_client import FlowClient


class AccountIdentityError(ValueError):
    """Raised when a session cannot be verified as a usable Flow account."""

    def __init__(self, message: str, *, code: str = "invalid_account"):
        super().__init__(message)
        self.code = code


def _is_unauthenticated(error: Exception) -> bool:
    message = str(error).casefold()
    return "401" in message or "unauthenticated" in message or "invalid_token" in message


def _parse_expiry(raw_expiry) -> Optional[datetime]:
    if not raw_expiry:
        return None
    try:
        return datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _select_session_token(original_st: str, rotated_st) -> str:
    if isinstance(rotated_st, str):
        candidate = rotated_st.strip()
        if candidate != original_st and len(candidate) >= MIN_ST_LEN:
            return candidate
    return original_st


async def inspect_account_identity(
    flow_client: "FlowClient",
    st: str,
) -> VerifiedAccountSnapshot:
    """Resolve and strongly validate a Flow account from its session token."""
    session_token = str(st or "").strip()
    if len(session_token) < MIN_ST_LEN:
        raise AccountIdentityError(
            f"session token is too short ({len(session_token)} < {MIN_ST_LEN})"
        )

    try:
        session = await flow_client.st_to_at(session_token)
    except Exception as exc:
        code = "session_rejected" if _is_unauthenticated(exc) else "session_error"
        raise AccountIdentityError(
            f"session validation failed: {type(exc).__name__}", code=code
        ) from exc

    if not isinstance(session, dict):
        raise AccountIdentityError("session validation returned an invalid payload")

    access_token = session.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise AccountIdentityError("session validation returned no access token")

    user = session.get("user")
    user = user if isinstance(user, dict) else {}
    email = str(user.get("email") or "").strip()
    normalized_email = normalize_account_email(email)
    if not normalized_email:
        raise AccountIdentityError("session validation returned no account identity")

    try:
        credits_result = await flow_client.get_credits(access_token)
    except Exception as exc:
        code = "grant_expired" if _is_unauthenticated(exc) else "credits_error"
        raise AccountIdentityError(
            f"credits validation failed: {type(exc).__name__}", code=code
        ) from exc

    if not isinstance(credits_result, dict):
        raise AccountIdentityError("credits validation returned an invalid payload")
    credits = credits_result.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, int):
        raise AccountIdentityError("credits validation returned an invalid credits value")

    raw_tier = credits_result.get("userPaygateTier")
    user_paygate_tier = str(raw_tier).strip() if isinstance(raw_tier, str) and raw_tier.strip() else None
    name = str(user.get("name") or "").strip() or email.split("@", 1)[0]

    return VerifiedAccountSnapshot(
        email=email,
        normalized_email=normalized_email,
        name=name,
        st=_select_session_token(session_token, session.get("rotated_st")),
        at=access_token.strip(),
        at_expires=_parse_expiry(session.get("expires")),
        credits=credits,
        user_paygate_tier=user_paygate_tier,
    )
