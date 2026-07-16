"""Characterization: extract_video_info.

锁住从 _poll_video_result 抽出的 video 元数据提取——原为 3 处内联
`X["operation"].get("metadata", {}).get("video", {})`。
关键:保留 `["operation"]` 的 KeyError 语义。
"""
import pytest

from src.services.generation.response_parsing import extract_video_info


def test_full_path():
    op = {"operation": {"metadata": {"video": {"fifeUrl": "u", "mediaGenerationId": "m"}}}}
    assert extract_video_info(op) == {"fifeUrl": "u", "mediaGenerationId": "m"}


def test_missing_video_returns_empty():
    assert extract_video_info({"operation": {"metadata": {}}}) == {}


def test_missing_metadata_returns_empty():
    assert extract_video_info({"operation": {}}) == {}


def test_missing_operation_key_raises_keyerror():
    # 原内联用 ["operation"] 下标,缺失时抛 KeyError —— 必须保留
    with pytest.raises(KeyError):
        extract_video_info({})


def test_matches_original_inline():
    def _original(x):
        return x["operation"].get("metadata", {}).get("video", {})

    for op in [
        {"operation": {"metadata": {"video": {"a": 1}}}},
        {"operation": {"metadata": {}}},
        {"operation": {"metadata": {"video": {}}}},
        {"operation": {"other": 1}},
    ]:
        assert extract_video_info(op) == _original(op)
