"""Persistence coverage for token lifecycle and safe onboarding jobs.

All tests use the temp database fixture. They intentionally exercise migrations and
SQLite locking without starting services, browsers, or touching the production DB.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


FORBIDDEN_ONBOARDING_COLUMNS = {
    "st",
    "at",
    "cookie",
    "cookies",
    "access_token",
    "session_token",
    "token_id",
    "profile_path",
    "path",
    "command",
}

REQUIRED_ONBOARDING_COLUMNS = {
    "target_token_id",
    "resolved_token_id",
    "phase",
    "state",
    "browser_pid",
    "browser_start_ticks",
    "discovered_email",
    "discovered_tier",
    "discovered_credits",
    "discovered_at_expires",
    "project_count",
    "profile_ready",
    "conflict_status",
    "conflict_policy",
    "requested_business_enabled",
    "requested_keepalive_enabled",
    "requested_runtime_mode",
    "error_code",
    "error_message",
    "expires_at",
    "started_at",
    "completed_at",
    "cancelled_at",
}


def test_fresh_schema_and_token_lifecycle_creation_deletion(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()

        async with db._connect() as connection:
            lifecycle_columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(token_lifecycle)")).fetchall()
            }
            onboarding_columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(onboarding_jobs)")).fetchall()
            }

        token_id = await db.add_token(
            Token(st="fresh-token-" + "x" * 100, email="fresh@example.com")
        )
        lifecycle = await db.get_token_lifecycle(token_id)
        await db.delete_token(token_id)

        return lifecycle_columns, onboarding_columns, lifecycle, await db.get_token_lifecycle(token_id)

    lifecycle_columns, onboarding_columns, lifecycle, lifecycle_after_delete = asyncio.run(run())
    assert {
        "token_id",
        "membership_confirmed_status",
        "membership_candidate",
        "membership_candidate_count",
        "keepalive_enabled",
        "runtime_mode",
        "profile_state",
        "verified_email",
        "next_due_at",
        "last_failure_at",
        "last_failure_code",
        "last_failure_detail",
        "last_observed_tier",
        "last_observed_at",
        "retired_at",
        "restored_at",
        "last_alert_code",
        "last_alert_at",
    } <= lifecycle_columns
    assert REQUIRED_ONBOARDING_COLUMNS <= onboarding_columns
    assert FORBIDDEN_ONBOARDING_COLUMNS.isdisjoint(onboarding_columns)
    assert lifecycle.token_id > 0
    assert lifecycle.membership_confirmed_status == "active"
    assert lifecycle.membership_candidate == "unknown"
    assert lifecycle.membership_candidate_count == 0
    assert lifecycle.keepalive_enabled is False
    assert lifecycle.runtime_mode == "warm"
    assert lifecycle.profile_state == "unprovisioned"
    assert lifecycle.verified_email is None
    assert lifecycle_after_delete is None


def test_legacy_backfill_is_idempotent_and_preserves_manual_changes(temp_db_path):
    from src.core.database import Database

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()

        async with db.transaction() as connection:
            enabled_cursor = await connection.execute(
                "INSERT INTO tokens (st, email, is_active) VALUES (?, ?, 1)",
                ("legacy-enabled-" + "a" * 100, "enabled@example.com"),
            )
            other_cursor = await connection.execute(
                "INSERT INTO tokens (st, email, is_active) VALUES (?, ?, 1)",
                ("legacy-other-" + "b" * 100, "other@example.com"),
            )
            manual_cursor = await connection.execute(
                "INSERT INTO tokens (st, email, is_active, ban_reason) VALUES (?, ?, 0, NULL)",
                ("legacy-manual-" + "c" * 100, "manual@example.com"),
            )
            enabled_id = enabled_cursor.lastrowid
            other_id = other_cursor.lastrowid
            manual_id = manual_cursor.lastrowid
            await connection.execute("DROP TABLE onboarding_jobs")
            await connection.execute("DROP TABLE token_lifecycle")

        config_dict = {"keepalive": {"browser_token_ids": f"{enabled_id}"}}
        await db.check_and_migrate_db(config_dict)
        first = {
            token_id: await db.get_token_lifecycle(token_id)
            for token_id in (enabled_id, other_id, manual_id)
        }

        await db.set_token_desired_state(
            enabled_id,
            keepalive_enabled=False,
            runtime_mode="warm",
            profile_state="ready",
        )
        await db.check_and_migrate_db(config_dict)
        second = {
            token_id: await db.get_token_lifecycle(token_id)
            for token_id in (enabled_id, other_id, manual_id)
        }
        manual_token = await db.get_token(manual_id)
        return enabled_id, other_id, manual_id, first, second, manual_token

    enabled_id, other_id, manual_id, first, second, manual_token = asyncio.run(run())
    assert first[enabled_id].keepalive_enabled is True
    assert first[enabled_id].runtime_mode == "persistent"
    assert first[enabled_id].profile_state == "ready"
    assert first[other_id].keepalive_enabled is False
    assert first[other_id].runtime_mode == "warm"
    assert first[other_id].profile_state == "unprovisioned"
    assert first[manual_id].keepalive_enabled is False
    assert first[manual_id].runtime_mode == "warm"
    assert first[manual_id].profile_state == "unprovisioned"
    assert manual_token.ban_reason == "manual_disabled"
    assert second[enabled_id].keepalive_enabled is False
    assert second[enabled_id].runtime_mode == "warm"
    assert second[enabled_id].profile_state == "ready"
    assert second[other_id] == first[other_id]
    assert second[manual_id] == first[manual_id]


def test_keepalive_enabled_selection_ignores_business_is_active(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(st="disabled-business-" + "d" * 100, email="disabled@example.com")
        )
        await db.update_token(token_id, is_active=False, ban_reason="manual_disabled")
        await db.set_token_desired_state(
            token_id,
            keepalive_enabled=True,
            runtime_mode="persistent",
            profile_state="ready",
        )
        lifecycles = await db.list_enabled_token_lifecycles()
        tokens = await db.list_keepalive_enabled_tokens()
        return token_id, lifecycles, tokens

    token_id, lifecycles, tokens = asyncio.run(run())
    assert token_id in {lifecycle.token_id for lifecycle in lifecycles}
    assert token_id in {token.id for token in tokens}
    selected = next(token for token in tokens if token.id == token_id)
    assert selected.is_active is False
    assert selected.ban_reason == "manual_disabled"
    assert selected.keepalive_enabled is True
    assert selected.runtime_mode == "persistent"


def test_desired_state_partial_updates_preserve_unspecified_fields(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(st="partial-state-" + "p" * 100, email="partial@example.com")
        )
        await db.set_token_desired_state(
            token_id,
            keepalive_enabled=True,
            runtime_mode="warm",
            profile_state="ready",
        )
        await db.set_token_desired_state(token_id, runtime_mode="persistent")
        after_mode = await db.get_token_lifecycle(token_id)
        await db.set_token_desired_state(token_id, keepalive_enabled=False)
        after_toggle = await db.get_token_lifecycle(token_id)
        return after_mode, after_toggle

    after_mode, after_toggle = asyncio.run(run())
    assert after_mode.keepalive_enabled is True
    assert after_mode.runtime_mode == "persistent"
    assert after_mode.profile_state == "ready"
    assert after_toggle.keepalive_enabled is False
    assert after_toggle.runtime_mode == "persistent"
    assert after_toggle.profile_state == "ready"


def test_membership_state_round_trips_canonical_state_model(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token
    from src.core.token_states import (
        AccountLifecycleState,
        AccountLifecycleStatus,
        TierClassification,
    )

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(st="membership-state-" + "m" * 100, email="membership@example.com")
        )
        observed_at = datetime(2026, 7, 19, 2, 3, 4, tzinfo=timezone.utc)
        candidate = AccountLifecycleState(
            confirmed_status=AccountLifecycleStatus.ACTIVE,
            candidate=TierClassification.FREE,
            candidate_count=1,
        )
        await db.update_token_membership_state(
            token_id,
            candidate,
            observed_tier="PAYGATE_TIER_NOT_PAID",
            observed_at=observed_at,
        )
        retired = AccountLifecycleState(confirmed_status=AccountLifecycleStatus.RETIRED)
        await db.update_token_membership_state(
            token_id,
            retired,
            observed_tier="PAYGATE_TIER_NOT_PAID",
            observed_at=observed_at,
        )
        restored = AccountLifecycleState(confirmed_status=AccountLifecycleStatus.ACTIVE)
        await db.update_token_membership_state(
            token_id,
            restored,
            observed_tier="PAYGATE_TIER_ONE",
            observed_at=observed_at,
        )
        await db.update_token_lifecycle_alert(
            token_id,
            alert_code="ACCOUNT_RESTORED",
            alerted_at=observed_at,
        )
        return await db.get_token_lifecycle(token_id)

    lifecycle = asyncio.run(run())
    assert lifecycle.account_lifecycle_state == AccountLifecycleState(
        confirmed_status=AccountLifecycleStatus.ACTIVE
    )
    assert lifecycle.last_observed_tier == "PAYGATE_TIER_ONE"
    assert lifecycle.last_observed_at is not None
    assert lifecycle.retired_at is not None
    assert lifecycle.restored_at is not None
    assert lifecycle.last_alert_code == "ACCOUNT_RESTORED"
    assert lifecycle.last_alert_at is not None


def test_keepalive_telemetry_clears_error_with_explicit_sql(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(st="telemetry-" + "e" * 100, email="telemetry@example.com")
        )
        attempt_at = datetime(2026, 7, 19, 1, 2, 3, tzinfo=timezone.utc)
        next_due_at = datetime(2026, 7, 19, 1, 22, 3, tzinfo=timezone.utc)
        await db.update_token_keepalive_telemetry(
            token_id,
            status="failed",
            error="network unavailable",
            error_code="NETWORK_ERROR",
            attempted_at=attempt_at,
            next_due_at=next_due_at,
        )
        failed = await db.get_token_lifecycle(token_id)
        await db.update_token_keepalive_telemetry(
            token_id,
            status="success",
            error=None,
            attempted_at=attempt_at,
            next_due_at=next_due_at,
        )
        succeeded = await db.get_token_lifecycle(token_id)
        return failed, succeeded

    failed, succeeded = asyncio.run(run())
    assert failed.last_keepalive_status == "failed"
    assert failed.last_keepalive_error == "network unavailable"
    assert failed.last_failure_code == "NETWORK_ERROR"
    assert failed.last_failure_detail == "network unavailable"
    assert failed.last_failure_at is not None
    assert failed.next_due_at is not None
    assert failed.keepalive_failure_count == 1
    assert succeeded.last_keepalive_status == "success"
    assert succeeded.last_keepalive_error is None
    assert succeeded.keepalive_failure_count == 0
    assert succeeded.last_keepalive_success_at is not None
    assert succeeded.last_failure_code == "NETWORK_ERROR"
    assert succeeded.next_due_at is not None


def test_alert_state_round_trips_for_restart_deduplication(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(st="alert-state-" + "h" * 100, email="alerts@example.com")
        )
        await db.update_token_alert_state(
            token_id,
            alert_code="network",
            episode=3,
            alerted=True,
        )
        failed = await db.get_token_lifecycle(token_id)
        await db.update_token_alert_state(
            token_id,
            alert_code=None,
            episode=3,
            alerted=False,
        )
        recovered = await db.get_token_lifecycle(token_id)
        return failed, recovered

    failed, recovered = asyncio.run(run())
    assert failed.last_alert_code == "network"
    assert failed.alert_episode == 3
    assert failed.alerted is True
    assert failed.last_alert_at is not None
    assert recovered.last_alert_code is None
    assert recovered.alert_episode == 3
    assert recovered.alerted is False


def test_onboarding_crud_stores_only_resumable_safe_fields(temp_db_path):
    from src.core.database import Database
    from src.core.models import OnboardingJob, Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(st="onboarding-token-" + "f" * 100, email="onboarding@example.com")
        )
        expires_at = datetime(2026, 7, 19, 5, 0, 0, tzinfo=timezone.utc)
        job_id = await db.create_onboarding_job(
            OnboardingJob(
                target_token_id=token_id,
                phase="browser_start",
                state="pending",
                conflict_policy="reuse_target",
                requested_business_enabled=False,
                requested_keepalive_enabled=True,
                requested_runtime_mode="persistent",
                expires_at=expires_at,
            )
        )
        created = await db.get_onboarding_job(job_id)
        with pytest.raises(ValueError):
            await db.update_onboarding_job(job_id, profile_path="/must/not/persist")
        await db.update_onboarding_job(
            job_id,
            browser_pid=321,
            browser_start_ticks=654,
            discovered_email="verified@example.com",
            discovered_tier="PAYGATE_TIER_ONE",
            discovered_credits=1000,
            discovered_at_expires=expires_at,
            project_count=4,
            profile_ready=True,
            conflict_status="archived_and_replaced",
            resolved_token_id=token_id,
        )
        discovered = await db.get_onboarding_job(job_id)
        await db.update_onboarding_job_state(
            job_id,
            "failed",
            phase="verify",
            error_code="LOGIN_REQUIRED",
            error_message="login required",
        )
        failed = await db.get_onboarding_job(job_id)
        await db.update_onboarding_job_state(
            job_id,
            "running",
            phase="browser_start",
            clear_error=True,
        )
        running = await db.get_onboarding_job(job_id)
        listed = await db.list_onboarding_jobs(target_token_id=token_id)
        await db.update_onboarding_job_state(
            job_id,
            "completed",
            phase="completed",
            clear_error=True,
        )
        completed = await db.get_onboarding_job(job_id)
        await db.delete_token(token_id)
        detached = await db.get_onboarding_job(job_id)
        await db.delete_onboarding_job(job_id)
        deleted = await db.get_onboarding_job(job_id)

        async with db._connect() as connection:
            columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(onboarding_jobs)")).fetchall()
            }
        return created, discovered, failed, running, listed, completed, detached, deleted, columns

    created, discovered, failed, running, listed, completed, detached, deleted, columns = asyncio.run(run())
    assert created.state == "pending"
    assert created.phase == "browser_start"
    assert created.requested_business_enabled is False
    assert created.requested_keepalive_enabled is True
    assert created.requested_runtime_mode == "persistent"
    assert discovered.browser_pid == 321
    assert discovered.browser_start_ticks == 654
    assert discovered.discovered_email == "verified@example.com"
    assert discovered.discovered_credits == 1000
    assert discovered.project_count == 4
    assert discovered.profile_ready is True
    assert discovered.conflict_status == "archived_and_replaced"
    assert failed.phase == "verify"
    assert failed.state == "failed"
    assert failed.error_code == "LOGIN_REQUIRED"
    assert failed.error_message == "login required"
    assert running.state == "running"
    assert running.error_code is None
    assert running.error_message is None
    assert running.completed_at is None
    assert [job.job_id for job in listed] == [created.job_id]
    assert completed.state == "completed"
    assert completed.phase == "completed"
    assert completed.completed_at is not None
    assert detached.target_token_id is None
    assert detached.resolved_token_id is None
    assert deleted is None
    assert REQUIRED_ONBOARDING_COLUMNS <= columns
    assert FORBIDDEN_ONBOARDING_COLUMNS.isdisjoint(columns)

    for unsafe_field in ("st", "at", "cookies", "profile_path", "path", "command"):
        with pytest.raises(Exception):
            OnboardingJob(state="pending", **{unsafe_field: "must-not-be-stored"})


def test_provisional_schema_migrates_to_approved_safe_schema_idempotently(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(st="migration-token-" + "h" * 100, email="migration@example.com")
        )
        async with db.transaction() as connection:
            await connection.execute("DROP TABLE onboarding_jobs")
            await connection.execute("DROP TABLE token_lifecycle")
            await connection.execute("""
                CREATE TABLE token_lifecycle (
                    token_id INTEGER PRIMARY KEY,
                    membership_confirmed_status TEXT NOT NULL DEFAULT 'active',
                    membership_candidate TEXT NOT NULL DEFAULT 'unknown',
                    membership_candidate_count INTEGER NOT NULL DEFAULT 0,
                    keepalive_enabled BOOLEAN NOT NULL DEFAULT 0,
                    runtime_mode TEXT NOT NULL DEFAULT 'warm',
                    profile_state TEXT NOT NULL DEFAULT 'unprovisioned',
                    verified_email TEXT,
                    last_keepalive_at TIMESTAMP,
                    last_keepalive_success_at TIMESTAMP,
                    last_keepalive_status TEXT,
                    last_keepalive_error TEXT,
                    keepalive_failure_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await connection.execute(
                "INSERT INTO token_lifecycle (token_id, keepalive_enabled) VALUES (?, 1)",
                (token_id,),
            )
            await connection.execute("""
                CREATE TABLE onboarding_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER,
                    state TEXT NOT NULL DEFAULT 'pending',
                    profile_path TEXT,
                    verified_email TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP
                )
            """)
            await connection.execute(
                """
                INSERT INTO onboarding_jobs (
                    job_id, token_id, state, profile_path, verified_email, error_message
                ) VALUES ('legacy-job', ?, 'failed', '/unsafe/profile', 'old@example.com', 'old error')
                """,
                (token_id,),
            )

        await db.check_and_migrate_db({"keepalive": {"browser_token_ids": ""}})
        first_lifecycle = await db.get_token_lifecycle(token_id)
        first_job = await db.get_onboarding_job("legacy-job")
        await db.check_and_migrate_db({"keepalive": {"browser_token_ids": ""}})
        second_lifecycle = await db.get_token_lifecycle(token_id)
        second_job = await db.get_onboarding_job("legacy-job")
        async with db._connect() as connection:
            lifecycle_columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(token_lifecycle)")).fetchall()
            }
            onboarding_columns = {
                row[1]
                for row in await (await connection.execute("PRAGMA table_info(onboarding_jobs)")).fetchall()
            }
        return (
            token_id,
            first_lifecycle,
            second_lifecycle,
            first_job,
            second_job,
            lifecycle_columns,
            onboarding_columns,
        )

    (
        token_id,
        first_lifecycle,
        second_lifecycle,
        first_job,
        second_job,
        lifecycle_columns,
        onboarding_columns,
    ) = asyncio.run(run())
    assert first_lifecycle.keepalive_enabled is True
    assert second_lifecycle == first_lifecycle
    assert first_job.target_token_id == token_id
    assert first_job.resolved_token_id is None
    assert first_job.discovered_email == "old@example.com"
    assert first_job.project_count is None
    assert first_job.profile_ready is None
    assert first_job.conflict_status is None
    assert first_job.error_message == "old error"
    assert second_job == first_job
    assert {
        "next_due_at",
        "last_failure_at",
        "last_failure_code",
        "last_failure_detail",
        "last_observed_tier",
        "last_observed_at",
        "retired_at",
        "restored_at",
        "last_alert_code",
        "last_alert_at",
    } <= lifecycle_columns
    assert REQUIRED_ONBOARDING_COLUMNS <= onboarding_columns
    assert FORBIDDEN_ONBOARDING_COLUMNS.isdisjoint(onboarding_columns)


