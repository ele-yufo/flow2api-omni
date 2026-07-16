"""Characterization: lock tier-based video model-key resolution + result-state helpers."""
from tests.conftest import assert_golden


def test_resolve_video_model_key_for_tier_golden(subtests):
    from src.services.generation.state import resolve_video_model_key_for_tier

    # (model_key, allow_tier_upgrade) × tier — covers upgrade/downgrade/no-op branches
    configs = [
        ("veo_3_1_t2v_fast", True),
        ("veo_3_1_t2v_fast", False),
        ("veo_3_1_i2v_s_fast_fl", True),
        ("veo_3_1_t2v_fast_ultra", True),
        ("veo_3_1_i2v_s_fast_ultra_fl", True),
    ]
    tiers = ["PAYGATE_TIER_NOT_PAID", "PAYGATE_TIER_ONE", "PAYGATE_TIER_TWO"]
    out = {}
    for mk, allow in configs:
        for tier in tiers:
            with subtests.test(model_key=mk, allow=allow, tier=tier):
                key, note = resolve_video_model_key_for_tier(
                    {"model_key": mk, "allow_tier_upgrade": allow}, tier
                )
                out[f"{mk}|allow={allow}|{tier}"] = [key, note]
    assert_golden("generation_state_tier", out)


def test_result_state_helpers():
    from src.services.generation.state import (
        create_generation_result,
        mark_generation_failed,
        mark_generation_succeeded,
        normalize_error_message,
    )

    r = create_generation_result()
    assert r == {"success": False, "error_message": None, "error_emitted": False}
    mark_generation_failed(r, "boom")
    assert r == {"success": False, "error_message": "boom", "error_emitted": True}
    mark_generation_succeeded(r)
    assert r == {"success": True, "error_message": None, "error_emitted": False}
    from src.services.generation.state import get_no_token_error_message
    assert "图片生成" in get_no_token_error_message("image")
    assert "视频生成" in get_no_token_error_message("video")
    assert normalize_error_message("") == "未知错误"
    assert normalize_error_message("x" * 2000).endswith("...")
    assert len(normalize_error_message("x" * 2000)) == 1000


def test_delegation_matches():
    """GenerationHandler 薄委托 == 纯函数。"""
    from src.services.generation_handler import GenerationHandler
    from src.services.generation import state as S

    gh = GenerationHandler.__new__(GenerationHandler)
    cfg = {"model_key": "veo_3_1_t2v_fast", "allow_tier_upgrade": True}
    assert gh._resolve_video_model_key_for_tier(cfg, "PAYGATE_TIER_TWO") == \
        S.resolve_video_model_key_for_tier(cfg, "PAYGATE_TIER_TWO")
    assert gh._normalize_error_message("z" * 3000) == S.normalize_error_message("z" * 3000)
