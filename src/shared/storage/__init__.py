"""Storage — media download/cache/proxy (shared, app-agnostic)."""
from .file_cache import FileCache
from .media_types import detect_image_mime_type

__all__ = ["FileCache", "detect_image_mime_type"]