def test_safe_onboarding_schema_adds_result_columns_idempotently(temp_db_path):
    from src.core.database import Database

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        async with db.transaction() as connection:
            await connection.execute("DROP TABLE onboarding_jobs")
            await connection.execute("""
                CREATE TABLE onboarding_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    target_token_id INTEGER,
                    resolved_token_id INTEGER,
                    phase TEXT NOT NULL DEFAULT 'created',
                    state TEXT NOT NULL DEFAULT 'pending',
                    browser_pid INTEGER,
                    browser_start_ticks INTEGER,
                    discovered_email TEXT,
                    discovered_tier TEXT,
                    discovered_credits INTEGER,
                    discovered_at_expires TIMESTAMP,
                    conflict_policy TEXT NOT NULL DEFAULT 'reject',
                    requested_business_enabled BOOLEAN NOT NULL DEFAULT 0,
                    requested_keepalive_enabled BOOLEAN NOT NULL DEFAULT 0,
                    requested_runtime_mode TEXT NOT NULL DEFAULT 'warm',
                    error_code TEXT,
                    error_message TEXT,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    cancelled_at TIMESTAMP
                )
            """)
            await connection.execute(
                """
                INSERT INTO onboarding_jobs (
                    job_id, phase, state, discovered_email, discovered_credits
                ) VALUES ('safe-old-job', 'completed', 'completed', ?, 700)
                """,
                ("safe@example.com",),
            )

        await db.check_and_migrate_db({"keepalive": {"browser_token_ids": ""}})
        first = await db.get_onboarding_job("safe-old-job")
        await db.update_onboarding_job(
            "safe-old-job",
            project_count=3,
            profile_ready=True,
            conflict_status="no_conflict",
        )
        await db.check_and_migrate_db({"keepalive": {"browser_token_ids": ""}})
        second = await db.get_onboarding_job("safe-old-job")
        return first, second

    first, second = asyncio.run(run())
    assert first.discovered_email == "safe@example.com"
    assert first.discovered_credits == 700
    assert first.project_count is None
    assert first.profile_ready is None
    assert first.conflict_status is None
    assert second.project_count == 3
    assert second.profile_ready is True
    assert second.conflict_status == "no_conflict"


