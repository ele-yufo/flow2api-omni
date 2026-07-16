"""Characterization: lock task CRUD (create/get/update + result_urls JSON) on a temp DB."""
import asyncio

from tests.conftest import assert_golden

_VOL = {"created_at", "updated_at", "completed_at", "id", "token_id"}


def _scrub(t):
    if t is None:
        return None
    d = t.model_dump() if hasattr(t, "model_dump") else dict(t)
    return {k: v for k, v in sorted(d.items()) if k not in _VOL}


def test_task_crud_golden(temp_db_path):
    from src.core.database import Database
    from src.core.models import Task, Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        tid = await db.add_token(Token(st="S" * 60, email="task@example.com", name="tasktok"))
        await db.create_task(Task(task_id="tk-1", token_id=tid, model="veo", prompt="hi",
                                  status="pending", progress=0, scene_id="sc-1"))
        created = await db.get_task("tk-1")
        await db.update_task("tk-1", status="done", progress=100,
                             result_urls=["http://a", "http://b"])
        updated = await db.get_task("tk-1")
        missing = await db.get_task("nope")
        return {"created": _scrub(created), "updated": _scrub(updated), "missing": _scrub(missing)}

    out = asyncio.run(run())
    assert out["created"]["status"] == "pending"
    assert out["updated"]["status"] == "done"
    assert out["updated"]["result_urls"] == ["http://a", "http://b"]  # JSON round-trip
    assert out["missing"] is None
    assert_golden("db_tasks", out)
