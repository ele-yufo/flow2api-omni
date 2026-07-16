"""DB — generic async sqlite engine (shared). Business repositories stay in the app layer.

二期可复用 SqliteEngine 做连接/锁/pragma 管理;各服务的表与仓储在各自 app 层定义。
"""
from .engine import SqliteEngine

__all__ = ["SqliteEngine"]