def test_onboarding_account_completion_is_atomic_and_preserves_owned_bans(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token
    from src.core.token_states import (
        TOKEN_REASON_429_RATE_LIMIT,
        TOKEN_REASON_MANUAL_DISABLED,
        TOKEN_REASON_ONBOARDING_PENDING,
    )

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        pending_paid_id = await db.add_token(
            Token(
                st="pending-paid-" + "p" * 100,
                email="pending-paid@example.com",
                is_active=False,
                ban_reason=TOKEN_REASON_ONBOARDING_PENDING,
            )
        )
        pending_free_id = await db.add_token(
            Token(
                st="pending-free-" + "f" * 100,
                email="pending-free@example.com",
                is_active=False,
                ban_reason=TOKEN_REASON_ONBOARDING_PENDING,
            )
        )
        manual_id = await db.add_token(
            Token(
                st="manual-owned-" + "m" * 100,
                email="manual-owned@example.com",
                is_active=False,
                ban_reason=TOKEN_REASON_MANUAL_DISABLED,
            )
        )
        rate_limited_id = await db.add_token(
            Token(
                st="rate-owned-" + "r" * 100,
                email="rate-owned@example.com",
                is_active=False,
                ban_reason=TOKEN_REASON_429_RATE_LIMIT,
            )
        )

        await db.finalize_onboarding_account_state(
            pending_paid_id,
            keepalive_enabled=True,
            runtime_mode="persistent",
            enable_business_if_pending=True,
        )
        await db.finalize_onboarding_account_state(
            pending_free_id,
            keepalive_enabled=True,
            runtime_mode="warm",
            enable_business_if_pending=False,
        )
        for token_id in (manual_id, rate_limited_id):
            await db.finalize_onboarding_account_state(
                token_id,
                keepalive_enabled=True,
                runtime_mode="warm",
                enable_business_if_pending=True,
            )

        return {
            token_id: (
                await db.get_token(token_id),
                await db.get_token_lifecycle(token_id),
            )
            for token_id in (
                pending_paid_id,
                pending_free_id,
                manual_id,
                rate_limited_id,
            )
        }

    records = asyncio.run(run())
    pending_paid, pending_paid_lifecycle = next(
        value for value in records.values() if value[0].email == "pending-paid@example.com"
    )
    pending_free, pending_free_lifecycle = next(
        value for value in records.values() if value[0].email == "pending-free@example.com"
    )
    manual, _manual_lifecycle = next(
        value for value in records.values() if value[0].email == "manual-owned@example.com"
    )
    rate_limited, _rate_limited_lifecycle = next(
        value for value in records.values() if value[0].email == "rate-owned@example.com"
    )

    assert pending_paid.is_active is True
    assert pending_paid.ban_reason is None
    assert pending_paid_lifecycle.keepalive_enabled is True
    assert pending_paid_lifecycle.runtime_mode == "persistent"
    assert pending_paid_lifecycle.profile_state == "ready"
    assert pending_free.is_active is False
    assert pending_free.ban_reason == TOKEN_REASON_MANUAL_DISABLED
    assert pending_free_lifecycle.keepalive_enabled is True
    assert pending_free_lifecycle.profile_state == "ready"
    assert manual.is_active is False
    assert manual.ban_reason == TOKEN_REASON_MANUAL_DISABLED
    assert rate_limited.is_active is False
    assert rate_limited.ban_reason == TOKEN_REASON_429_RATE_LIMIT


@pytest.mark.parametrize(
    ("browser_pid", "browser_start_ticks"),
    [(4321, 9876), (4321, None), (None, 9876)],
)
def test_onboarding_claim_blocks_unreconciled_failed_browser_metadata(
    temp_db_path,
    browser_pid,
    browser_start_ticks,
):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        failed_job_id = await db.create_onboarding_job(OnboardingJob())
        next_job_id = await db.create_onboarding_job(OnboardingJob())
        assert await db.claim_onboarding_job(failed_job_id) is True
        await db.update_onboarding_job(
            failed_job_id,
            browser_pid=browser_pid,
            browser_start_ticks=browser_start_ticks,
        )
        await db.update_onboarding_job_state(
            failed_job_id,
            "failed",
            phase="stop_browser",
            error_code="process_stop_failed",
            error_message="safe failure",
        )
        blocked = await db.claim_onboarding_job(next_job_id)
        await db.update_onboarding_job(
            failed_job_id,
            browser_pid=None,
            browser_start_ticks=None,
        )
        blocked_after_clear = await db.claim_onboarding_job(next_job_id)
        await db.update_onboarding_job_state(
            failed_job_id,
            "cancelled",
            phase="cancelled",
            clear_error=True,
        )
        claimed = await db.claim_onboarding_job(next_job_id)
        return blocked, blocked_after_clear, claimed

    blocked, blocked_after_clear, claimed = asyncio.run(run())
    assert blocked is False
    assert blocked_after_clear is False
    assert claimed is True


def test_onboarding_failed_pre_migration_job_can_be_atomically_reclaimed_for_resume(
    temp_db_path,
):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        original_expires_at = datetime(2026, 7, 19, 8, 10, tzinfo=timezone.utc)
        refreshed_expires_at = original_expires_at + timedelta(hours=1)
        job_id = await db.create_onboarding_job(
            OnboardingJob(expires_at=original_expires_at)
        )
        assert await db.claim_onboarding_job(job_id) is True
        await db.update_onboarding_job(
            job_id,
            browser_pid=4321,
            browser_start_ticks=9876,
        )
        await db.update_onboarding_job_state(
            job_id,
            "failed",
            phase="verify_account",
            error_code="login_required",
            error_message="safe failure",
        )

        stale_claim = await db.claim_failed_onboarding_job_resume(
            job_id,
            expected_phase="verify_account",
            expected_error_code="login_required",
            expected_pid=4321,
            expected_start_ticks=9876,
            expected_expires_at=original_expires_at - timedelta(seconds=1),
            refreshed_expires_at=refreshed_expires_at,
        )
        after_stale_claim = await db.get_onboarding_job(job_id)
        claimed = await db.claim_failed_onboarding_job_resume(
            job_id,
            expected_phase="verify_account",
            expected_error_code="login_required",
            expected_pid=4321,
            expected_start_ticks=9876,
            expected_expires_at=original_expires_at,
            refreshed_expires_at=refreshed_expires_at,
        )
        return (
            stale_claim,
            after_stale_claim,
            claimed,
            original_expires_at,
            refreshed_expires_at,
            await db.get_onboarding_job(job_id),
        )

    (
        stale_claim,
        after_stale_claim,
        claimed,
        original_expires_at,
        refreshed_expires_at,
        resumed,
    ) = asyncio.run(run())
    assert stale_claim is False
    assert after_stale_claim.state == "failed"
    assert after_stale_claim.expires_at == original_expires_at
    assert claimed is True
    assert resumed.state == "running"
    assert resumed.phase == "browser_start"
    assert resumed.browser_pid is None
    assert resumed.browser_start_ticks is None
    assert resumed.error_code is None
    assert resumed.error_message is None
    assert resumed.completed_at is None
    assert resumed.expires_at == refreshed_expires_at


def test_onboarding_failed_resume_claim_is_atomic_across_database_instances(
    temp_db_path,
):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        first_db = Database(db_path=temp_db_path)
        second_db = Database(db_path=temp_db_path)
        await first_db.init_db()
        original_expires_at = datetime(2026, 7, 19, 8, 10, tzinfo=timezone.utc)
        refreshed_expires_at = original_expires_at + timedelta(hours=1)
        job_id = await first_db.create_onboarding_job(
            OnboardingJob(expires_at=original_expires_at)
        )
        assert await first_db.claim_onboarding_job(job_id) is True
        await first_db.update_onboarding_job_state(
            job_id,
            "failed",
            phase="verify_account",
            error_code="login_required",
            error_message="safe failure",
        )
        claims = await asyncio.gather(
            first_db.claim_failed_onboarding_job_resume(
                job_id,
                expected_phase="verify_account",
                expected_error_code="login_required",
                expected_pid=None,
                expected_start_ticks=None,
                expected_expires_at=original_expires_at,
                refreshed_expires_at=refreshed_expires_at,
            ),
            second_db.claim_failed_onboarding_job_resume(
                job_id,
                expected_phase="verify_account",
                expected_error_code="login_required",
                expected_pid=None,
                expected_start_ticks=None,
                expected_expires_at=original_expires_at,
                refreshed_expires_at=refreshed_expires_at,
            ),
        )
        return claims, refreshed_expires_at, await first_db.get_onboarding_job(job_id)

    claims, refreshed_expires_at, resumed = asyncio.run(run())
    assert sorted(claims) == [False, True]
    assert resumed.state == "running"
    assert resumed.phase == "browser_start"
    assert resumed.expires_at == refreshed_expires_at


def test_onboarding_failed_resume_is_blocked_by_another_failed_job(temp_db_path):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        resumable_id = await db.create_onboarding_job(OnboardingJob())
        blocker_id = await db.create_onboarding_job(OnboardingJob())
        assert await db.claim_onboarding_job(resumable_id) is True
        await db.update_onboarding_job_state(
            resumable_id,
            "failed",
            phase="verify_account",
            error_code="login_required",
            error_message="safe failure",
        )
        await db.update_onboarding_job_state(
            blocker_id,
            "failed",
            phase="stop_browser",
            error_code="process_ownership_mismatch",
            error_message="safe failure",
        )

        claimed = await db.claim_failed_onboarding_job_resume(
            resumable_id,
            expected_phase="verify_account",
            expected_error_code="login_required",
            expected_pid=None,
            expected_start_ticks=None,
            expected_expires_at=None,
            refreshed_expires_at=datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc),
        )
        return claimed, await db.get_onboarding_job(resumable_id)

    claimed, preserved = asyncio.run(run())
    assert claimed is False
    assert preserved.state == "failed"
    assert preserved.phase == "verify_account"
    assert preserved.error_code == "login_required"


