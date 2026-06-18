import unittest
from unittest.mock import AsyncMock

from src.services.token_manager import TokenManager
from src.core.models import Token


class FakeDB:
    def __init__(self, tokens):
        self._tokens = {t.id: t for t in tokens}
    async def get_token(self, tid): return self._tokens.get(tid)
    async def update_token(self, tid, **kw):
        for k, v in kw.items(): setattr(self._tokens[tid], k, v)
    async def get_active_tokens(self):
        return [t for t in self._tokens.values() if t.is_active]
    async def reset_error_count(self, tid): pass
    async def clear_token_ban(self, tid):
        self._tokens[tid].ban_reason = None


def _tm(db):
    tm = TokenManager(db=db, flow_client=AsyncMock())
    tm._alert = AsyncMock()  # 拦截告警，断言调用
    return tm


class KeepaliveSweepTests(unittest.IsolatedAsyncioTestCase):
    async def test_sweep_tries_all_active_and_survives_errors(self):
        # 保活扫描必须遍历所有活跃号，且单个号失败不中断其余
        toks = [Token(id=i, st=f"s{i}", email=f"{i}@x.com", is_active=True) for i in (1, 2, 3)]
        toks.append(Token(id=4, st="s4", email="4@x.com", is_active=False))  # 禁用号应被跳过
        db = FakeDB(toks)
        tm = TokenManager(db=db, flow_client=AsyncMock())
        seen = []
        async def fake_rotate(tid):
            seen.append(tid)
            if tid == 2:
                raise RuntimeError("boom")  # 中间一个炸
            return True
        tm.keepalive_rotate_st = fake_rotate
        await tm.keepalive_sweep(inter_delay=0)
        self.assertEqual(sorted(seen), [1, 2, 3])  # 1/2/3 都被尝试，4(禁用)跳过


class PoolLowAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_disable_below_threshold_alerts_once(self):
        toks = [Token(id=i, st=f"s{i}", email=f"{i}@x.com", is_active=True) for i in (1, 2, 3)]
        db = FakeDB(toks); tm = _tm(db)  # 阈值默认 2
        await tm.disable_token(1)  # 剩 2 个 → <=2 触发
        self.assertEqual(tm._alert.await_count, 1)
        await tm.disable_token(2)  # 剩 1 个 → 已告警过，不重发
        self.assertEqual(tm._alert.await_count, 1)

    async def test_enable_recovers_then_can_realert(self):
        toks = [Token(id=i, st=f"s{i}", email=f"{i}@x.com", is_active=True) for i in (1, 2, 3)]
        db = FakeDB(toks); tm = _tm(db)
        await tm.disable_token(1)            # 剩2 → 告警#1，标志=True
        await tm.enable_token(1)             # 回到3 → 复位
        await tm.disable_token(1)            # 剩2 → 再次告警#2
        self.assertEqual(tm._alert.await_count, 2)


class CreditsDrainedAlertTests(unittest.IsolatedAsyncioTestCase):
    def _make(self, prev_credits, new_credits):
        tok = Token(id=7, st="old", email="a@b.com", at="x", credits=prev_credits)
        db = FakeDB([tok]); tm = _tm(db)
        ff = AsyncMock()
        ff.st_to_at = AsyncMock(return_value={"access_token": "at", "expires": "2026-07-16T00:00:00.000Z", "user": {}})
        ff.get_credits = AsyncMock(return_value={"credits": new_credits, "userPaygateTier": "PAYGATE_TIER_ONE"})
        tm.flow_client = ff
        return tm

    async def test_crossing_to_drained_alerts(self):
        tm = self._make(prev_credits=50, new_credits=0)  # floor 默认 1
        await tm._do_refresh_at(7, "old")
        self.assertEqual(tm._alert.await_count, 1)

    async def test_already_drained_does_not_realert(self):
        tm = self._make(prev_credits=0, new_credits=0)
        await tm._do_refresh_at(7, "old")
        self.assertEqual(tm._alert.await_count, 0)


class Ban429PoolLowTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_ban_below_threshold_alerts(self):
        # 429 禁用让可用账号跌破阈值，也应触发池告急（不只 ST_REVOKED）
        toks = [Token(id=i, st=f"s{i}", email=f"{i}@x.com", is_active=True) for i in (1, 2, 3)]
        db = FakeDB(toks); tm = _tm(db)
        await tm.ban_token_for_429(1)  # 剩 2 个 → <=2 触发
        self.assertEqual(tm._alert.await_count, 1)
