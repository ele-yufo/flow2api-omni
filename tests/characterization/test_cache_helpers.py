"""Characterization: lock media-cache pure helpers."""
from tests.conftest import assert_golden


def test_cache_helpers_golden():
    from src.shared.storage.cache_helpers import (
        build_download_headers, guess_extension, normalize_cache_error)

    out = {
        "ext_video_mp4": guess_extension("http://x/v.mp4", "video"),
        "ext_video_unknown": guess_extension("http://x/v", "video"),
        "ext_image_png": guess_extension("http://x/i.png", "image"),
        "ext_image_unknown": guess_extension("http://x/i", "image"),
        "headers_image_dest": build_download_headers("image")["Sec-Fetch-Dest"],
        "headers_video_dest": build_download_headers("video")["Sec-Fetch-Dest"],
        "headers_fp_ua": build_download_headers("image", {"user_agent": "MyUA"})["User-Agent"],
        "err_filenotfound": normalize_cache_error(FileNotFoundError(2, "no", "curl")),
        "err_prefix": normalize_cache_error(Exception("Failed to cache file: real reason")),
        "err_empty": normalize_cache_error(Exception("")),
    }
    assert out["ext_video_unknown"] == ".mp4"
    assert out["ext_image_unknown"] == ".jpg"
    assert out["headers_image_dest"] == "image"
    assert out["headers_fp_ua"] == "MyUA"
    assert "curl" in out["err_filenotfound"]
    assert out["err_prefix"] == "real reason"
    assert_golden("cache_helpers", out)