@pytest.mark.parametrize(
    ("phase", "metadata_update"),
    [
        ("account_commit", {}),
        ("verify_account", {"conflict_status": "no_conflict"}),
    ],
)
def test_onboarding_failed_resume_rejects_migration_or_commit_state(
    temp_db_path,
    phase,
    metadata_update,
):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        job_id = await db.create_onboarding_job(OnboardingJob())
        assert await db.claim_onboarding_job(job_id) is True
        if metadata_update:
            await db.update_onboarding_job(job_id, **metadata_update)
        await db.update_onboarding_job_state(
            job_id,
            "failed",
            phase=phase,
            error_code="login_required",
            error_message="safe failure",
        )
        claimed = await db.claim_failed_onboarding_job_resume(
            job_id,
            expected_phase=phase,
            expected_error_code="login_required",
            expected_pid=None,
            expected_start_ticks=None,
            expected_expires_at=None,
            refreshed_expires_at=datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc),
        )
        return claimed, await db.get_onboarding_job(job_id)

    claimed, preserved = asyncio.run(run())
    assert claimed is False
    assert preserved.state == "failed"
    assert preserved.phase == phase


def test_onboarding_pid_only_failed_metadata_blocks_two_database_instances(
    temp_db_path,
):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        first_db = Database(db_path=temp_db_path)
        second_db = Database(db_path=temp_db_path)
        await first_db.init_db()
        failed_job_id = await first_db.create_onboarding_job(OnboardingJob())
        first_pending = await first_db.create_onboarding_job(OnboardingJob())
        second_pending = await first_db.create_onboarding_job(OnboardingJob())
        assert await first_db.claim_onboarding_job(failed_job_id) is True
        await first_db.update_onboarding_job(
            failed_job_id,
            browser_pid=4321,
            browser_start_ticks=None,
        )
        await first_db.update_onboarding_job_state(
            failed_job_id,
            "failed",
            phase="browser_start",
            error_code="process_identity_unavailable",
            error_message="safe failure",
        )
        return await asyncio.gather(
            first_db.claim_onboarding_job(first_pending),
            second_db.claim_onboarding_job(second_pending),
        )

    assert asyncio.run(run()) == [False, False]


