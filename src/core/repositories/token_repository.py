"""Token persistence (CRUD + active/dashboard/system aggregates + cascade delete).

Extracted from Database (P3b repositories). add_token also seeds a token_stats row;
delete_token cascades across request_logs/tasks/token_stats/projects (behavior preserved).
Locked by test_db_token_crud + test_db_token_full.
"""
from typing import Any, Dict, List, Optional

import aiosqlite

from ..models import Token


_BLOCKING_ONBOARDING_STATES = ("pending", "running", "failed")


class TokenDeletionBlockedError(RuntimeError):
    """Raised when a resumable onboarding job still owns a token reference."""

    def __init__(self, *, token_id: int, job_id: str, job_state: str):
        self.token_id = token_id
        self.job_id = job_id
        self.job_state = job_state
        super().__init__(
            f"Token {token_id} cannot be deleted while onboarding job "
            f"{job_id} is {job_state}."
        )


class TokenRepository:
    """CRUD + read aggregates for the tokens table."""

    def __init__(self, engine, lifecycle_repository):
        self._engine = engine
        self._lifecycle = lifecycle_repository

    async def add_token(self, token: Token) -> int:
            """Add a token, stats, and its lifecycle row atomically."""
            async with self._engine.transaction() as db:
                cursor = await db.execute("""
                    INSERT INTO tokens (st, at, at_expires, email, name, remark, is_active,
                                       credits, user_paygate_tier, current_project_id, current_project_name,
                                       image_enabled, video_enabled, image_concurrency, video_concurrency,
                                       captcha_proxy_url, ban_reason, banned_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (token.st, token.at, token.at_expires, token.email, token.name, token.remark,
                      token.is_active, token.credits, token.user_paygate_tier,
                      token.current_project_id, token.current_project_name,
                      token.image_enabled, token.video_enabled,
                      token.image_concurrency, token.video_concurrency, token.captcha_proxy_url,
                      token.ban_reason, token.banned_at))
                token_id = cursor.lastrowid
                await db.execute("INSERT INTO token_stats (token_id) VALUES (?)", (token_id,))
                await self._lifecycle.create_for_token(token_id, db=db)
                return token_id

    async def get_token(self, token_id: int) -> Optional[Token]:
            """Get token by ID"""
            async with self._engine._connect() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
                row = await cursor.fetchone()
                if row:
                    return Token(**dict(row))
                return None

    async def get_token_by_st(self, st: str) -> Optional[Token]:
            """Get token by ST"""
            async with self._engine._connect() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM tokens WHERE st = ?", (st,))
                row = await cursor.fetchone()
                if row:
                    return Token(**dict(row))
                return None

    async def get_token_by_email(self, email: str) -> Optional[Token]:
            """Get token by email"""
            async with self._engine._connect() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM tokens WHERE email = ?", (email,))
                row = await cursor.fetchone()
                if row:
                    return Token(**dict(row))
                return None

    async def get_all_tokens(self) -> List[Token]:
            """Get all tokens"""
            async with self._engine._connect() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM tokens ORDER BY created_at DESC")
                rows = await cursor.fetchall()
                return [Token(**dict(row)) for row in rows]

    async def get_all_tokens_with_stats(self) -> List[Dict[str, Any]]:
            """Get all tokens with merged statistics in one query"""
            async with self._engine._connect() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("""
                    SELECT
                        t.*,
                        COALESCE(ts.image_count, 0) AS image_count,
                        COALESCE(ts.video_count, 0) AS video_count,
                        COALESCE(ts.error_count, 0) AS error_count,
                        l.membership_confirmed_status,
                        l.membership_candidate,
                        l.membership_candidate_count,
                        l.keepalive_enabled,
                        l.runtime_mode,
                        l.profile_state,
                        l.verified_email,
                        l.last_keepalive_success_at,
                        l.last_keepalive_status,
                        l.next_due_at,
                        l.last_failure_at,
                        l.last_failure_code,
                        l.last_observed_tier,
                        l.last_observed_at,
                        l.retired_at,
                        l.restored_at
                    FROM tokens t
                    LEFT JOIN token_stats ts ON ts.token_id = t.id
                    LEFT JOIN token_lifecycle l ON l.token_id = t.id
                    ORDER BY t.created_at DESC
                """)
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_dashboard_stats(self) -> Dict[str, int]:
            """Get dashboard counters with aggregated SQL queries"""
            async with self._engine._connect() as db:
                db.row_factory = aiosqlite.Row

                token_cursor = await db.execute("""
                    SELECT
                        COUNT(*) AS total_tokens,
                        COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens
                    FROM tokens
                """)
                token_row = await token_cursor.fetchone()

                stats_cursor = await db.execute("""
                    SELECT
                        COALESCE(SUM(image_count), 0) AS total_images,
                        COALESCE(SUM(video_count), 0) AS total_videos,
                        COALESCE(SUM(error_count), 0) AS total_errors,
                        COALESCE(SUM(today_image_count), 0) AS today_images,
                        COALESCE(SUM(today_video_count), 0) AS today_videos,
                        COALESCE(SUM(today_error_count), 0) AS today_errors
                    FROM token_stats
                """)
                stats_row = await stats_cursor.fetchone()

                token_data = dict(token_row) if token_row else {}
                stats_data = dict(stats_row) if stats_row else {}

                return {
                    "total_tokens": int(token_data.get("total_tokens") or 0),
                    "active_tokens": int(token_data.get("active_tokens") or 0),
                    "total_images": int(stats_data.get("total_images") or 0),
                    "total_videos": int(stats_data.get("total_videos") or 0),
                    "total_errors": int(stats_data.get("total_errors") or 0),
                    "today_images": int(stats_data.get("today_images") or 0),
                    "today_videos": int(stats_data.get("today_videos") or 0),
                    "today_errors": int(stats_data.get("today_errors") or 0)
                }

    async def get_system_info_stats(self) -> Dict[str, int]:
            """Get lightweight system counters used by admin dashboard"""
            async with self._engine._connect() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("""
                    SELECT
                        COUNT(*) AS total_tokens,
                        COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens,
                        COALESCE(SUM(CASE WHEN is_active = 1 THEN credits ELSE 0 END), 0) AS total_credits
                    FROM tokens
                """)
                row = await cursor.fetchone()
                data = dict(row) if row else {}
                return {
                    "total_tokens": int(data.get("total_tokens") or 0),
                    "active_tokens": int(data.get("active_tokens") or 0),
                    "total_credits": int(data.get("total_credits") or 0)
                }

    async def get_active_tokens(self) -> List[Token]:
            """Get all active tokens"""
            async with self._engine._connect() as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT * FROM tokens WHERE is_active = 1 ORDER BY last_used_at ASC")
                rows = await cursor.fetchall()
                return [Token(**dict(row)) for row in rows]

    async def update_token(self, token_id: int, **kwargs):
            """Update token fields"""
            async with self._engine._connect(write=True) as db:
                updates = []
                params = []

                for key, value in kwargs.items():
                    if value is not None:
                        updates.append(f"{key} = ?")
                        params.append(value)

                if updates:
                    params.append(token_id)
                    query = f"UPDATE tokens SET {', '.join(updates)} WHERE id = ?"
                    await db.execute(query, params)
                    await db.commit()

    async def clear_token_ban(self, token_id: int):
            """显式把 ban_reason / banned_at 置 NULL。

            update_token 会跳过 None 值（无法把列清空），故清除禁用原因需走此专用方法。
            """
            async with self._engine._connect(write=True) as db:
                await db.execute(
                    "UPDATE tokens SET ban_reason = NULL, banned_at = NULL WHERE id = ?",
                    (token_id,),
                )
                await db.commit()

    async def delete_token(self, token_id: int):
            """Delete a token unless a resumable onboarding job still references it."""
            async with self._engine.transaction() as db:
                blocking_cursor = await db.execute(
                    """
                    SELECT job_id, state
                    FROM onboarding_jobs
                    WHERE (target_token_id = ? OR resolved_token_id = ?)
                      AND state IN (?, ?, ?)
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (token_id, token_id, *_BLOCKING_ONBOARDING_STATES),
                )
                blocking_job = await blocking_cursor.fetchone()
                if blocking_job is not None:
                    raise TokenDeletionBlockedError(
                        token_id=token_id,
                        job_id=blocking_job[0],
                        job_state=blocking_job[1],
                    )

                await db.execute("UPDATE request_logs SET token_id = NULL WHERE token_id = ?", (token_id,))
                await db.execute(
                    """
                    UPDATE onboarding_jobs
                    SET target_token_id = CASE WHEN target_token_id = ? THEN NULL ELSE target_token_id END,
                        resolved_token_id = CASE WHEN resolved_token_id = ? THEN NULL ELSE resolved_token_id END
                    WHERE target_token_id = ? OR resolved_token_id = ?
                    """,
                    (token_id, token_id, token_id, token_id),
                )
                await db.execute("DELETE FROM tasks WHERE token_id = ?", (token_id,))
                await db.execute("DELETE FROM token_stats WHERE token_id = ?", (token_id,))
                await db.execute("DELETE FROM projects WHERE token_id = ?", (token_id,))
                await self._lifecycle.delete_for_token(token_id, db=db)
                await db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
