"""Task persistence (create/get/update with result_urls JSON round-trip).

Extracted from Database (P3b repositories). Behavior locked by
tests/characterization/test_db_tasks.py.
"""
import json
from typing import Optional

import aiosqlite

from ..models import Task


class TaskRepository:
    """CRUD for the tasks table."""

    def __init__(self, engine):
        self._engine = engine

    async def create_task(self, task: Task) -> int:
        """Create a new task"""
        async with self._engine._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO tasks (task_id, token_id, model, prompt, status, progress, scene_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (task.task_id, task.token_id, task.model, task.prompt,
                  task.status, task.progress, task.scene_id))
            await db.commit()
            return cursor.lastrowid

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID"""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            if row:
                task_dict = dict(row)
                if task_dict.get("result_urls"):
                    task_dict["result_urls"] = json.loads(task_dict["result_urls"])
                return Task(**task_dict)
            return None

    async def update_task(self, task_id: str, **kwargs):
        """Update task"""
        async with self._engine._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    if key == "result_urls" and isinstance(value, list):
                        value = json.dumps(value)
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                params.append(task_id)
                query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"
                await db.execute(query, params)
                await db.commit()
