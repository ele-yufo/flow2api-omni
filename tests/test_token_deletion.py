"""Token deletion guards for resumable onboarding references.

All persistence tests use a temporary SQLite database. No browser, network, or
production database is accessed.
"""

from __future__ import annotations

import asyncio

import pytest


BLOCKING_STATES = ("pending", "running", "failed")
TERMINAL_STATES = ("completed", "cancelled")
REFERENCE_COLUMNS = ("target_token_id", "resolved_token_id")


@pytest.mark.parametrize("reference_column", REFERENCE_COLUMNS)
@pytest.mark.parametrize("job_state", BLOCKING_STATES)
def test_repository_atomically_rejects_delete_for_blocking_onboarding_reference(
    temp_db_path,
    reference_column,
    job_state,
):
    from src.core.database import Database
    from src.core.models import OnboardingJob, Token
    from src.core.repositories.token_repository import TokenDeletionBlockedError

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(
                st=f"delete-blocked-{reference_column}-{job_state}-" + "x" * 100,
                email=f"{reference_column}-{job_state}@example.com",
            )
        )
        job_id = await db.create_onboarding_job(
            OnboardingJob(
                state=job_state,
                **{reference_column: token_id},
            )
        )

        with pytest.raises(TokenDeletionBlockedError) as blocked:
            await db.delete_token(token_id)

        return (
            token_id,
            job_id,
            blocked.value,
            await db.get_token(token_id),
            await db.get_token_lifecycle(token_id),
            await db.get_token_stats(token_id),
            await db.get_onboarding_job(job_id),
        )

    token_id, job_id, blocked, token, lifecycle, stats, job = asyncio.run(run())
    assert blocked.token_id == token_id
    assert blocked.job_id == job_id
    assert blocked.job_state == job_state
    assert token is not None
    assert lifecycle is not None
    assert stats is not None
    assert getattr(job, reference_column) == token_id


@pytest.mark.parametrize("reference_column", REFERENCE_COLUMNS)
@pytest.mark.parametrize("job_state", TERMINAL_STATES)
def test_repository_detaches_terminal_onboarding_reference_and_deletes_token(
    temp_db_path,
    reference_column,
    job_state,
):
    from src.core.database import Database
    from src.core.models import OnboardingJob, Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        token_id = await db.add_token(
            Token(
                st=f"delete-terminal-{reference_column}-{job_state}-" + "y" * 100,
                email=f"terminal-{reference_column}-{job_state}@example.com",
            )
        )
        job_id = await db.create_onboarding_job(
            OnboardingJob(
                state=job_state,
                **{reference_column: token_id},
            )
        )

        await db.delete_token(token_id)
        detached = await db.get_onboarding_job(job_id)
        await db.delete_onboarding_job(job_id)

        return (
            await db.get_token(token_id),
            await db.get_token_lifecycle(token_id),
            await db.get_token_stats(token_id),
            detached,
            await db.get_onboarding_job(job_id),
        )

    token, lifecycle, stats, detached, deleted_job = asyncio.run(run())
    assert token is None
    assert lifecycle is None
    assert stats is None
    assert detached is not None
    assert getattr(detached, reference_column) is None
    assert deleted_job is None


def test_delete_waits_for_concurrent_onboarding_write_then_rejects(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token
    from src.core.repositories.token_repository import TokenDeletionBlockedError

    async def run():
        writer_db = Database(db_path=temp_db_path)
        deleter_db = Database(db_path=temp_db_path)
        await writer_db.init_db()
        token_id = await writer_db.add_token(
            Token(
                st="delete-concurrent-" + "z" * 100,
                email="delete-concurrent@example.com",
            )
        )

        onboarding_inserted = asyncio.Event()
        release_writer = asyncio.Event()
        delete_finished = asyncio.Event()

        async def create_blocking_job():
            async with writer_db.transaction() as connection:
                await connection.execute(
                    """
                    INSERT INTO onboarding_jobs (job_id, target_token_id, state)
                    VALUES (?, ?, 'pending')
                    """,
                    ("concurrent-blocker", token_id),
                )
                onboarding_inserted.set()
                await release_writer.wait()

        async def delete_token():
            await onboarding_inserted.wait()
            try:
                await deleter_db.delete_token(token_id)
            finally:
                delete_finished.set()

        writer_task = asyncio.create_task(create_blocking_job())
        deleter_task = asyncio.create_task(delete_token())
        await onboarding_inserted.wait()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(delete_finished.wait(), timeout=0.05)
        release_writer.set()
        await writer_task
        with pytest.raises(TokenDeletionBlockedError) as blocked:
            await deleter_task
        return token_id, blocked.value, await writer_db.get_token(token_id)

    token_id, blocked, token = asyncio.run(run())
    assert blocked.token_id == token_id
    assert blocked.job_id == "concurrent-blocker"
    assert blocked.job_state == "pending"
    assert token is not None


def test_token_manager_translates_repository_delete_conflict():
    from src.core.models import Token
    from src.core.repositories.token_repository import TokenDeletionBlockedError
    from src.services.token_manager import TokenDeletionConflictError, TokenManager

    token = Token(
        id=23,
        st="service-delete-blocked-" + "s" * 100,
        email="service-delete-blocked@example.com",
    )

    class BlockingDatabase:
        async def get_token(self, token_id):
            return token if token_id == token.id else None

        async def get_projects_by_token(self, token_id):
            assert token_id == token.id
            return []

        async def delete_token(self, token_id):
            raise TokenDeletionBlockedError(
                token_id=token_id,
                job_id="blocked-service-job",
                job_state="failed",
            )

    async def run():
        manager = TokenManager(db=BlockingDatabase(), flow_client=None)
        with pytest.raises(TokenDeletionConflictError) as conflict:
            await manager.delete_token(token.id)
        return conflict.value

    conflict = asyncio.run(run())
    assert conflict.code == "onboarding_job_blocks_token_deletion"
    assert conflict.token_id == token.id
    assert conflict.job_id == "blocked-service-job"
    assert conflict.job_state == "failed"
    assert isinstance(conflict.__cause__, TokenDeletionBlockedError)


def test_admin_delete_returns_conflict_for_nonterminal_onboarding_job():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api import admin
    from src.services.token_manager import TokenDeletionConflictError

    class BlockingTokenManager:
        async def delete_token(self, token_id):
            raise TokenDeletionConflictError(
                token_id=token_id,
                job_id="blocked-admin-job",
                job_state="running",
            )

    admin_token = "delete-conflict-admin-token"
    app = FastAPI()
    app.include_router(admin.router)
    admin.set_dependencies(BlockingTokenManager(), None, None, None, None)
    admin.active_admin_tokens.add(admin_token)
    try:
        with TestClient(app) as client:
            response = client.delete(
                "/api/tokens/23",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
    finally:
        admin.active_admin_tokens.discard(admin_token)
        admin.set_dependencies(None, None, None, None, None)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "onboarding_job_blocks_token_deletion",
        "message": "Token deletion is blocked by an active onboarding job.",
        "job_id": "blocked-admin-job",
        "job_state": "running",
    }