def test_onboarding_browser_identity_clear_is_compare_and_swap(temp_db_path):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        job_id = await db.create_onboarding_job(OnboardingJob())
        assert await db.claim_onboarding_job(job_id) is True
        await db.update_onboarding_job(
            job_id,
            browser_pid=5432,
            browser_start_ticks=888,
        )
        stale_replace = await db.replace_onboarding_browser_identity(
            job_id,
            expected_pid=4321,
            expected_start_ticks=777,
            browser_pid=6543,
            browser_start_ticks=999,
        )
        preserved = await db.get_onboarding_job(job_id)
        current_replace = await db.replace_onboarding_browser_identity(
            job_id,
            expected_pid=5432,
            expected_start_ticks=888,
            browser_pid=6543,
            browser_start_ticks=999,
        )
        replaced = await db.get_onboarding_job(job_id)
        stale_clear = await db.clear_onboarding_browser_identity(
            job_id,
            expected_pid=5432,
            expected_start_ticks=888,
        )
        current_clear = await db.clear_onboarding_browser_identity(
            job_id,
            expected_pid=6543,
            expected_start_ticks=999,
        )
        cleared = await db.get_onboarding_job(job_id)
        return (
            stale_replace,
            preserved,
            current_replace,
            replaced,
            stale_clear,
            current_clear,
            cleared,
        )

    (
        stale_replace,
        preserved,
        current_replace,
        replaced,
        stale_clear,
        current_clear,
        cleared,
    ) = asyncio.run(run())
    assert stale_replace is False
    assert preserved.browser_pid == 5432
    assert preserved.browser_start_ticks == 888
    assert current_replace is True
    assert replaced.browser_pid == 6543
    assert replaced.browser_start_ticks == 999
    assert stale_clear is False
    assert current_clear is True
    assert cleared.browser_pid is None
    assert cleared.browser_start_ticks is None


