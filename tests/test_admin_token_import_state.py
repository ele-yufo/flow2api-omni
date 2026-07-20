"""Regression coverage for pool-state handling during verified token imports."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.api import admin
from src.core.account_identity import VerifiedAccountSnapshot
from src.core.models import Token
from src.core.token_states import (
    TOKEN_REASON_GRANT_EXPIRED,
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_ST_REVOKED,
)


OLD_ST = "old-session-token"
NEW_ST = "new-session-token"
NEW_AT = "new-access-token"
EMAIL = "existing@example.com"
AT_EXPIRES = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class RecordingTokenManager:
    """Offline token-manager double that exposes every import-side state change."""

    def __init__(self, existing: Token | None):
        self.existing = existing
        self.snapshot = VerifiedAccountSnapshot(
            email=EMAIL,
            normalized_email=EMAIL,
            name="Existing Account",
            st=NEW_ST,
            at=NEW_AT,
            at_expires=AT_EXPIRES,
            credits=800,
            user_paygate_tier="PAYGATE_TIER_ONE",
        )
        self.update_calls: list[dict] = []
        self.add_calls: list[dict] = []
        self.enable_calls: list[int] = []
        self.disable_calls: list[int] = []

    async def inspect_account(self, st: str) -> VerifiedAccountSnapshot:
        assert st == NEW_ST
        return self.snapshot

    async def find_token_by_email(self, email: str) -> Token | None:
        assert email == EMAIL
        return self.existing

    async def update_token(self, **kwargs) -> None:
        self.update_calls.append(kwargs)
        if self.existing is None:
            raise AssertionError("update_token called for a new account")

        snapshot = kwargs["verified_snapshot"]
        self.existing.st = snapshot.st
        self.existing.at = snapshot.at
        self.existing.at_expires = snapshot.at_expires
        self.existing.name = snapshot.name
        self.existing.credits = snapshot.credits
        self.existing.user_paygate_tier = snapshot.user_paygate_tier

        if kwargs.get("allow_auth_reactivate", True) and self.existing.ban_reason in {
            TOKEN_REASON_GRANT_EXPIRED,
            TOKEN_REASON_ST_REVOKED,
        }:
            self.existing.is_active = True
            self.existing.ban_reason = None

    async def add_token(self, **kwargs) -> Token:
        self.add_calls.append(kwargs)
        if self.existing is not None:
            raise AssertionError("add_token called for an existing account")
        return Token(
            id=99,
            st=kwargs["verified_snapshot"].st,
            at=kwargs["verified_snapshot"].at,
            at_expires=kwargs["verified_snapshot"].at_expires,
            email=kwargs["verified_snapshot"].email,
            name=kwargs["verified_snapshot"].name,
            credits=kwargs["verified_snapshot"].credits,
            user_paygate_tier=kwargs["verified_snapshot"].user_paygate_tier,
            is_active=kwargs["is_active"],
            ban_reason=kwargs["ban_reason"],
        )

    async def enable_token(self, token_id: int) -> None:
        self.enable_calls.append(token_id)
        if self.existing is None:
            raise AssertionError("enable_token called without an existing account")
        self.existing.is_active = True
        self.existing.ban_reason = None

    async def disable_token(self, token_id: int) -> None:
        self.disable_calls.append(token_id)
        if self.existing is None:
            raise AssertionError("disable_token called without an existing account")
        self.existing.is_active = False
        self.existing.ban_reason = TOKEN_REASON_MANUAL_DISABLED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_is_active", "existing_ban_reason", "import_fields"),
    [
        pytest.param(False, TOKEN_REASON_ST_REVOKED, {}, id="default-active-does-not-reactivate"),
        pytest.param(False, TOKEN_REASON_MANUAL_DISABLED, {"is_active": True}, id="explicit-active-does-not-reactivate"),
        pytest.param(True, None, {"is_active": False}, id="explicit-inactive-does-not-disable"),
        pytest.param(True, None, {"is_active": True}, id="matching-explicit-state-remains-unchanged"),
    ],
)
async def test_existing_verified_import_preserves_pool_state(
    monkeypatch,
    existing_is_active,
    existing_ban_reason,
    import_fields,
):
    existing = Token(
        id=23,
        st=OLD_ST,
        email=EMAIL,
        is_active=existing_is_active,
        ban_reason=existing_ban_reason,
    )
    manager = RecordingTokenManager(existing)
    monkeypatch.setattr(admin, "token_manager", manager)
    request = admin.ImportTokensRequest(
        tokens=[admin.ImportTokenItem(session_token=NEW_ST, **import_fields)]
    )

    result = await admin.import_tokens(request, token="offline-admin-token")

    assert result["added"] == 0
    assert result["updated"] == 1
    assert result["errors"] is None
    assert existing.st == NEW_ST
    assert existing.at == NEW_AT
    assert (existing.is_active, existing.ban_reason) == (
        existing_is_active,
        existing_ban_reason,
    )
    assert len(manager.update_calls) == 1
    assert manager.update_calls[0]["allow_auth_reactivate"] is False
    assert manager.enable_calls == []
    assert manager.disable_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("import_fields", "expected_is_active", "expected_ban_reason"),
    [
        pytest.param({}, True, None, id="default-active"),
        pytest.param({"is_active": False}, False, TOKEN_REASON_MANUAL_DISABLED, id="explicit-inactive"),
    ],
)
async def test_new_verified_import_honors_requested_pool_state(
    monkeypatch,
    import_fields,
    expected_is_active,
    expected_ban_reason,
):
    manager = RecordingTokenManager(existing=None)
    monkeypatch.setattr(admin, "token_manager", manager)
    request = admin.ImportTokensRequest(
        tokens=[admin.ImportTokenItem(session_token=NEW_ST, **import_fields)]
    )

    result = await admin.import_tokens(request, token="offline-admin-token")

    assert result["added"] == 1
    assert result["updated"] == 0
    assert result["errors"] is None
    assert len(manager.add_calls) == 1
    assert manager.add_calls[0]["is_active"] is expected_is_active
    assert manager.add_calls[0]["ban_reason"] == expected_ban_reason
    assert manager.enable_calls == []
    assert manager.disable_calls == []
