"""Characterization: lock paygate tier pure-function behavior."""
from tests.conftest import assert_golden


def test_account_tiers_golden():
    from src.core import account_tiers as at

    tiers = [None, "", "NOT_PAID", "TIER_ONE", "TIER_TWO", "garbage"]
    models = [
        "gemini-3.1-flash-image-landscape",
        "veo_3_1_t2v_fast_landscape",
        "gemini_omni_t2v_4s",
        "veo_3_1_t2v_fast_ultra",
    ]
    out = {
        "normalize": {str(t): at.normalize_user_paygate_tier(t) for t in tiers},
        "rank": {str(t): at.get_paygate_tier_rank(t) for t in tiers},
        "label": {str(t): at.get_paygate_tier_label(t) for t in tiers},
        "required": {m: at.get_required_paygate_tier_for_model(m) for m in models},
        "supports": {
            f"{m}|{t}": at.supports_model_for_tier(m, t)
            for m in models
            for t in ["NOT_PAID", "TIER_ONE", "TIER_TWO"]
        },
    }
    assert_golden("account_tiers", out)
