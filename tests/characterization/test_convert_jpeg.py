"""Characterization: lock PNG->JPEG conversion (magic bytes + RGB flatten)."""
from io import BytesIO


def test_convert_to_jpeg():
    from src.shared.storage.media_types import convert_to_jpeg, detect_image_mime_type
    try:
        from PIL import Image
    except ImportError:
        import pytest; pytest.skip("PIL not installed")

    # build a small RGBA PNG in memory
    buf = BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 128)).save(buf, format="PNG")
    png_bytes = buf.getvalue()
    assert detect_image_mime_type(png_bytes) == "image/png"

    jpeg = convert_to_jpeg(png_bytes)
    assert detect_image_mime_type(jpeg) == "image/jpeg"  # now JPEG
    assert jpeg[:3] == b"\xff\xd8\xff"  # JPEG magic
