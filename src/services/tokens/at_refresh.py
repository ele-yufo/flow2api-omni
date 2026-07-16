"""Access-token refresh timing decision (pure given a clock).

Extracted from TokenManager. Decides whether a token's Google Access Token needs a
refresh: missing AT, unknown expiry, or <1h remaining. `now` is injectable for tests.
Locked by tests/characterization/test_at_refresh.py.
"""
from datetime import datetime, timezone
from typing import Optional

from ...core.models import Token
from ...shared.telemetry import debug_logger


def should_refresh_at(token: Token, now: Optional[datetime] = None) -> bool:
    """根据当前 token 快照判断是否需要刷新 AT。"""
    if not token.at:
        debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT不存在,需要刷新")
        return True

    if not token.at_expires:
        debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT过期时间未知,尝试刷新")
        return True

    if now is None:
        now = datetime.now(timezone.utc)
    if token.at_expires.tzinfo is None:
        at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
    else:
        at_expires_aware = token.at_expires

    time_until_expiry = at_expires_aware - now
    if time_until_expiry.total_seconds() < 3600:
        debug_logger.log_info(
            f"[AT_CHECK] Token {token.id}: AT即将过期 "
            f"(剩余 {time_until_expiry.total_seconds():.0f} 秒),需要刷新"
        )
        return True

    return False
