"""Characterization: 配额耗尽双信号摘除（2026-08-16）。

USER_QUOTA_REACHED → mark_quota_exhausted 打时间标记 + credits 快照，
不改 credits / is_active（UI 数据保持真实）；负载均衡在冷却窗口内且
credits 未回涨时摘除；充值回涨、冷却到期、成功生成三者任一即回池。
锁住打标分支与摘除判定不被回归。
"""
import asyncio
from datetime import datetime, timedelta, timezone

from src.core.models import Token


# ========== generation_handler._record_generation_error ==========

class _FakeTokenManager:
    def __init__(self):
        self.marked_quota = []
        self.banned = []
        self.recorded = []

    async def mark_quota_exhausted(self, token_id):
        self.marked_quota.append(token_id)

    async def ban_token_for_429(self, token_id):
        self.banned.append(token_id)

    async def record_error(self, token_id):
        self.recorded.append(token_id)


class _FakeToken:
    id = 7
    email = "quota@example.com"


def _bare_handler():
    from src.services.generation_handler import GenerationHandler
    gh = GenerationHandler.__new__(GenerationHandler)
    gh.token_manager = _FakeTokenManager()
    return gh


def test_quota_error_marks_quota_exhausted():
    gh = _bare_handler()
    err = Exception(
        "Flow API request failed: PUBLIC_ERROR_USER_QUOTA_REACHED: "
        "Resource has been exhausted (e.g. check quota)."
    )
    asyncio.run(gh._record_generation_error(_FakeToken(), err))
    assert gh.token_manager.marked_quota == [7]
    assert gh.token_manager.banned == []       # 不禁用账号
    assert gh.token_manager.recorded == []     # 不走连续错误计数


def test_friendly_wrapped_quota_message_also_marks():
    """视频 operation 报 FAILED 时错误文案带友好前缀/后缀，也必须命中打标。"""
    gh = _bare_handler()
    err = Exception(
        "视频生成失败: PUBLIC_ERROR_USER_QUOTA_REACHED: "
        "Resource has been exhausted (e.g. check quota).，请重试"
    )
    asyncio.run(gh._record_generation_error(_FakeToken(), err))
    assert gh.token_manager.marked_quota == [7]
    assert gh.token_manager.recorded == []


def test_content_policy_error_not_counted():
    gh = _bare_handler()
    err = Exception("unsafe_generation: 输出被安全策略过滤")
    asyncio.run(gh._record_generation_error(_FakeToken(), err))
    assert gh.token_manager.marked_quota == []
    assert gh.token_manager.recorded == []


def test_non_quota_error_uses_consecutive_counter():
    gh = _bare_handler()
    err = Exception("HTTP Error 502 Bad Gateway")
    asyncio.run(gh._record_generation_error(_FakeToken(), err))
    assert gh.token_manager.marked_quota == []
    assert gh.token_manager.recorded == [7]


def test_plain_429_rate_limit_only_counted():
    """分钟级限流不该被摘除——只走连续错误计数。"""
    gh = _bare_handler()
    err = Exception("HTTP Error 429: Too Many Requests")
    asyncio.run(gh._record_generation_error(_FakeToken(), err))
    assert gh.token_manager.marked_quota == []
    assert gh.token_manager.recorded == [7]


# ========== token_manager.mark_quota_exhausted / record_success ==========

def test_mark_quota_exhausted_writes_mark_and_snapshot():
    """打标只写标记时间 + credits 快照，不动 credits / is_active / ban_reason。"""
    from src.services.token_manager import TokenManager

    class _FakeDB:
        def __init__(self, token):
            self.token = token
            self.updates = []

        async def get_token(self, token_id):
            return self.token

        async def update_token(self, token_id, **kwargs):
            self.updates.append((token_id, kwargs))

    tm = TokenManager.__new__(TokenManager)
    tm.db = _FakeDB(Token(st="s", email="e", credits=40))
    asyncio.run(tm.mark_quota_exhausted(7))
    token_id, kwargs = tm.db.updates[0]
    assert token_id == 7
    assert set(kwargs) == {"quota_exhausted_at", "quota_exhausted_credits"}
    assert kwargs["quota_exhausted_credits"] == 40
    assert isinstance(kwargs["quota_exhausted_at"], datetime)


def test_record_success_clears_quota_mark():
    """一次成功生成 = 配额恢复的最强信号，立即清标回池。"""
    from src.services.token_manager import TokenManager

    class _FakeDB:
        def __init__(self):
            self.calls = []

        async def reset_error_count(self, token_id):
            self.calls.append(("reset_error_count", token_id))

        async def clear_token_quota_mark(self, token_id):
            self.calls.append(("clear_quota_mark", token_id))

    tm = TokenManager.__new__(TokenManager)
    tm.db = _FakeDB()
    asyncio.run(tm.record_success(7))
    assert ("reset_error_count", 7) in tm.db.calls
    assert ("clear_quota_mark", 7) in tm.db.calls


# ========== load_balancer._quota_mark_excludes（纯函数判定）==========

def _lb(cooldown=43200, min_credits=20):
    from src.services.load_balancer import LoadBalancer
    lb = LoadBalancer.__new__(LoadBalancer)
    lb.min_credits_to_select = min_credits
    lb.quota_exhausted_cooldown_seconds = cooldown
    return lb


def _token(credits, marked_hours_ago=None, snapshot=None):
    kwargs = {"st": "s", "email": "e", "credits": credits}
    if marked_hours_ago is not None:
        kwargs["quota_exhausted_at"] = datetime.now(timezone.utc) - timedelta(hours=marked_hours_ago)
    if snapshot is not None:
        kwargs["quota_exhausted_credits"] = snapshot
    return Token(**kwargs)


def test_unmarked_token_not_excluded():
    assert _lb()._quota_mark_excludes(_token(credits=9)) is False


def test_marked_fresh_low_credits_excluded():
    """打标 1h、credits 已跌到 9（快照 40）→ 摘除。"""
    assert _lb()._quota_mark_excludes(_token(9, marked_hours_ago=1, snapshot=40)) is True


def test_marked_fresh_flat_credits_excluded():
    """打标 1h、credits 与配额脱节横盘 179（快照 179）→ 仍摘除，不 20 分钟翻覆。"""
    assert _lb()._quota_mark_excludes(_token(179, marked_hours_ago=1, snapshot=179)) is True


def test_marked_refilled_credits_returns():
    """打标 1h、月度充值 credits 涨到 3000（快照 40）→ 回池。"""
    assert _lb()._quota_mark_excludes(_token(3000, marked_hours_ago=1, snapshot=40)) is False


def test_marked_expired_cooldown_probes_once():
    """打标 13h（冷却 12h）→ 放行探测；仍失败会再打标。"""
    assert _lb()._quota_mark_excludes(_token(179, marked_hours_ago=13, snapshot=179)) is False


def test_marked_credits_between_threshold_and_snapshot_excluded():
    """credits=30 高于阈值 20 但低于快照 40 → 判定未回涨，继续摘除。"""
    assert _lb()._quota_mark_excludes(_token(30, marked_hours_ago=1, snapshot=40)) is True


def test_marked_without_snapshot_uses_threshold_only():
    """快照缺失（None→0）→ 上限退化为阈值 20；credits=30 回池。"""
    assert _lb()._quota_mark_excludes(_token(30, marked_hours_ago=1, snapshot=None)) is False
