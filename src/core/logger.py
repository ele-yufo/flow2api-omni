"""Backward-compat shim — DebugLogger moved to src/shared/telemetry/.

Existing `from ..core.logger import debug_logger` keeps working via this re-export.
`debug_logger` stays a single shared instance.
"""
from ..shared.telemetry.logger import DebugLogger, debug_logger

__all__ = ["DebugLogger", "debug_logger"]
