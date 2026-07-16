"""Backward-compat shim — the Config provider now lives in src/shared/config/.

Existing code imports `from ..core.config import config`; that keeps working via
this re-export. New code should import from `src.shared.config`. The re-exported
`config` is the SAME single app-wide instance (DB overrides 回灌至此,可变性不变)。
"""
from ..shared.config.provider import Config, Settings, config

__all__ = ["Config", "Settings", "config"]
