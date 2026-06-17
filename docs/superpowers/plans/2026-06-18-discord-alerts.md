# Discord 运维告警 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 账号失效/池告急/额度耗尽时把告警投递到 Discord webhook，让运维者无需盯日志。

**Architecture:** 独立 `alert_notifier.py`（Discord 格式化+投递）；`token_manager` 在三处事件点带去重地触发；webhook URL 走环境变量不进 git。

**Tech Stack:** Python/asyncio/curl_cffi/FastAPI/SQLite；测试用 `unittest.IsolatedAsyncioTestCase` + `AsyncMock`，`.venv/bin/python -m pytest`。

**Spec：** `docs/superpowers/specs/2026-06-18-discord-alerts-design.md`

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/services/alert_notifier.py` | Discord embed 构造 + 投递 + 容错 | 新建 |
| `src/core/config.py` | `alert_webhook_url`(env优先) + `alert_pool_low_threshold`；重命名 st_alert_webhook_url | 修改 |
| `config/setting.toml` / `setting_example.toml` | 配置项重命名 + 新增阈值 | 修改 |
| `src/services/token_manager.py` | `_alert` helper + 三处触发 + 去重；删 `_send_st_alert` | 修改 |
| `README.md` / memory | 同步配置项重命名与新告警 | 修改 |
| `tests/test_alert_notifier.py` | notifier 单测 | 新建 |
| `tests/test_alert_triggers.py` | 三触发器去重单测 | 新建 |

---

## Task 1: alert_notifier 模块（纯函数 + 投递，TDD）

**Files:** Create `src/services/alert_notifier.py`, `tests/test_alert_notifier.py`

- [ ] **Step 1: 失败测试** —— `tests/test_alert_notifier.py`：

```python
import unittest
from unittest.mock import AsyncMock, patch

from src.services.alert_notifier import build_discord_payload, AlertNotifier


class BuildPayloadTests(unittest.TestCase):
    def test_critical_red_embed_with_fields(self):
        p = build_discord_payload(
            title="账号失效需重登",
            description="账号 a@b.com 的 ST 已失效",
            fields=[("账号", "a@b.com", True), ("Token ID", "7", True), ("建议操作", "重登并粘贴 cookies.txt", False)],
            severity="critical",
        )
        self.assertIn("embeds", p)
        embed = p["embeds"][0]
        self.assertEqual(embed["title"], "账号失效需重登")
        self.assertEqual(embed["description"], "账号 a@b.com 的 ST 已失效")
        self.assertEqual(embed["color"], 15158332)  # red
        self.assertEqual(len(embed["fields"]), 3)
        self.assertEqual(embed["fields"][0], {"name": "账号", "value": "a@b.com", "inline": True})
        self.assertIn("timestamp", embed)
        self.assertIn("username", p)

    def test_warning_orange(self):
        p = build_discord_payload(title="额度耗尽", description="x", fields=None, severity="warning")
        self.assertEqual(p["embeds"][0]["color"], 15105570)  # orange
        self.assertEqual(p["embeds"][0].get("fields", []), [])


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_url_is_log_only_no_post(self):
        n = AlertNotifier("")
        with patch("src.services.alert_notifier.AsyncSession") as S:
            ok = await n.send_alert("t", "d")
        self.assertFalse(ok)
        S.assert_not_called()

    async def test_posts_discord_body(self):
        n = AlertNotifier("https://discord.test/webhook")
        sent = {}
        class FakeSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, **kw): sent["url"] = url; sent["json"] = kw.get("json")
        with patch("src.services.alert_notifier.AsyncSession", return_value=FakeSession()):
            ok = await n.send_alert("账号失效", "desc", fields=[("账号", "a@b.com", True)], severity="critical")
        self.assertTrue(ok)
        self.assertEqual(sent["url"], "https://discord.test/webhook")
        self.assertIn("embeds", sent["json"])

    async def test_post_exception_returns_false(self):
        n = AlertNotifier("https://discord.test/webhook")
        class BoomSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): raise RuntimeError("network down")
        with patch("src.services.alert_notifier.AsyncSession", return_value=BoomSession()):
            ok = await n.send_alert("t", "d")
        self.assertFalse(ok)
```

- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m pytest tests/test_alert_notifier.py -v` → FAIL（无模块）

