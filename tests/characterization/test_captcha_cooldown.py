"""Characterization: lock reCAPTCHA cooldown streak + exponential backoff."""
from tests.conftest import assert_golden


def test_cooldown_backoff_golden():
    from src.services.captcha.cooldown import CaptchaCooldownTracker

    clock = {"t": 1000.0}
    tr = CaptchaCooldownTracker(clock=lambda: clock["t"])

    delays = [tr.record_rejection("p1") for _ in range(6)]  # 10,20,40,80,120,120
    remaining_immediate = tr.get_cooldown_delay("p1")       # ~120 (until - now)
    clock["t"] += 200                                       # advance past cooldown
    remaining_after = tr.get_cooldown_delay("p1")           # 0
    tr.clear("p1")
    delay_after_clear = tr.record_rejection("p1")           # streak resets -> 10
    global_key = tr.key(None)

    out = {
        "delays": delays,
        "remaining_immediate": remaining_immediate,
        "remaining_after_advance": remaining_after,
        "delay_after_clear": delay_after_clear,
        "global_key": global_key,
    }
    assert delays == [10.0, 20.0, 40.0, 80.0, 120.0, 120.0]
    assert remaining_after == 0.0
    assert delay_after_clear == 10.0
    assert global_key == "_global"
    assert_golden("captcha_cooldown", out)