def test_onboarding_terminal_state_transition_is_compare_and_swap(temp_db_path):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        job_id = await db.create_onboarding_job(OnboardingJob())
        assert await db.claim_onboarding_job(job_id) is True
        await db.update_onboarding_job_state(
            job_id,
            "running",
            phase="awaiting_login",
            clear_error=True,
        )
        stale = await db.transition_onboarding_job_state(
            job_id,
            expected_state="running",
            expected_phase="browser_start",
            state="cancelled",
            phase="cancelled",
            clear_error=True,
        )
        cancelled = await db.transition_onboarding_job_state(
            job_id,
            expected_state="running",
            expected_phase="awaiting_login",
            state="cancelled",
            phase="cancelled",
            clear_error=True,
        )
        overwritten = await db.transition_onboarding_job_state(
            job_id,
            expected_state="running",
            expected_phase="awaiting_login",
            state="completed",
            phase="completed",
            clear_error=True,
        )
        current = await db.get_onboarding_job(job_id)
        return stale, cancelled, overwritten, current

    stale, cancelled, overwritten, current = asyncio.run(run())
    assert stale is False
    assert cancelled is True
    assert overwritten is False
    assert current.state == "cancelled"
    assert current.phase == "cancelled"


def test_onboarding_start_claim_is_atomic_across_database_instances(temp_db_path):
    from src.core.database import Database
    from src.core.models import OnboardingJob

    async def run():
        first_db = Database(db_path=temp_db_path)
        second_db = Database(db_path=temp_db_path)
        await first_db.init_db()
        first_job_id = await first_db.create_onboarding_job(OnboardingJob())
        second_job_id = await first_db.create_onboarding_job(OnboardingJob())

        claims = await asyncio.gather(
            first_db.claim_onboarding_job(first_job_id),
            second_db.claim_onboarding_job(second_job_id),
        )
        jobs = {
            job.job_id: job
            for job in await first_db.list_onboarding_jobs()
        }
        return first_job_id, second_job_id, claims, jobs

    first_job_id, second_job_id, claims, jobs = asyncio.run(run())
    assert sorted(claims) == [False, True]
    claimed_job_ids = {
        job_id
        for job_id in (first_job_id, second_job_id)
        if jobs[job_id].state == "running"
    }
    assert len(claimed_job_ids) == 1
    claimed_job = jobs[claimed_job_ids.pop()]
    assert claimed_job.phase == "browser_start"
    pending_jobs = [job for job in jobs.values() if job.state == "pending"]
    assert len(pending_jobs) == 1


