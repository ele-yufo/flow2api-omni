"""Token-statistics persistence (image/video/error counters with daily reset).

Extracted from Database (P3b repositories). Takes an engine exposing `_connect`.
Behavior locked by tests/characterization/test_db_token_stats.py.
"""
from datetime import date
from typing import Optional

import aiosqlite

from ..models import TokenStats


class TokenStatsRepository:
    """CRUD for the token_stats table."""

    def __init__(self, engine):
        self._engine = engine  # provides _connect()

    async def increment_token_stats(self, token_id: int, stat_type: str):
        """Increment token statistics (delegates to specific methods)"""
        if stat_type == "image":
            await self.increment_image_count(token_id)
        elif stat_type == "video":
            await self.increment_video_count(token_id)
        elif stat_type == "error":
            await self.increment_error_count(token_id)

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return TokenStats(**dict(row))
            return None

    async def increment_image_count(self, token_id: int):
        """Increment image generation count with daily reset"""
        async with self._engine._connect(write=True) as db:
            today = str(date.today())
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = today_image_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_video_count(self, token_id: int):
        """Increment video generation count with daily reset"""
        async with self._engine._connect(write=True) as db:
            today = str(date.today())
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = today_video_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_error_count(self, token_id: int):
        """Increment error count with daily reset.

        error_count: historical total (never reset); consecutive_error_count: reset on
        success/enable; today_error_count: reset on date change.
        """
        async with self._engine._connect(write=True) as db:
            today = str(date.today())
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = today_error_count + 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count only (keep historical error_count/today_error_count)."""
        async with self._engine._connect(write=True) as db:
            await db.execute("""
                UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?
            """, (token_id,))
            await db.commit()