- [ ] **Step 3: 实现 `src/services/alert_notifier.py`**：

```python
"""运维告警投递：把告警格式化为 Discord webhook embed 并尽力投递。

只负责"怎么发"；"何时/为何发"由调用方（token_manager）决定。
告警失败绝不影响主流程——所有异常都被吞掉并记日志。
"""
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple, Union

from curl_cffi.requests import AsyncSession

from ..core.logger import debug_logger

_SEVERITY_COLOR = {
    "critical": 15158332,  # 红
    "warning": 15105570,   # 橙
}
_DEFAULT_COLOR = 15105570

FieldsType = Optional[Sequence[Union[Tuple[str, str, bool], dict]]]


def _normalize_fields(fields: FieldsType) -> List[dict]:
    out: List[dict] = []
    for f in fields or []:
        if isinstance(f, dict):
            out.append({"name": str(f.get("name", "")), "value": str(f.get("value", "")),
                        "inline": bool(f.get("inline", False))})
        else:
            name, value, inline = f
            out.append({"name": str(name), "value": str(value), "inline": bool(inline)})
    return out


def build_discord_payload(title: str, description: str, fields: FieldsType = None,
                          severity: str = "warning") -> dict:
    """构造 Discord webhook body（单 embed）。纯函数，便于单测。"""
    embed = {
        "title": title,
        "description": description,
        "color": _SEVERITY_COLOR.get(severity, _DEFAULT_COLOR),
        "fields": _normalize_fields(fields),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return {"username": "Flow2API 哨兵", "embeds": [embed]}


class AlertNotifier:
    """把告警投递到 Discord webhook（尽力而为）。"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url or ""

    async def send_alert(self, title: str, description: str, fields: FieldsType = None,
                         severity: str = "warning") -> bool:
        if not self.webhook_url:
            debug_logger.log_warning(f"[ALERT] (无 webhook，仅记录) {title}: {description}")
            return False
        payload = build_discord_payload(title, description, fields, severity)
        try:
            async with AsyncSession() as session:
                await session.post(self.webhook_url, json=payload, timeout=10)
            debug_logger.log_info(f"[ALERT] 已投递: {title}")
            return True
        except Exception as e:
            debug_logger.log_warning(f"[ALERT] 投递失败 ({title}): {e}")
            return False
```

- [ ] **Step 4: 运行确认通过** —— `.venv/bin/python -m pytest tests/test_alert_notifier.py -v` → PASS（5）

- [ ] **Step 5: Commit**
```bash
git add src/services/alert_notifier.py && git add -f tests/test_alert_notifier.py
git commit -m "feat(alerts): Discord embed notifier (build + deliver, best-effort)"
```

---

## Task 2: 配置（env 优先的 alert_webhook_url + 阈值；重命名）

**Files:** `src/core/config.py`, `config/setting.toml`, `config/setting_example.toml`, `tests/test_alert_notifier.py`(追加)

- [ ] **Step 1: 失败测试**（追加到 `tests/test_alert_notifier.py`）：

```python
import os
from unittest.mock import patch as _patch
from src.core.config import config


class ConfigTests(unittest.TestCase):
    def test_env_overrides_toml_for_webhook(self):
        with _patch.dict(os.environ, {"FLOW2API_ALERT_WEBHOOK_URL": "https://env.example/wh"}):
            self.assertEqual(config.alert_webhook_url, "https://env.example/wh")

    def test_pool_low_threshold_default(self):
        self.assertGreaterEqual(config.alert_pool_low_threshold, 1)
```

- [ ] **Step 2: 运行确认失败** —— FAIL（无 `alert_webhook_url`/`alert_pool_low_threshold`）

- [ ] **Step 3a: `src/core/config.py`** 顶部加 `import os`（若没有）。把 `st_alert_webhook_url` property 替换为：

```python
    @property
    def alert_webhook_url(self) -> str:
        # 优先环境变量（密钥不进 git），回退 toml [admin] alert_webhook_url
        env = os.environ.get("FLOW2API_ALERT_WEBHOOK_URL")
        if env:
            return env.strip()
        return str(self._config.get("admin", {}).get("alert_webhook_url", "") or "")

    @property
    def alert_pool_low_threshold(self) -> int:
        try:
            return int(self._config.get("admin", {}).get("alert_pool_low_threshold", 2))
        except (TypeError, ValueError):
            return 2
```

