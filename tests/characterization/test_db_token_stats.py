"""Characterization: lock token-stats counters (image/video/error/reset) on a temp DB.

Protects the stats behavior before extracting a TokenStatsRepository.
"""
import asyncio

from tests.conftest import assert_golden


def test_token_stats_golden(temp_db_path):
    from src.core.database import Database
    from src.core.models import Token

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        tid = await db.add_token(Token(st="S" * 60, email="stats@example.com", name="stats"))

        await db.increment_image_count(tid)
        await db.increment_image_count(tid)
        await db.increment_video_count(tid)
        await db.increment_error_count(tid)
        await db.increment_error_count(tid)
        s_before_reset = await db.get_token_stats(tid)
        await db.reset_error_count(tid)
        s_after_reset = await db.get_token_stats(tid)
        # dispatcher path
        await db.increment_token_stats(tid, "image")

        s_final = await db.get_token_stats(tid)

        def scrub(s):
            d = s.model_dump() if hasattr(s, "model_dump") else dict(s)
            # today_date / last_error_at are time-dependent -> drop
            for k in ("today_date", "last_error_at"):
                d.pop(k, None)
            return d

        return {
            "before_reset": scrub(s_before_reset),
            "after_reset": scrub(s_after_reset),
            "final": scrub(s_final),
        }

    out = asyncio.run(run())
    # sanity: image=2, video=1, error=2 before reset; consecutive reset to 0; image=3 final
    assert out["before_reset"]["image_count"] == 2
    assert out["before_reset"]["video_count"] == 1
    assert out["before_reset"]["error_count"] == 2
    assert out["after_reset"]["consecutive_error_count"] == 0
    assert out["after_reset"]["error_count"] == 2  # historical kept
    assert out["final"]["image_count"] == 3
    assert_golden("db_token_stats", out)
