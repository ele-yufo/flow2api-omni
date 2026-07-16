"""GPU service clients — dewatermark (ProPainter) HTTP client (shared).

二期去水印 SaaS 直接复用此客户端调 :18290 GPU 服务。
"""
from .watermark_client import dewatermark_video

__all__ = ["dewatermark_video"]