- [ ] **Step 3b: `config/setting.toml` 与 `setting_example.toml`** 的 `[admin]` 段：把 `st_alert_webhook_url = ""` 改名为 `alert_webhook_url = ""`，并新增 `alert_pool_low_threshold = 2`。注释说明 webhook 优先读环境变量 `FLOW2API_ALERT_WEBHOOK_URL`。

- [ ] **Step 4: 运行确认通过** —— `.venv/bin/python -m pytest tests/test_alert_notifier.py -v` → PASS（含新 2 条）

- [ ] **Step 5: Commit**
```bash
git add src/core/config.py config/setting.toml config/setting_example.toml && git add -f tests/test_alert_notifier.py
git commit -m "feat(config): alert_webhook_url (env-first) + alert_pool_low_threshold"
```

---

## Task 3: token_manager 三处触发 + 去重（TDD）

**Files:** `src/services/token_manager.py`, `tests/test_alert_triggers.py`

- [ ] **Step 1: 失败测试** —— `tests/test_alert_triggers.py`：

```python
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
```

> 执行者先 Read `token_manager.py` 确认 `disable_token`/`enable_token`/`add_token`/`_do_refresh_at`/`__init__` 与 `_handle_refresh_failure` 现状，再按下方落地。`config.min_credits_to_select` 默认 1、`config.alert_pool_low_threshold` 默认 2。

- [ ] **Step 2: 运行确认失败** —— `.venv/bin/python -m pytest tests/test_alert_triggers.py -v` → FAIL

- [ ] **Step 3a: `__init__`** 末尾加 `self._pool_low_alerted = False`。

- [ ] **Step 3b: 新增 `_alert` helper**（靠近原 `_send_st_alert`）：

```python
    async def _alert(self, title: str, description: str, fields=None, severity: str = "warning") -> None:
        """构造 AlertNotifier 投递告警；任何异常都不外抛，绝不影响主流程。"""
        try:
            from ..core.config import config
            from .alert_notifier import AlertNotifier
            await AlertNotifier(config.alert_webhook_url).send_alert(title, description, fields, severity)
        except Exception as e:
            debug_logger.log_warning(f"[ALERT] 发送异常被忽略: {e}")
```

- [ ] **Step 3c: 删除 `_send_st_alert`**，并把 `_handle_refresh_failure` 里 `await self._send_st_alert(token_id)` 替换为：

```python
            token = await self.db.get_token(token_id)
            email = token.email if token else str(token_id)
            await self._alert(
                title="账号失效需重登",
                description=f"账号 {email} 的 Session Token 已被 Google 撤销/失效，需人工重登。",
                fields=[("账号", email, True), ("Token ID", str(token_id), True),
                        ("建议操作", "登录 labs.google/fx/tools/flow，在后台「添加账号」粘贴该号 cookies.txt", False)],
                severity="critical",
            )
```

- [ ] **Step 3d: `disable_token` 加池告急检测**：

```python
    async def disable_token(self, token_id: int):
        """Disable a token"""
        await self.db.update_token(token_id, is_active=False)
        await self._check_pool_low()

    async def _check_pool_low(self) -> None:
        try:
            from ..core.config import config
            active = await self.db.get_active_tokens()
            threshold = config.alert_pool_low_threshold
            if len(active) <= threshold and not self._pool_low_alerted:
                self._pool_low_alerted = True
                await self._alert(
                    title="账号池告急",
                    description=f"可用账号仅剩 {len(active)} 个（阈值 {threshold}），请尽快补充新 Pro 账号。",
                    fields=[("可用账号数", str(len(active)), True), ("阈值", str(threshold), True)],
                    severity="critical",
                )
            elif len(active) > threshold:
                self._pool_low_alerted = False
        except Exception as e:
            debug_logger.log_warning(f"[ALERT] 池告急检测异常被忽略: {e}")
```

- [ ] **Step 3e: `enable_token` 与 `add_token` 末尾**调用 `await self._check_pool_low()`（用于活跃数回升后复位标志；`_check_pool_low` 的 `elif > threshold` 分支会复位）。`enable_token` 现有实现末尾追加该调用；`add_token` 在 `return ...` 之前追加。

