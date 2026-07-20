"""Atomic verified-account snapshot persistence tests."""

import asyncio
from datetime import datetime, timezone

import pytest

from src.core.token_states import (
    TOKEN_REASON_429_RATE_LIMIT,
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_MEMBERSHIP_EXPIRED,
)
from src.services.tokens.account_identity import VerifiedAccountSnapshot


LONG_ST_A = "eyJ" + "a" * 1100
LONG_ST_B = "eyJ" + "b" * 1100


def _snapshot(*, email="owner@example.com", st=LONG_ST_B, tier="PAYGATE_TIER_ONE"):
    return VerifiedAccountSnapshot(
        email=email,
        normalized_email=email.strip().casefold(),
        name="Owner",
        st=st,
        at="verified-at",
        at_expires=datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
        credits=900,
        user_paygate_tier=tier,
    )


def test_identity_mismatch_and_st_collision_leave_all_state_unchanged(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(st=LONG_ST_A, email="owner@example.com", remark="original")
        )
        await db.add_token(Token(st=LONG_ST_B, email="other@example.com"))
        before_token = await db.get_token(token_id)
        before_lifecycle = await db.get_token_lifecycle(token_id)

        with pytest.raises(ValueError, match="identity"):
            await db.apply_verified_account_snapshot(
                token_id, _snapshot(email="wrong@example.com", st="eyJ" + "c" * 1100)
            )
        after_mismatch_token = await db.get_token(token_id)
        after_mismatch_lifecycle = await db.get_token_lifecycle(token_id)

        with pytest.raises(ValueError, match="session token"):
            await db.apply_verified_account_snapshot(token_id, _snapshot(st=LONG_ST_B))
        after_collision_token = await db.get_token(token_id)
        after_collision_lifecycle = await db.get_token_lifecycle(token_id)

        return (
            before_token,
            before_lifecycle,
            after_mismatch_token,
            after_mismatch_lifecycle,
            after_collision_token,
            after_collision_lifecycle,
        )

    values = asyncio.run(run())
    assert values[2] == values[0]
    assert values[3] == values[1]
    assert values[4] == values[0]
    assert values[5] == values[1]


def test_verified_snapshot_updates_credentials_without_touching_usage_or_429(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        last_used = datetime(2026, 7, 18, 1, 2, 3, tzinfo=timezone.utc)
        token_id = await db.add_token(
            Token(
                st=LONG_ST_A,
                email="owner@example.com",
                at="old-at",
                credits=10,
                is_active=False,
                ban_reason=TOKEN_REASON_429_RATE_LIMIT,
            )
        )
        await db.update_token(token_id, last_used_at=last_used, use_count=7)
        result = await db.apply_verified_account_snapshot(token_id, _snapshot())
        token = await db.get_token(token_id)
        lifecycle = await db.get_token_lifecycle(token_id)
        return result, token, lifecycle, last_used

    result, token, lifecycle, last_used = asyncio.run(run())
    assert token.st == LONG_ST_B
    assert token.at == "verified-at"
    assert token.credits == 900
    assert token.is_active is False
    assert token.ban_reason == TOKEN_REASON_429_RATE_LIMIT
    assert token.last_used_at == last_used
    assert token.use_count == 7
    assert lifecycle.verified_email == "owner@example.com"
    assert lifecycle.last_keepalive_status == "success"
    assert result.pool_transition is None


def test_two_free_observations_retire_then_two_paid_restore_only_owned_ban(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(
                st=LONG_ST_A,
                email="owner@example.com",
                user_paygate_tier="PAYGATE_TIER_ONE",
                is_active=True,
            )
        )

        first_free = await db.apply_verified_account_snapshot(
            token_id, _snapshot(tier="PAYGATE_TIER_NOT_PAID")
        )
        after_first_free = await db.get_token(token_id)
        second_free = await db.apply_verified_account_snapshot(
            token_id, _snapshot(tier="PAYGATE_TIER_NOT_PAID")
        )
        retired = await db.get_token(token_id)

        first_paid = await db.apply_verified_account_snapshot(
            token_id, _snapshot(tier="PAYGATE_TIER_TWO")
        )
        after_first_paid = await db.get_token(token_id)
        second_paid = await db.apply_verified_account_snapshot(
            token_id, _snapshot(tier="PAYGATE_TIER_ONE")
        )
        restored = await db.get_token(token_id)
        return (
            first_free,
            after_first_free,
            second_free,
            retired,
            first_paid,
            after_first_paid,
            second_paid,
            restored,
        )

    values = asyncio.run(run())
    assert values[1].is_active is True
    assert values[0].pool_transition is None
    assert values[3].is_active is False
    assert values[3].ban_reason == TOKEN_REASON_MEMBERSHIP_EXPIRED
    assert values[2].pool_transition == "retired"
    assert values[5].is_active is False
    assert values[4].pool_transition is None
    assert values[7].is_active is True
    assert values[7].ban_reason is None
    assert values[6].pool_transition == "restored"


def test_paid_observations_never_restore_manual_disable(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(
                st=LONG_ST_A,
                email="owner@example.com",
                is_active=False,
                ban_reason=TOKEN_REASON_MANUAL_DISABLED,
            )
        )
        await db.apply_verified_account_snapshot(token_id, _snapshot())
        await db.apply_verified_account_snapshot(token_id, _snapshot())
        return await db.get_token(token_id)

    token = asyncio.run(run())
    assert token.is_active is False
    assert token.ban_reason == TOKEN_REASON_MANUAL_DISABLED