def test_begin_immediate_serializes_database_instances_and_rolls_back(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        first_db = Database(db_path=temp_db_path)
        second_db = Database(db_path=temp_db_path)
        await first_db.init_db()
        token_id = await first_db.add_token(
            Token(st="transaction-token-" + "g" * 100, email="transaction@example.com")
        )

        first_acquired = asyncio.Event()
        release_first = asyncio.Event()
        second_attempting = asyncio.Event()
        second_acquired = asyncio.Event()

        async def first_writer():
            async with first_db.transaction() as connection:
                await connection.execute(
                    "UPDATE tokens SET remark = ? WHERE id = ?", ("first", token_id)
                )
                first_acquired.set()
                await release_first.wait()

        async def second_writer():
            await first_acquired.wait()
            second_attempting.set()
            async with second_db.transaction() as connection:
                second_acquired.set()
                await connection.execute(
                    "UPDATE tokens SET remark = ? WHERE id = ?", ("second", token_id)
                )

        first_task = asyncio.create_task(first_writer())
        second_task = asyncio.create_task(second_writer())
        await first_acquired.wait()
        await second_attempting.wait()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_acquired.wait(), timeout=0.05)
        release_first.set()
        await asyncio.gather(first_task, second_task)

        try:
            async with first_db.transaction() as connection:
                await connection.execute(
                    "UPDATE tokens SET remark = ? WHERE id = ?", ("rolled-back", token_id)
                )
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass

        token = await first_db.get_token(token_id)
        return token.remark

    assert asyncio.run(run()) == "second"
