"""放大轮询的 media 名字必须认两种提交响应形状。

只认 operations 时，media 形状的提交会拿到空列表：轮询查不到东西，200 次空转之后
把已经放大完成的 1080P/4K 成品静默丢掉——不报错，用户只看到"放大失败"。
"""

from src.services.generation.response_parsing import media_names_for_status_poll


def test_operations_shape():
    refs = {"operations": [{"operation": {"name": "media-abc_upsampled"}}], "media": []}
    assert media_names_for_status_poll(refs) == ["media-abc_upsampled"]


def test_media_shape_is_not_dropped():
    refs = {"operations": [], "media": [{"name": "media-xyz_upsampled", "projectId": "p1"}]}
    assert media_names_for_status_poll(refs) == ["media-xyz_upsampled"]


def test_operations_wins_when_both_present():
    refs = {
        "operations": [{"operation": {"name": "op-name"}}],
        "media": [{"name": "media-name"}],
    }
    assert media_names_for_status_poll(refs) == ["op-name"]


def test_garbage_entries_are_skipped():
    refs = {
        "operations": [{"operation": {}}, "not-a-dict"],
        "media": [{"projectId": "p1"}, None, {"name": "good"}],
    }
    assert media_names_for_status_poll(refs) == ["good"]


def test_empty_refs():
    assert media_names_for_status_poll({}) == []
