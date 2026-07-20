"""Credential-free, resumable onboarding job persistence."""

from datetime import datetime
from typing import List, Optional, Union
from uuid import uuid4

import aiosqlite

from ..models import OnboardingJob


_UNSET = object()
_RESUMABLE_FAILED_PHASES = {"stop_browser", "verify_account"}
_UPDATABLE_FIELDS = {
    "target_token_id",
    "resolved_token_id",
    "phase",
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
}


class OnboardingJobRepository:
    """CRUD and resumable state updates for safe onboarding metadata."""

    def __init__(self, engine):
        self._engine = engine

    @staticmethod
    def _lookup(identifier: Union[int, str]):
        if isinstance(identifier, int):
            return "id = ?", identifier
        return "job_id = ?", str(identifier)

    async def create(self, job: OnboardingJob) -> str:
        """Create an onboarding job and return its stable public job ID."""
        if not isinstance(job, OnboardingJob):
            raise TypeError("job must be an OnboardingJob")
        job_id = job.job_id or uuid4().hex
        async with self._engine.transaction() as db:
            await db.execute(
                """
                INSERT INTO onboarding_jobs (
                    job_id, target_token_id, resolved_token_id, phase, state,
                    browser_pid, browser_start_ticks, discovered_email, discovered_tier,
                    discovered_credits, discovered_at_expires, project_count,
                    profile_ready, conflict_status, conflict_policy,
                    requested_business_enabled, requested_keepalive_enabled,
                    requested_runtime_mode, error_code, error_message, expires_at,
                    started_at, completed_at, cancelled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job.target_token_id,
                    job.resolved_token_id,
                    job.phase,
                    job.state,
                    job.browser_pid,
                    job.browser_start_ticks,
                    job.discovered_email,
                    job.discovered_tier,
                    job.discovered_credits,
                    job.discovered_at_expires,
                    job.project_count,
                    job.profile_ready,
                    job.conflict_status,
                    job.conflict_policy,
                    job.requested_business_enabled,
                    job.requested_keepalive_enabled,
                    job.requested_runtime_mode,
                    job.error_code,
                    job.error_message,
                    job.expires_at,
                    job.started_at,
                    job.completed_at,
                    job.cancelled_at,
                ),
            )
        return job_id

    async def get(self, identifier: Union[int, str]) -> Optional[OnboardingJob]:
        """Get a job by integer row ID or public job ID."""
        clause, value = self._lookup(identifier)
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM onboarding_jobs WHERE {clause}", (value,)
            )
            row = await cursor.fetchone()
            return OnboardingJob(**dict(row)) if row else None

    async def list(
        self,
        *,
        target_token_id: Optional[int] = None,
        resolved_token_id: Optional[int] = None,
        state: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> List[OnboardingJob]:
        """List jobs newest-first with optional approved-field filters."""
        filters = {
            "target_token_id": target_token_id,
            "resolved_token_id": resolved_token_id,
            "state": state,
            "phase": phase,
        }
        clauses = []
        params = []
        for column, value in filters.items():
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT * FROM onboarding_jobs
                {where}
                ORDER BY created_at DESC, id DESC
                """,
                params,
            )
            return [OnboardingJob(**dict(row)) for row in await cursor.fetchall()]

    async def claim_start(self, job_id: str) -> bool:
        """Claim only when no running or unresolved failed onboarding job exists."""
        public_job_id = str(job_id or "").strip()
        if not public_job_id:
            raise ValueError("job_id must not be empty")
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                """
                UPDATE onboarding_jobs
                SET state = 'running', phase = 'browser_start',
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    completed_at = NULL, cancelled_at = NULL,
                    error_code = NULL, error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                  AND state = 'pending'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM onboarding_jobs AS active
                      WHERE active.state IN ('running', 'failed')
                        AND active.job_id <> ?
                  )
                """,
                (public_job_id, public_job_id),
            )
            return cursor.rowcount == 1

    async def claim_failed_resume(
        self,
        job_id: str,
        *,
        expected_phase: str,
        expected_error_code: Optional[str],
        expected_pid: Optional[int],
        expected_start_ticks: Optional[int],
        expected_expires_at: Optional[datetime],
        refreshed_expires_at: datetime,
    ) -> bool:
        """Atomically reclaim one unchanged failed job and refresh its TTL."""
        public_job_id = str(job_id or "").strip()
        if not public_job_id:
            raise ValueError("job_id must not be empty")
        if expected_phase not in _RESUMABLE_FAILED_PHASES:
            return False
        if expected_expires_at is not None and not isinstance(
            expected_expires_at, datetime
        ):
            raise TypeError("expected_expires_at must be a datetime or null")
        if not isinstance(refreshed_expires_at, datetime):
            raise TypeError("refreshed_expires_at must be a datetime")
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                """
                UPDATE onboarding_jobs
                SET state = 'running', phase = 'browser_start',
                    browser_pid = NULL, browser_start_ticks = NULL,
                    expires_at = ?,
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    completed_at = NULL, cancelled_at = NULL,
                    error_code = NULL, error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                  AND state = 'failed'
                  AND phase = ?
                  AND error_code IS ?
                  AND browser_pid IS ?
                  AND browser_start_ticks IS ?
                  AND expires_at IS ?
                  AND resolved_token_id IS NULL
                  AND discovered_email IS NULL
                  AND discovered_tier IS NULL
                  AND discovered_credits IS NULL
                  AND discovered_at_expires IS NULL
                  AND project_count IS NULL
                  AND profile_ready IS NULL
                  AND conflict_status IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM onboarding_jobs AS active
                      WHERE active.state IN ('running', 'failed')
                        AND active.job_id <> ?
                  )
                """,
                (
                    refreshed_expires_at,
                    public_job_id,
                    expected_phase,
                    expected_error_code,
                    expected_pid,
                    expected_start_ticks,
                    expected_expires_at,
                    public_job_id,
                ),
            )
            return cursor.rowcount == 1

    async def replace_browser_identity(
        self,
        identifier: Union[int, str],
        *,
        expected_pid: Optional[int],
        expected_start_ticks: Optional[int],
        browser_pid: Optional[int],
        browser_start_ticks: Optional[int],
    ) -> bool:
        """Replace the complete browser tuple only while the expected tuple matches."""
        if browser_pid is None and browser_start_ticks is not None:
            raise ValueError("browser start ticks require a browser PID")
        clause, identifier_value = self._lookup(identifier)
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                f"""
                UPDATE onboarding_jobs
                SET browser_pid = ?, browser_start_ticks = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE {clause}
                  AND state IN ('running', 'failed')
                  AND browser_pid IS ?
                  AND browser_start_ticks IS ?
                """,
                (
                    browser_pid,
                    browser_start_ticks,
                    identifier_value,
                    expected_pid,
                    expected_start_ticks,
                ),
            )
            return cursor.rowcount == 1

    async def clear_browser_identity(
        self,
        identifier: Union[int, str],
        *,
        expected_pid: Optional[int],
        expected_start_ticks: Optional[int],
    ) -> bool:
        """Clear browser identity only if both recorded generation fields still match."""
        return await self.replace_browser_identity(
            identifier,
            expected_pid=expected_pid,
            expected_start_ticks=expected_start_ticks,
            browser_pid=None,
            browser_start_ticks=None,
        )

    async def update(self, identifier: Union[int, str], **fields) -> None:
        """Update only explicitly approved resumable metadata fields."""
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported onboarding fields: {sorted(unknown)}")
        if not fields:
            return
        required_fields = {
            "phase",
            "conflict_policy",
            "requested_business_enabled",
            "requested_keepalive_enabled",
            "requested_runtime_mode",
        }
        null_required = {field for field in required_fields if field in fields and fields[field] is None}
        if null_required:
            raise ValueError(f"onboarding fields cannot be null: {sorted(null_required)}")
        if (
            "requested_runtime_mode" in fields
            and fields["requested_runtime_mode"] not in ("persistent", "warm")
        ):
            raise ValueError("requested_runtime_mode must be 'persistent' or 'warm'")
        project_count = fields.get("project_count")
        if project_count is not None and (
            isinstance(project_count, bool)
            or not isinstance(project_count, int)
            or project_count < 0
        ):
            raise ValueError("project_count must be a non-negative integer or null")
        profile_ready = fields.get("profile_ready")
        if profile_ready is not None and not isinstance(profile_ready, bool):
            raise ValueError("profile_ready must be a bool or null")
        conflict_status = fields.get("conflict_status")
        if conflict_status not in (None, "no_conflict", "rejected", "archived_and_replaced"):
            raise ValueError("conflict_status is invalid")
        clause, identifier_value = self._lookup(identifier)
        assignments = [f"{field} = ?" for field in fields]
        params = list(fields.values())
        params.append(identifier_value)
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                f"""
                UPDATE onboarding_jobs
                SET {', '.join(assignments)}, updated_at = CURRENT_TIMESTAMP
                WHERE {clause}
                """,
                params,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"onboarding job not found: {identifier}")

    async def update_state(
        self,
        identifier: Union[int, str],
        state: str,
        *,
        phase: Optional[str] = None,
        error_code=_UNSET,
        error_message=_UNSET,
        clear_error: bool = False,
        expected_state=_UNSET,
        expected_phase=_UNSET,
    ) -> bool:
        """Update workflow state, phase, terminal timestamps, and safe errors."""
        normalized_state = str(state).strip()
        if not normalized_state:
            raise ValueError("state must not be empty")
        if phase is not None and not str(phase).strip():
            raise ValueError("phase must not be empty")
        clause, value = self._lookup(identifier)
        assignments = [
            "state = ?",
            "phase = COALESCE(?, phase)",
            "started_at = CASE "
            "WHEN ? = 'running' THEN COALESCE(started_at, CURRENT_TIMESTAMP) "
            "ELSE started_at END",
            "completed_at = CASE "
            "WHEN ? IN ('completed', 'failed') THEN CURRENT_TIMESTAMP "
            "WHEN ? IN ('pending', 'running') THEN NULL "
            "ELSE completed_at END",
            "cancelled_at = CASE "
            "WHEN ? = 'cancelled' THEN CURRENT_TIMESTAMP "
            "WHEN ? IN ('pending', 'running', 'completed', 'failed') THEN NULL "
            "ELSE cancelled_at END",
        ]
        params = [
            normalized_state,
            phase,
            normalized_state,
            normalized_state,
            normalized_state,
            normalized_state,
            normalized_state,
        ]
        if clear_error:
            assignments.extend(["error_code = NULL", "error_message = NULL"])
        else:
            if error_code is not _UNSET:
                assignments.append("error_code = ?")
                params.append(error_code)
            if error_message is not _UNSET:
                assignments.append("error_message = ?")
                params.append(error_message)
        predicates = [clause]
        params.append(value)
        if expected_state is not _UNSET:
            predicates.append("state IS ?")
            params.append(expected_state)
        if expected_phase is not _UNSET:
            predicates.append("phase IS ?")
            params.append(expected_phase)
        conditional = expected_state is not _UNSET or expected_phase is not _UNSET
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                f"""
                UPDATE onboarding_jobs
                SET {', '.join(assignments)}, updated_at = CURRENT_TIMESTAMP
                WHERE {' AND '.join(predicates)}
                """,
                params,
            )
            if cursor.rowcount != 1:
                if conditional:
                    return False
                raise KeyError(f"onboarding job not found: {identifier}")
            return True

    async def delete(self, identifier: Union[int, str]) -> None:
        """Delete an onboarding job by row or public ID."""
        clause, value = self._lookup(identifier)
        async with self._engine.transaction() as db:
            cursor = await db.execute(
                f"DELETE FROM onboarding_jobs WHERE {clause}", (value,)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"onboarding job not found: {identifier}")
