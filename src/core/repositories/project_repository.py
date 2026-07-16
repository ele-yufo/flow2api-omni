"""Project persistence (add/get-by-id/get-by-token/delete).

Extracted from Database (P3b repositories). Behavior locked by
tests/characterization/test_db_projects.py.
"""
from typing import List, Optional

import aiosqlite

from ..models import Project


class ProjectRepository:
    """CRUD for the projects table."""

    def __init__(self, engine):
        self._engine = engine

    async def add_project(self, project: Project) -> int:
        """Add a new project"""
        async with self._engine._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO projects (project_id, token_id, project_name, tool_name, is_active)
                VALUES (?, ?, ?, ?, ?)
            """, (project.project_id, project.token_id, project.project_name,
                  project.tool_name, project.is_active))
            await db.commit()
            return cursor.lastrowid

    async def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Get project by UUID"""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
            row = await cursor.fetchone()
            if row:
                return Project(**dict(row))
            return None

    async def get_projects_by_token(self, token_id: int) -> List[Project]:
        """Get all projects for a token"""
        async with self._engine._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE token_id = ? ORDER BY created_at DESC",
                (token_id,)
            )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def delete_project(self, project_id: str):
        """Delete project"""
        async with self._engine._connect(write=True) as db:
            await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            await db.commit()
