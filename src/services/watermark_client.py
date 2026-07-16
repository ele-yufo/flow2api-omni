"""Backward-compat shim — dewatermark_video moved to src/shared/gpu/."""
from ..shared.gpu.watermark_client import dewatermark_video

__all__ = ["dewatermark_video"]
