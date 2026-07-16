"""Characterization: truncate_prompt_for_log + poll_progress_percent.

锁住从 generation_handler 抽出的两个纯函数(subagent 扫出的重复 idiom)。
"""
from src.services.generation.state import truncate_prompt_for_log
from src.services.generation.response_parsing import poll_progress_percent


def test_truncate_short_prompt_unchanged():
    assert truncate_prompt_for_log("hello") == "hello"
    assert truncate_prompt_for_log("x" * 2000) == "x" * 2000  # 边界:恰好 2000 不截


def test_truncate_long_prompt():
    out = truncate_prompt_for_log("y" * 2500)
    assert out == "y" * 2000 + "...(truncated)"
    assert len(out) == 2000 + len("...(truncated)")


def test_truncate_matches_original_inline():
    def _original(prompt):
        return prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
    for p in ["", "short", "z" * 1999, "z" * 2000, "z" * 2001, "z" * 5000]:
        assert truncate_prompt_for_log(p) == _original(p)


def test_poll_progress_basic():
    assert poll_progress_percent(0, 100) == 0
    assert poll_progress_percent(50, 100) == 50
    assert poll_progress_percent(94, 100) == 94
    assert poll_progress_percent(95, 100) == 95
    assert poll_progress_percent(99, 100) == 95   # capped at 95
    assert poll_progress_percent(100, 100) == 95  # capped


def test_poll_progress_matches_original_inline():
    def _original(attempt, max_attempts):
        return min(int((attempt / max_attempts) * 100), 95)
    for a, m in [(0, 10), (3, 10), (7, 200), (150, 200), (119, 120), (5, 120)]:
        assert poll_progress_percent(a, m) == _original(a, m)
