"""Backward-compat shim — FileCache moved to src/shared/storage/."""
from ..shared.storage.file_cache import FileCache

__all__ = ["FileCache"]
