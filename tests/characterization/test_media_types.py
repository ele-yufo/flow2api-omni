"""Characterization: lock image MIME magic-byte detection."""
from tests.conftest import assert_golden


def test_detect_image_mime_type_golden():
    from src.shared.storage.media_types import detect_image_mime_type

    samples = {
        "jpeg": b"\xff\xd8\xff\xe0" + b"0" * 20,
        "png": b"\x89PNG\r\n\x1a\n" + b"0" * 20,
        "webp": b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 8,
        "gif87": b"GIF87a" + b"0" * 20,
        "gif89": b"GIF89a" + b"0" * 20,
        "bmp": b"BM" + b"0" * 20,
        "jp2": b"\x00\x00\x00\x0cjP" + b"0" * 20,
        "too_short": b"\xff\xd8",
        "unknown": b"XXXXXXXXXXXX",
    }
    out = {k: detect_image_mime_type(v) for k, v in samples.items()}
    assert out["png"] == "image/png"
    assert out["webp"] == "image/webp"
    assert out["too_short"] == "image/jpeg"
    assert out["unknown"] == "image/jpeg"
    assert_golden("media_types", out)
