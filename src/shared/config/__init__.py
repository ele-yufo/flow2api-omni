"""Config provider — shared, app-agnostic settings layer.

`config` is the single app-wide mutable provider instance (toml base; DB overrides
回灌至此)。二期多租户可用 `Settings(config_path=...)` 构造独立实例。
"""
from .provider import Config, Settings, config

__all__ = ["Config", "Settings", "config"]