- [ ] **Step 3f: `_do_refresh_at` 额度耗尽**：成功分支中，把现有"读 token 清 ST_REVOKED"的那次 `get_token` 提前到更新 credits **之前**，用它同时拿 `prev_credits`；更新后判断跨越：

```python
                before = await self.db.get_token(token_id)
                prev_credits = before.credits if before else None
                credits_result = await self.flow_client.get_credits(new_at)
                new_credits = credits_result.get("credits", 0)
                await self.db.update_token(
                    token_id,
                    credits=new_credits,
                    user_paygate_tier=credits_result.get("userPaygateTier"),
                )
                if before and before.ban_reason == "ST_REVOKED":
                    await self.db.clear_token_ban(token_id)
                    debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: 健康恢复，已清除 ST_REVOKED 标记")
                from ..core.config import config as _cfg
                floor = _cfg.min_credits_to_select
                if prev_credits is not None and prev_credits > floor >= new_credits:
                    email = before.email if before else str(token_id)
                    await self._alert(
                        title="单账号额度耗尽",
                        description=f"账号 {email} 剩余额度已降至 {new_credits}（阈值 {floor}），将不再被调度。",
                        fields=[("账号", email, True), ("剩余额度", str(new_credits), True),
                                ("建议操作", "为该账号充值或更换新号", False)],
                        severity="warning",
                    )
                debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: AT 验证成功（余额: {new_credits}）")
                return True
```

> 注意：这是替换现有成功分支（保留 return True、保留 ST_REVOKED 清除逻辑），合并为"读一次 token"。执行者 Read 现有分支后等价改写，勿丢失任何现有行为。

- [ ] **Step 4: 运行确认通过** —— `.venv/bin/python -m pytest tests/test_alert_triggers.py -v` → PASS（4）

- [ ] **Step 5: 全量回归** —— `.venv/bin/python -m pytest tests/ -q` 全绿（含上一功能的 86 条）

- [ ] **Step 6: Commit**
```bash
git add src/services/token_manager.py && git add -f tests/test_alert_triggers.py
git commit -m "feat(alerts): trigger Discord alerts on revoke / pool-low / credits-drained"
```

---

## Task 4: 文档同步

**Files:** `README.md`, memory `reference_st_self_renewal_model.md`

- [ ] **Step 1:** README 把 `st_alert_webhook_url` 改为 `alert_webhook_url`，补充：env 优先（`FLOW2API_ALERT_WEBHOOK_URL`）、三类 Discord 告警、`alert_pool_low_threshold`。
- [ ] **Step 2:** 更新 memory `reference_st_self_renewal_model.md` 里 `st_alert_webhook_url` 的提及为 `alert_webhook_url`。
- [ ] **Step 3: Commit** `git commit -m "docs(alerts): document Discord alerts + alert_webhook_url rename"`

---

## Task 5: 端到端（真实 Discord）+ 部署 env

> 由编排者（非子代理）执行，涉及真实 webhook 与生产服务环境变量。

- [ ] **Step 1:** 设置运行时环境变量（systemd drop-in 或 EnvironmentFile）`FLOW2API_ALERT_WEBHOOK_URL=<用户提供的 webhook>`；`daemon-reload` + 重启 `flow2api`，确认启动无误。
- [ ] **Step 2:** 真实投递一条测试告警（直接构造 `AlertNotifier(config.alert_webhook_url).send_alert(...)` 或临时触发），确认 Discord 频道收到。
- [ ] **Step 3:** 记录结果。

---

## Task 6: 代码 Review + 迭代

- [ ] code-reviewer 审查全 diff；按反馈修复→重测→直至 review 与测试全绿。

---

## Self-Review（计划编写者自查）

- **Spec 覆盖：** notifier=Task1；config/env/rename=Task2；三触发+去重=Task3；文档=Task4；真实E2E+部署=Task5；review=Task6。✅
- **占位符：** 无 TBD；代码步骤含完整实现或等价改写指引。
- **命名一致：** `build_discord_payload`/`AlertNotifier`/`send_alert`/`alert_webhook_url`/`alert_pool_low_threshold`/`_alert`/`_check_pool_low`/`_pool_low_alerted` 全程一致。
- **风险点：** Task3 的 `_do_refresh_at` 成功分支是"等价改写+插入"，执行者须先 Read 保证不丢失现有行为（尤其 ST_REVOKED 清除与 return True）。
