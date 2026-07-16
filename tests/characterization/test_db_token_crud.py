"""Characterization: lock token add→get→update observable behavior on a temp DB.

Locks the DB layer's observable results so P3 (repository extraction) can be
proven behavior-preserving. Runs entirely on a temp DB (temp_db_path fixture);
the autouse prod-DB guard prevents any accidental production-DB access.
"""
import asyncio

from tests.conftest import assert_golden

# Fields that are secret or vary run-to-run — dropped before golden comparison.
_VOLATILE = {
    "st", "at", "at_expires", "created_at", "updated_at", "last_used_at",
    "banned_at", "id", "token_id",
}


def _scrub(row):
    if not isinstance(row, dict):
        return row
    return {k: v for k, v in sorted(row.items()) if k not in _VOLATILE}


def test_token_crud_golden(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()

        token_id = await db.add_token(
            Token(
                st="ST_SAMPLE_VALUE_LONG_ENOUGH_TO_PASS" * 3,
                email="chartest@example.com",
                name="chartest",
                credits=42,
                user_paygate_tier="PAYGATE_TIER_ONE",
            )
        )
        after_add = await db.get_all_tokens_with_stats()

        await db.update_token(token_id, ban_reason="GRANT_EXPIRED", is_active=False)
        after_update = await db.get_all_tokens_with_stats()

        return {
            "returned_id_is_int": isinstance(token_id, int),
            "after_add": [_scrub(r) for r in after_add],
            "after_update": [_scrub(r) for r in after_update],
        }

    out = asyncio.run(run())
    assert_golden("db_token_crud", out)
