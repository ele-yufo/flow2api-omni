"""Generic async SQLite engine — connection, write-serialization, schema probes.

App-agnostic plumbing extracted from flow2api's Database. Holds NO business schema;
subclasses (e.g. Database) add tables/CRUD. Safe to reuse across apps.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


class SqliteEngine:
    """Async SQLite connection manager with serialized writes + tuned pragmas."""

    def __init__(self, db_path: str, *, connect_timeout: int = 30, busy_timeout_ms: int = 30000):
        self.db_path = db_path
        self._write_lock = asyncio.Lock()
        self._connect_timeout = connect_timeout
        self._busy_timeout_ms = busy_timeout_ms

    def db_exists(self) -> bool:
        """Check if database file exists"""
        return Path(self.db_path).exists()

    async def _configure_connection(self, db):
        """Apply SQLite runtime settings for better concurrent behavior."""
        await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        await db.execute("PRAGMA foreign_keys = ON")

    @asynccontextmanager
    async def _connect(self, *, write: bool = False):
        """Open a configured SQLite connection and optionally serialize writes."""
        if write:
            async with self._write_lock:
                async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
                    await self._configure_connection(db)
                    yield db
            return

        async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
            await self._configure_connection(db)
            yield db

    @asynccontextmanager
    async def transaction(self):
        """Run a write transaction that acquires SQLite's lock immediately.

        The in-process lock serializes writers sharing this engine instance. ``BEGIN
        IMMEDIATE`` additionally serializes separate ``SqliteEngine`` instances and
        processes through SQLite's own busy timeout.
        """
        async with self._connect(write=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            else:
                await db.commit()

    @asynccontextmanager
    async def _transaction(self):
        """Compatibility alias for repository code using private engine helpers."""
        async with self.transaction() as db:
            yield db

    async def _table_exists(self, db, table_name: str) -> bool:
        """Check if a table exists in the database"""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        result = await cursor.fetchone()
        return result is not None

    async def _column_exists(self, db, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table"""
        try:
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = await cursor.fetchall()
            return any(col[1] == column_name for col in columns)
        except Exception:
            return False
