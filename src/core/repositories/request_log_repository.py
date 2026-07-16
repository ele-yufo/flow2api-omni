"""Request-log persistence (add/update/list/detail/clear).

Extracted from Database (P3b repositories). Takes an engine exposing `_connect` and
`_column_exists`. list/detail LEFT JOIN tokens for email/name. Behavior locked by
tests/characterization/test_db_request_logs.py.
"""
from typing import Any, Dict, Optional

import aiosqlite

from ..models import RequestLog


class RequestLogRepository:
    """CRUD for the request_logs table."""

    def __init__(self, engine):
        self._engine = engine  # provides _connect() and _column_exists()

    async def add_request_log(self, log: RequestLog) -> int:
        """Add request log and return log id"""
        async with self._engine._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO request_logs (token_id, operation, request_body, response_body, status_code, duration, status_text, progress)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                log.token_id,
                log.operation,
                log.request_body,
                log.response_body,
                log.status_code,
                log.duration,
                log.status_text or "",
                log.progress,
            ))
            await db.commit()
            return cursor.lastrowid

    async def update_request_log(self, log_id: int, **kwargs):
        """Update an existing request log row."""
        if not kwargs:
            return

        allowed_fields = {
            "token_id",
            "operation",
            "request_body",
            "response_body",
            "status_code",
            "duration",
            "status_text",
            "progress",
        }
        update_fields = {key: value for key, value in kwargs.items() if key in allowed_fields}
        if not update_fields:
            return

        clauses = []
        values = []
        for key, value in update_fields.items():
            clauses.append(f"{key} = ?")
            values.append(value)
        clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.append(log_id)

        async with self._engine._connect(write=True) as db:
            await db.execute(
                f"UPDATE request_logs SET {', '.join(clauses)} WHERE id = ?",
                values,
            )
            await db.commit()

    async def get_logs(self, limit: int = 100, token_id: Optional[int] = None, include_payload: bool = False):
        """Get request logs with token info, optionally including payload fields"""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            payload_columns = "rl.request_body, rl.response_body," if include_payload else ""
            response_excerpt_column = "substr(COALESCE(rl.response_body, ''), 1, 2048) as response_body_excerpt,"
            has_status_text = await self._engine._column_exists(db, "request_logs", "status_text")
            has_progress = await self._engine._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._engine._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"

            if token_id:
                cursor = await db.execute(f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    WHERE rl.token_id = ?
                    ORDER BY rl.created_at DESC
                    LIMIT ?
                """, (token_id, limit))
            else:
                cursor = await db.execute(f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    ORDER BY rl.created_at DESC
                    LIMIT ?
                """, (limit,))

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_log_detail(self, log_id: int) -> Optional[Dict[str, Any]]:
        """Get single request log detail including payload fields"""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            has_status_text = await self._engine._column_exists(db, "request_logs", "status_text")
            has_progress = await self._engine._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._engine._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"
            cursor = await db.execute(f"""
                SELECT
                    rl.id,
                    rl.token_id,
                    rl.operation,
                    rl.request_body,
                    rl.response_body,
                    rl.status_code,
                    rl.duration,
                    {status_text_column}
                    {progress_column}
                    rl.created_at,
                    {updated_at_column}
                    t.email as token_email,
                    t.name as token_username
                FROM request_logs rl
                LEFT JOIN tokens t ON rl.token_id = t.id
                WHERE rl.id = ?
                LIMIT 1
            """, (log_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_all_logs(self):
        """Clear all request logs"""
        async with self._engine._connect(write=True) as db:
            await db.execute("DELETE FROM request_logs")
            await db.commit()
