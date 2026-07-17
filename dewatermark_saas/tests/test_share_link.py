from dewatermark_saas.share_link import OG_VIDEO_BASE, extract_share_uuid, og_video_url

UUID = "acf396ba-f17a-40dc-a9b8-7ddfad28be07"


def test_extract_from_full_share_url():
    url = f"https://labs.google/fx/tools/flow/shared/video/{UUID}"
    assert extract_share_uuid(url) == UUID


def test_extract_with_query_and_fragment_and_uppercase():
    assert extract_share_uuid(f"https://x/{UUID.upper()}?a=1#z") == UUID


def test_extract_bare_uuid():
    assert extract_share_uuid(UUID) == UUID


def test_extract_invalid_returns_none():
    assert extract_share_uuid("not a link") is None
    assert extract_share_uuid("") is None
    assert extract_share_uuid("1234") is None


def test_og_video_url():
    assert og_video_url(UUID) == f"{OG_VIDEO_BASE}/{UUID}"
