"""Characterization: lock request-log CRUD (add/get/update/detail/clear) on a temp DB."""
import asyncio

from tests.conftest import assert_golden

_VOL = {"created_at", "updated_at", "id", "token_id"}


def _scrub(d):
    if not isinstance(d, dict):
        return d
    return {k: v for k, v in sorted(d.items()) if k not in _VOL}


def test_request_log_crud_golden(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token, RequestLog

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        tid = await db.add_token(Token(st="S" * 60, email="log@example.com", name="logtok"))

        log_id = await db.add_request_log(RequestLog(
            token_id=tid, operation="t2v", request_body="req", response_body="resp",
            status_code=200, duration=1.5, status_text="ok", progress=100,
        ))
        after_add = await db.get_logs(limit=10)
        await db.update_request_log(log_id, status_text="updated", progress=50)
        detail = await db.get_log_detail(log_id)
        after_update = await db.get_logs(limit=10, token_id=tid, include_payload=True)
        await db.clear_all_logs()
        after_clear = await db.get_logs(limit=10)

        return {
            "returned_id_is_int": isinstance(log_id, int),
            "after_add": [_scrub(r) for r in after_add],
            "detail": _scrub(detail),
            "after_update_with_payload": [_scrub(r) for r in after_update],
            "after_clear_count": len(after_clear),
        }

    out = asyncio.run(run())
    assert out["after_add"][0]["operation"] == "t2v"
    assert out["after_add"][0]["token_email"] == "log@example.com"  # JOIN works
    assert out["detail"]["status_text"] == "updated"
    assert out["after_update_with_payload"][0]["request_body"] == "req"  # payload included
    assert out["after_clear_count"] == 0
    assert_golden("db_request_logs", out)
