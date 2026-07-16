"""Characterization: lock paygate tier pure-function behavior.

Uses the canonical tier strings so ranking/labels/gating are actually exercised,
plus junk inputs to lock the "unknown → free" fallback.
"""
from tests.conftest import assert_golden


def test_account_tiers_golden():
    from src.core import account_tiers as at

    tiers = [
        None,
        "",
        at.PAYGATE_TIER_NOT_PAID,   # canonical free
        at.PAYGATE_TIER_ONE,        # canonical pro
        at.PAYGATE_TIER_TWO,        # canonical ultra
        "TIER_ONE",                 # non-canonical → fallback
        "garbage",                  # junk → fallback
    ]
    models = [
        "gemini-3.1-flash-image-landscape",   # plain → NOT_PAID
        "gemini-3.0-pro-image-square-2k",     # -2k → TIER_ONE
        "veo_3_1_t2v_fast_1080p",             # _1080p → TIER_ONE
        "veo_3_1_t2v_fast_ultra",             # _ultra → TIER_TWO
        "veo_3_1_t2v_fast_4k",                # _4k → TIER_TWO
    ]
    out = {
        "normalize": {str(t): at.normalize_user_paygate_tier(t) for t in tiers},
        "rank": {str(t): at.get_paygate_tier_rank(t) for t in tiers},
        "label": {str(t): at.get_paygate_tier_label(t) for t in tiers},
        "required": {m: at.get_required_paygate_tier_for_model(m) for m in models},
        "supports": {
            f"{m}|{t}": at.supports_model_for_tier(m, t)
            for m in models
            for t in [at.PAYGATE_TIER_NOT_PAID, at.PAYGATE_TIER_ONE, at.PAYGATE_TIER_TWO]
        },
    }
    assert_golden("account_tiers", out)
