"""Strict Google account inspection tests."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.services.tokens.account_identity import (
    AccountIdentityError,
    inspect_account_identity,
    normalize_account_email,
)


LONG_ST = "eyJ" + "s" * 1100
ROTATED_ST = "eyJ" + "r" * 1100


def test_normalize_account_email_only_strips_and_casefolds():
    assert normalize_account_email("  User.Name+tag@GMAIL.com ") == "user.name+tag@gmail.com"
    assert normalize_account_email("user-name@gmail.com") != normalize_account_email(
        "user.name@gmail.com"
    )


@pytest.mark.asyncio
async def test_inspect_account_identity_returns_verified_real_snapshot():
    flow_client = AsyncMock()
    flow_client.st_to_at.return_value = {
        "access_token": "real-at",
        "expires": "2026-07-20T10:11:12Z",
        "rotated_st": ROTATED_ST,
        "user": {"email": "Ruby@Example.com", "name": "Ruby"},
    }
    flow_client.get_credits.return_value = {
        "credits": 960,
        "userPaygateTier": "PAYGATE_TIER_ONE",
    }

    snapshot = await inspect_account_identity(flow_client, LONG_ST)

    assert snapshot.email == "Ruby@Example.com"
    assert snapshot.normalized_email == "ruby@example.com"
    assert snapshot.name == "Ruby"
    assert snapshot.st == ROTATED_ST
    assert snapshot.at == "real-at"
    assert snapshot.at_expires == datetime(2026, 7, 20, 10, 11, 12, tzinfo=timezone.utc)
    assert snapshot.credits == 960
    assert snapshot.user_paygate_tier == "PAYGATE_TIER_ONE"
    flow_client.get_credits.assert_awaited_once_with("real-at")


@pytest.mark.asyncio
async def test_inspect_account_identity_keeps_original_when_rotated_st_is_invalid():
    flow_client = AsyncMock()
    flow_client.st_to_at.return_value = {
        "access_token": "real-at",
        "expires": None,
        "rotated_st": "undefined",
        "user": {"email": "x@example.com"},
    }
    flow_client.get_credits.return_value = {
        "credits": 1,
        "userPaygateTier": "PAYGATE_TIER_NOT_PAID",
    }

    snapshot = await inspect_account_identity(flow_client, LONG_ST)

    assert snapshot.st == LONG_ST
    assert snapshot.at_expires is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_result",
    [
        {"expires": None, "user": {"email": "x@example.com"}},
        {"access_token": "at", "expires": None, "user": {}},
        {"access_token": "at", "expires": None, "user": {"email": "   "}},
    ],
)
async def test_inspect_account_identity_rejects_missing_access_token_or_identity(session_result):
    flow_client = AsyncMock()
    flow_client.st_to_at.return_value = session_result

    with pytest.raises(AccountIdentityError):
        await inspect_account_identity(flow_client, LONG_ST)

    flow_client.get_credits.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspect_account_identity_requires_successful_credits_probe():
    flow_client = AsyncMock()
    flow_client.st_to_at.return_value = {
        "access_token": "real-at",
        "expires": None,
        "user": {"email": "x@example.com"},
    }
    flow_client.get_credits.side_effect = RuntimeError("401 UNAUTHENTICATED")

    with pytest.raises(AccountIdentityError, match="credits"):
        await inspect_account_identity(flow_client, LONG_ST)


@pytest.mark.asyncio
@pytest.mark.parametrize("credits_result", [{}, {"credits": "not-a-number"}, None])
async def test_inspect_account_identity_rejects_invalid_credits_payload(credits_result):
    flow_client = AsyncMock()
    flow_client.st_to_at.return_value = {
        "access_token": "real-at",
        "expires": None,
        "user": {"email": "x@example.com"},
    }
    flow_client.get_credits.return_value = credits_result

    with pytest.raises(AccountIdentityError, match="credits"):
        await inspect_account_identity(flow_client, LONG_ST)
