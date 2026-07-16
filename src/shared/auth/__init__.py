"""Auth — API key / admin credential verification (shared).

预留:二期外部用户鉴权(user/plan/quota)在此扩展,当前只有单 admin + API key。
"""
from .auth import (
    AuthManager,
    optional_security,
    security,
    verify_api_key_flexible,
    verify_api_key_header,
)

__all__ = [
    "AuthManager", "optional_security", "security",
    "verify_api_key_flexible", "verify_api_key_header",
]
