"""Characterization: normalize_media_id_to_uuid_str.

锁住从 _poll_video_result 抽出的 media-id 归一化纯函数——原为 3 处内联
`try: str(uuid.UUID(x)) except ValueError: x`。行为必须逐字保持:
合法 UUID 规范化(含无破折号 hex / 大写),非法输入原样返回,只吞 ValueError。
"""
import uuid

import pytest

from src.services.generation.response_parsing import normalize_media_id_to_uuid_str


def test_canonical_uuid_unchanged():
    v = "12345678-1234-1234-1234-123456789abc"
    assert normalize_media_id_to_uuid_str(v) == v


def test_hex_without_dashes_gets_canonicalized():
    assert normalize_media_id_to_uuid_str("12345678123412341234123456789abc") == \
        "12345678-1234-1234-1234-123456789abc"


def test_uppercase_lowercased():
    assert normalize_media_id_to_uuid_str("12345678-1234-1234-1234-123456789ABC") == \
        "12345678-1234-1234-1234-123456789abc"


def test_non_uuid_passthrough():
    assert normalize_media_id_to_uuid_str("not-a-uuid") == "not-a-uuid"
    assert normalize_media_id_to_uuid_str("media_abc123") == "media_abc123"
    assert normalize_media_id_to_uuid_str("") == ""


def test_only_valueerror_swallowed_typeerror_propagates():
    # 原内联代码只 catch ValueError;None 触发 TypeError,必须照旧向上抛
    with pytest.raises(TypeError):
        normalize_media_id_to_uuid_str(None)


def test_matches_original_inline_behaviour():
    """与原始 try/except 内联实现逐样例对拍。"""
    def _original(raw):
        try:
            return str(uuid.UUID(raw))
        except ValueError:
            return raw

    for sample in [
        "12345678-1234-1234-1234-123456789abc",
        "12345678123412341234123456789abc",
        "ABCDEF00-1234-5678-9ABC-DEF012345678",
        "not-a-uuid",
        "media_generation_xyz",
        "",
    ]:
        assert normalize_media_id_to_uuid_str(sample) == _original(sample)
