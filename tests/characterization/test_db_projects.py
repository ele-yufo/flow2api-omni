"""Characterization: lock project CRUD (add/get_by_id/get_by_token/delete) on a temp DB."""
import asyncio

from tests.conftest import assert_golden

_VOL = {"created_at", "id"}


def _scrub(p):
    if p is None:
        return None
    d = p.model_dump() if hasattr(p, "model_dump") else dict(p)
    return {k: v for k, v in sorted(d.items()) if k not in _VOL}


def test_project_crud_golden(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token, Project

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        tid = await db.add_token(Token(st="S" * 60, email="proj@example.com", name="ptok"))
        await db.add_project(Project(project_id="uuid-1", token_id=tid, project_name="P1"))
        await db.add_project(Project(project_id="uuid-2", token_id=tid, project_name="P2"))
        by_id = await db.get_project_by_id("uuid-1")
        by_token = await db.get_projects_by_token(tid)
        await db.delete_project("uuid-1")
        after_delete = await db.get_project_by_id("uuid-1")
        return {
            "by_id": _scrub(by_id),
            "by_token_count": len(by_token),
            "by_token_names": sorted(p.project_name for p in by_token),
            "after_delete": _scrub(after_delete),
        }

    out = asyncio.run(run())
    assert out["by_id"]["project_name"] == "P1"
    assert out["by_token_count"] == 2
    assert out["after_delete"] is None
    assert_golden("db_projects", out)
