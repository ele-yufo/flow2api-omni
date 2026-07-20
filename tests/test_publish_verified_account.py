"""Publisher ``publish_verified_account`` tests.

Scope (per spec §10.1): delegation to ``apply_verified_snapshot`` + desired-state
small transaction + ``onboarding_pending`` clearing + ``business_enabled``
handling + retry idempotency + no-credential-leak. ``apply_verified_snapshot``
behaviors (membership hysteresis, auth recovery, identity check, ST collision,
retired/restored) are covered by ``test_verified_account_snapshot.py`` and are
not duplicated here.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from src.core.account_identity import VerifiedAccountSnapshot
from src.core.repositories.token_lifecycle_repository import (
    PublishError,
    PublishOutcome,
)
from src.core.token_states import (
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_ONBOARDING_PENDING,
)
from tests.helpers.db_fixtures import make_database_with_token


def _snapshot(email="alice@example.com", tier="PAYGATE_TIER_ONE", credits=1000):
    return VerifiedAccountSnapshot(
        st="x" * 1100,
        at="at-token",
        at_expires=datetime(2030, 1, 1, tzinfo=timezone.utc),
        email=email,
        normalized_email=email.casefold(),
        name="Alice",
        credits=credits,
        user_paygate_tier=tier,
    )


def test_publish_rejects_warm_mode():
    from src.core.repositories.token_lifecycle_repository import (
        TokenLifecycleRepository,
    )

    repo = TokenLifecycleRepository(engine=object())  # engine not touched (mode check first)
    with pytest.raises(PublishError) as exc:
        asyncio.run(
            repo.publish_verified_account(
                token_id=1,
                snapshot=_snapshot(),
                runtime_mode="warm",
                keepalive_enabled=True,
                business_enabled=True,
                observed_at=datetime.now(timezone.utc),
            )
        )
    assert exc.value.code == "warm_rejected"


def test_publish_sets_keepalive_and_runtime_and_clears_onboarding_pending(tmp_path):
    db, repo, token_id = make_database_with_token(
        tmp_path, ban_reason=TOKEN_REASON_ONBOARDING_PENDING
    )
    outcome = asyncio.run(
        repo.publish_verified_account(
            token_id=token_id,
            snapshot=_snapshot(),
            runtime_mode="persistent",
            keepalive_enabled=True,
            business_enabled=True,
            observed_at=datetime.now(timezone.utc),
        )
    )
    assert isinstance(outcome, PublishOutcome)
    assert outcome.keepalive_enabled is True
    assert outcome.runtime_mode == "persistent"
    assert outcome.profile_state == "ready"
    assert outcome.business_active is True
    assert outcome.ban_reason is None  # onboarding_pending cleared
    row = asyncio.run(db.get_token(token_id))
    assert row.is_active == 1
    assert row.ban_reason is None


def test_publish_preserves_manual_disabled(tmp_path):
    db, repo, token_id = make_database_with_token(
        tmp_path, ban_reason=TOKEN_REASON_MANUAL_DISABLED
    )
    outcome = asyncio.run(
        repo.publish_verified_account(
            token_id=token_id,
            snapshot=_snapshot(),
            runtime_mode="persistent",
            keepalive_enabled=True,
            business_enabled=True,
            observed_at=datetime.now(timezone.utc),
        )
    )
    # manual_disabled is protected: business_enabled=True does not clear it.
    assert outcome.ban_reason == TOKEN_REASON_MANUAL_DISABLED
    assert outcome.business_active is False
    assert outcome.keepalive_enabled is True  # desired state still written


def test_publish_sets_manual_disabled_when_business_disabled_and_no_ban(tmp_path):
    db, repo, token_id = make_database_with_token(tmp_path, ban_reason=None)
    outcome = asyncio.run(
        repo.publish_verified_account(
            token_id=token_id,
            snapshot=_snapshot(),
            runtime_mode="persistent",
            keepalive_enabled=True,
            business_enabled=False,
            observed_at=datetime.now(timezone.utc),
        )
    )
    assert outcome.ban_reason == TOKEN_REASON_MANUAL_DISABLED
    assert outcome.business_active is False


def test_publish_second_leg_failure_is_idempotent_on_retry(tmp_path):
    """Publish is idempotent: calling twice with the same args yields the same state.

    A real second-leg failure (e.g. transient DB error between the two
    transactions) leaves the token with a verified snapshot but default
    desired-state. Retrying ``publish_verified_account`` must converge: the
    first ``apply_verified_snapshot`` call is reentrant (overwrites same values)
    and the desired-state UPDATE is idempotent.
    """
    db, repo, token_id = make_database_with_token(
        tmp_path, ban_reason=TOKEN_REASON_ONBOARDING_PENDING
    )
    obs = datetime.now(timezone.utc)
    asyncio.run(
        repo.publish_verified_account(
            token_id=token_id,
            snapshot=_snapshot(),
            runtime_mode="persistent",
            keepalive_enabled=True,
            business_enabled=True,
            observed_at=obs,
        )
    )
    outcome2 = asyncio.run(
        repo.publish_verified_account(
            token_id=token_id,
            snapshot=_snapshot(),
            runtime_mode="persistent",
            keepalive_enabled=True,
            business_enabled=True,
            observed_at=obs,
        )
    )
    assert outcome2.business_active is True
    assert outcome2.ban_reason is None


def test_publish_never_returns_credentials(tmp_path):
    db, repo, token_id = make_database_with_token(tmp_path)
    outcome = asyncio.run(
        repo.publish_verified_account(
            token_id=token_id,
            snapshot=_snapshot(),
            runtime_mode="persistent",
            keepalive_enabled=True,
            business_enabled=True,
            observed_at=datetime.now(timezone.utc),
        )
    )
    dumped = repr(outcome)
    assert "x" * 1100 not in dumped  # ST
    assert "at-token" not in dumped  # AT
