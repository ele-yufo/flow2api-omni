"""Characterization: is_media_generation_failed.

锁住从 _poll_video_result 抽出的终态失败谓词——原为 3 处内联
`x in ("MEDIA_GENERATION_STATUS_FAILED",) or (x or "").startswith("...ERROR")`。
"""
import pytest

from src.services.generation.response_parsing import is_media_generation_failed


@pytest.mark.parametrize("status,expected", [
    ("MEDIA_GENERATION_STATUS_FAILED", True),
    ("MEDIA_GENERATION_STATUS_ERROR", True),
    ("MEDIA_GENERATION_STATUS_ERROR_QUOTA", True),      # ERROR 前缀变体
    ("MEDIA_GENERATION_STATUS_ERROR_SAFETY", True),
    ("MEDIA_GENERATION_STATUS_SUCCESSFUL", False),
    ("MEDIA_GENERATION_STATUS_PENDING", False),
    ("MEDIA_GENERATION_STATUS_ACTIVE", False),
    ("", False),
    (None, False),                                       # None-safe
])
def test_predicate(status, expected):
    assert is_media_generation_failed(status) is expected


def test_matches_original_inline():
    def _original(x):
        return x in ("MEDIA_GENERATION_STATUS_FAILED",) or \
            (x or "").startswith("MEDIA_GENERATION_STATUS_ERROR")

    for s in [
        "MEDIA_GENERATION_STATUS_FAILED", "MEDIA_GENERATION_STATUS_ERROR",
        "MEDIA_GENERATION_STATUS_ERROR_X", "MEDIA_GENERATION_STATUS_SUCCESSFUL",
        "MEDIA_GENERATION_STATUS_PENDING", "", None, "random",
    ]:
        assert is_media_generation_failed(s) == _original(s)
