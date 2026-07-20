"""Typed, immutable values shared by keepalive services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class RuntimeMode(str, Enum):
    """Browser lifetime policy for one keepalive account."""

    PERSISTENT = "persistent"
    WARM = "warm"


class FailureCode(str, Enum):
    """Stable machine-readable classifications for refresh failures."""

    PROFILE_MISSING = "profile_missing"
    PROFILE_BUSY = "profile_busy"
    IDENTITY_MISMATCH = "identity_mismatch"
    BROWSER_LAUNCH = "browser_launch"
    NAVIGATION = "navigation"
    SESSION_BODY = "session_body"
    COOKIE_MISSING = "cookie_missing"
    SESSION_REJECTED = "session_rejected"
    GRANT_EXPIRED = "grant_expired"
    CREDITS = "credits"
    NETWORK = "network"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """Side-effect-free result of one browser keepalive refresh attempt."""

    ok: bool
    code: Optional[FailureCode] = None
    detail: str = ""
    restart_browser: bool = False
    human_action: bool = False
    expiry: Optional[datetime] = None
    credits: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise TypeError("ok must be a bool")
        if self.code is not None and not isinstance(self.code, FailureCode):
            raise TypeError("code must be a FailureCode or None")
        if not isinstance(self.detail, str):
            raise TypeError("detail must be a string")
        if not isinstance(self.restart_browser, bool):
            raise TypeError("restart_browser must be a bool")
        if not isinstance(self.human_action, bool):
            raise TypeError("human_action must be a bool")
        if self.expiry is not None and not isinstance(self.expiry, datetime):
            raise TypeError("expiry must be a datetime or None")
        if self.credits is not None:
            if isinstance(self.credits, bool) or not isinstance(self.credits, int):
                raise TypeError("credits must be an integer or None")
            if self.credits < 0:
                raise ValueError("credits must be non-negative")
        self._validate_shape()

    def _validate_shape(self) -> None:
        if self.ok:
            if self.code is not None:
                raise ValueError("a successful outcome cannot have a failure code")
            if self.restart_browser or self.human_action:
                raise ValueError(
                    "a successful outcome cannot request restart or human action"
                )
            return
        if self.code is None:
            raise ValueError("a failed outcome must have a failure code")

    @classmethod
    def success(
        cls,
        *,
        detail: str = "",
        expiry: Optional[datetime] = None,
        credits: Optional[int] = None,
    ) -> "RefreshOutcome":
        """Build a validated success, including stale or unchanged expiry metadata."""

        return cls(
            ok=True,
            detail=detail,
            expiry=expiry,
            credits=credits,
        )

    @classmethod
    def failure(
        cls,
        code: FailureCode,
        *,
        detail: str = "",
        restart_browser: bool = False,
        human_action: bool = False,
        expiry: Optional[datetime] = None,
        credits: Optional[int] = None,
    ) -> "RefreshOutcome":
        """Build a validated failure with explicit recovery policy hints."""

        return cls(
            ok=False,
            code=code,
            detail=detail,
            restart_browser=restart_browser,
            human_action=human_action,
            expiry=expiry,
            credits=credits,
        )
