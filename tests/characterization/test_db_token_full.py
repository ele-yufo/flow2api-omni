"""Characterization: lock the full token repository surface before extracting TokenRepository.

Covers get/by_st/by_email/all/active/dashboard/system_info/clear_ban/delete +
cross-table cascade delete. (add/update/get_all_with_stats already in test_db_token_crud.)
"""
import asyncio

from tests.conftest import assert_golden


def test_token_full_surface_golden(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        t1 = await db.add_token(Token(st="ST_ONE_" + "x" * 50, email="a@e.com", name="A",
                                      credits=100, is_active=True))
        t2 = await db.add_token(Token(st="ST_TWO_" + "y" * 50, email="b@e.com", name="B",
                                      credits=50, is_active=True))

        by_id = await db.get_token(t1)
        by_st = await db.get_token_by_st("ST_ONE_" + "x" * 50)
        by_email = await db.get_token_by_email("b@e.com")
        all_count = len(await db.get_all_tokens())
        active_count_before = len(await db.get_active_tokens())
        dash = await db.get_dashboard_stats()
        sysinfo = await db.get_system_info_stats()

        await db.update_token(t2, is_active=False, ban_reason="429")
        active_count_after = len(await db.get_active_tokens())

        await db.clear_token_ban(t2)
        t2_after_clear = await db.get_token(t2)

        # cross-table cascade delete
        await db.increment_image_count(t1)
        await db.delete_token(t1)
        all_after_delete = len(await db.get_all_tokens())
        t1_stats_after_delete = await db.get_token_stats(t1)

        return {
            "by_id_email": by_id.email,
            "by_st_name": by_st.name,
            "by_email_name": by_email.name,
            "all_count": all_count,
            "active_before": active_count_before,
            "dashboard_total_active": [dash["total_tokens"], dash["active_tokens"]],
            "system_total_credits": sysinfo["total_credits"],
            "active_after_ban": active_count_after,
            "t2_ban_cleared": t2_after_clear.ban_reason,
            "all_after_delete": all_after_delete,
            "t1_stats_gone": t1_stats_after_delete is None,
        }

    out = asyncio.run(run())
    assert out["by_id_email"] == "a@e.com"
    assert out["by_email_name"] == "B"
    assert out["all_count"] == 2
    assert out["dashboard_total_active"] == [2, 2]
    assert out["system_total_credits"] == 150
    assert out["active_after_ban"] == 1  # t2 banned
    assert out["t2_ban_cleared"] is None  # cleared
    assert out["all_after_delete"] == 1
    assert out["t1_stats_gone"] is True   # cascade delete removed stats
    assert_golden("db_token_full", out)
