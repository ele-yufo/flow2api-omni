"""Media type detection (pure, shared).

Magic-byte image MIME sniffing extracted from FlowClient. Generic + reusable.
"""


def detect_image_mime_type(image_bytes: bytes) -> str:
    """通过文件头 magic bytes 检测图片 MIME 类型（默认 image/jpeg）。"""
    if len(image_bytes) < 12:
        return "image/jpeg"

    # WebP: RIFF....WEBP
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return "image/webp"
    # PNG: 89 50 4E 47
    if image_bytes[:4] == b'\x89PNG':
        return "image/png"
    # JPEG: FF D8 FF
    if image_bytes[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    # GIF: GIF87a 或 GIF89a
    if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    # BMP: BM
    if image_bytes[:2] == b'BM':
        return "image/bmp"
    # JPEG 2000: 00 00 00 0C 6A 50
    if image_bytes[:6] == b'\x00\x00\x00\x0cjP':
        return "image/jp2"

    return "image/jpeg"


def convert_to_jpeg(image_bytes: bytes) -> bytes:
    """将图片转换为 JPEG 格式(透明通道转 RGB)。"""
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(image_bytes))
    if img.mode in ('RGBA', 'LA', 'P'):
        img = img.convert('RGB')

    output = BytesIO()
    img.save(output, format='JPEG', quality=95)
    return output.getvalue()
