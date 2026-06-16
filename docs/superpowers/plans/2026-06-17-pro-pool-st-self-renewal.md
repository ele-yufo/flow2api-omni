# Pro 账号池 ST 自我续命 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 ~15 个 Pro 账号的凭证管理做到「每个 ST 只注入一次，之后服务器纯 HTTP 自我续命，零常驻浏览器」，并提供智能粘贴注入入口与额度感知调度。

**Architecture:** 利用实测事实——labs.google `/auth/session`（即 AT 刷新）每次都通过 `Set-Cookie` 回发一个滚动续期 ~30 天的新 ST，且旧 ST 轮换后仍有效（并发安全）。改造点：① `flow_client.st_to_at` 捕获响应 `Set-Cookie` 并返回 `rotated_st`；② `token_manager` 在 AT 刷新及每日保活时把 `rotated_st` 回写 DB；③ 智能粘贴框 + cookies.txt 解析；④ 把危险的浏览器 ST 刷新（多账号会写错号）默认关闭，失效走 `ST_REVOKED` 禁用+告警；⑤ 负载均衡跳过额度耗尽账号。

**Tech Stack:** Python 3 / asyncio / FastAPI / curl_cffi / SQLite(aiosqlite) / pytest + pytest-asyncio (测试用 `unittest.IsolatedAsyncioTestCase` + `unittest.mock.AsyncMock`，与现有 `tests/` 一致) / 原生 HTML+JS 前端 (`static/manage.html`)。

**测试运行约定：** 一律用 `.venv/bin/python -m pytest <file> -v`。

**Spec 参考：** `docs/superpowers/specs/2026-06-17-pro-pool-st-self-renewal-design.md`

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/core/cookie_extractor.py` | 纯函数：从 cookies.txt/Cookie头/JSON/裸值抽取 ST | 新建 |
| `src/core/config.py` | 新增配置项 property | 修改 |
| `config/setting.toml` / `config/setting_example.toml` | 新增配置项默认值 | 修改 |
| `src/services/flow_client.py` | `st_to_at` 捕获并返回 `rotated_st`；`_make_request` 支持回传 Set-Cookie | 修改 |
| `src/services/token_manager.py` | AT 刷新/保活时回写 rotated ST；关闭浏览器 ST 刷新；ST_REVOKED+告警；`keepalive_rotate_st` | 修改 |
| `src/main.py` | 每日 `st_keepalive_task` 后台任务 | 修改 |
| `src/api/admin.py` | `AddTokenRequest.raw` + 路由解析 raw→st | 修改 |
| `static/manage.html` | 粘贴 cookies.txt 文本框 + 跳转/复制网址 | 修改 |
| `src/services/load_balancer.py` | 跳过额度耗尽账号 + 额度优先排序 | 修改 |
| `tests/test_cookie_extractor.py` | 解析器单测 | 新建 |
| `tests/test_st_rotation.py` | 轮换捕获 + 回写单测 | 新建 |
| `tests/test_load_balancer_credits.py` | 额度过滤单测 | 新建 |
| `README.md` | 文档同步 | 修改 |

---

## Task 1: 配置脚手架（新增配置项）

**Files:**
- Modify: `config/setting.toml`
- Modify: `config/setting_example.toml`
- Modify: `src/core/config.py`
- Test: `tests/test_st_rotation.py`

- [ ] **Step 1: 写失败测试（config 默认值）**

新建 `tests/test_st_rotation.py`：

```python
import unittest
from unittest.mock import AsyncMock

from src.core.config import config


class ConfigDefaultsTests(unittest.TestCase):
    def test_st_keepalive_defaults(self):
        self.assertIsInstance(config.st_keepalive_enabled, bool)
        self.assertGreaterEqual(config.st_keepalive_interval_hours, 1)

    def test_st_browser_refresh_disabled_by_default(self):
        # 多账号下浏览器 ST 刷新会写错号，必须默认关闭
        self.assertFalse(config.st_browser_refresh_enabled)

    def test_min_credits_to_select_default(self):
        self.assertGreaterEqual(config.min_credits_to_select, 0)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_st_rotation.py::ConfigDefaultsTests -v`
Expected: FAIL（`AttributeError: 'Config' object has no attribute 'st_keepalive_enabled'`）

- [ ] **Step 3: 在 `src/core/config.py` 增加 property（放到文件内已有 property 区块末尾、`class Config` 内）**

```python
    # ========== ST 自我续命 / 保活 ==========
    @property
    def st_keepalive_enabled(self) -> bool:
        return bool(self._config.get("token", {}).get("st_keepalive_enabled", True))

    @property
    def st_keepalive_interval_hours(self) -> int:
        try:
            return int(self._config.get("token", {}).get("st_keepalive_interval_hours", 24))
        except (TypeError, ValueError):
            return 24

    @property
    def st_browser_refresh_enabled(self) -> bool:
        # 默认 False：多账号池下浏览器只登录一个号，用它刷新会把别的号 ST 写错
        return bool(self._config.get("token", {}).get("st_browser_refresh_enabled", False))

    @property
    def min_credits_to_select(self) -> int:
        try:
            return int(self._config.get("call_logic", {}).get("min_credits_to_select", 1))
        except (TypeError, ValueError):
            return 1

    @property
    def st_alert_webhook_url(self) -> str:
        return str(self._config.get("admin", {}).get("st_alert_webhook_url", "") or "")
```

- [ ] **Step 4: 在 `config/setting.toml` 与 `config/setting_example.toml` 增加默认段**

在两个文件都追加（`setting.toml` 用于本机运行，`setting_example.toml` 同步给文档）：

```toml
[token]
# ST 每日保活巡检：即使账号闲置也定时滚动续期 __Secure-next-auth.session-token
st_keepalive_enabled = true
st_keepalive_interval_hours = 24
# 浏览器 ST 刷新（旧机制）。多账号池下会写错号，默认关闭；单 Ultra 老部署可临时开
st_browser_refresh_enabled = false
```

并在 `[call_logic]` 段追加：

```toml
# 负载均衡时跳过剩余额度 <= 此值的账号（Pro 账号额度耗尽自动跳过）
min_credits_to_select = 1
```

并在 `[admin]` 段追加：

```toml
# ST 被 Google 撤销时的告警 webhook（POST JSON）。留空则只记日志
st_alert_webhook_url = ""
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_st_rotation.py::ConfigDefaultsTests -v`
Expected: PASS（3 passed）

- [ ] **Step 6: Commit**

```bash
git add src/core/config.py config/setting.toml config/setting_example.toml tests/test_st_rotation.py
git commit -m "feat(config): add ST keepalive / credits-floor / alert config keys"
```

---

## Task 2: cookies.txt 智能解析器（纯函数 TDD）

**Files:**
- Create: `src/core/cookie_extractor.py`
- Test: `tests/test_cookie_extractor.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_cookie_extractor.py`：

```python
import unittest

from src.core.cookie_extractor import extract_session_token

KEY = "__Secure-next-auth.session-token"
VALID = "eyJ" + "A" * 1100  # 模拟一个够长的 JWE


class CookieExtractorTests(unittest.TestCase):
    def test_netscape_cookies_txt_fulltext(self):
        raw = (
            "# Netscape HTTP Cookie File\n"
            "# comment line\n"
            ".labs.google\tTRUE\t/\tFALSE\t1816193751\t_ga\tGA1.1.x\n"
            f"labs.google\tFALSE\t/\tTRUE\t1784225752\t{KEY}\t{VALID}\n"
            "labs.google\tFALSE\t/\tFALSE\t0\temail\truby%40gmail.com\n"
        )
        self.assertEqual(extract_session_token(raw), VALID)

    def test_cookie_header(self):
        raw = f"_ga=GA1.1.x; {KEY}={VALID}; email=ruby%40gmail.com"
        self.assertEqual(extract_session_token(raw), VALID)

    def test_json_array(self):
        raw = f'[{{"name":"_ga","value":"x"}},{{"name":"{KEY}","value":"{VALID}"}}]'
        self.assertEqual(extract_session_token(raw), VALID)

    def test_bare_token(self):
        self.assertEqual(extract_session_token(f"  {VALID}  "), VALID)

    def test_missing_raises(self):
        with self.assertRaises(ValueError):
            extract_session_token("_ga=GA1.1.x; email=ruby%40gmail.com")

    def test_too_short_raises(self):
        raw = f"labs.google\tFALSE\t/\tTRUE\t0\t{KEY}\tundefined\n"
        with self.assertRaises(ValueError):
            extract_session_token(raw)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            extract_session_token("   ")

    def test_picks_last_when_duplicated(self):
        raw = (
            f"labs.google\tFALSE\t/\tTRUE\t0\t{KEY}\t{'eyJ' + 'B' * 1100}\n"
            f"labs.google\tFALSE\t/\tTRUE\t0\t{KEY}\t{VALID}\n"
        )
        self.assertEqual(extract_session_token(raw), VALID)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cookie_extractor.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.core.cookie_extractor'`）

- [ ] **Step 3: 实现 `src/core/cookie_extractor.py`**

```python
"""从多种粘贴格式中抽取 __Secure-next-auth.session-token (ST)。

支持（按优先级）：
1. Netscape cookies.txt 全文（制表符分隔，7 列，第 6 列为 name、第 7 列为 value）
2. Cookie 请求头 / `a=b; key=value` 分号串
3. JSON 数组（DevTools "Copy all as JSON" / EditThisCookie 导出）
4. 裸 ST 值（以 eyJ 开头的 JWE）

抽不到或长度 < MIN_ST_LEN 时抛 ValueError。
"""
import json
from typing import Optional

SESSION_TOKEN_KEY = "__Secure-next-auth.session-token"
MIN_ST_LEN = 200  # 实测 ST ~1064，护栏防止把 undefined/截断值写入


def _from_json(text: str) -> Optional[str]:
    stripped = text.lstrip()
    if not (stripped.startswith("[") or stripped.startswith("{")):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    cookies = data if isinstance(data, list) else data.get("cookies", [])
    if not isinstance(cookies, list):
        return None
    for item in cookies:
        if isinstance(item, dict) and item.get("name") == SESSION_TOKEN_KEY:
            value = item.get("value")
            if isinstance(value, str):
                return value.strip()
    return None


def _from_netscape(text: str) -> Optional[str]:
    found: Optional[str] = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5].strip() == SESSION_TOKEN_KEY:
            found = parts[6].strip()  # 命中多条时保留最后一条（最新）
    return found


def _from_cookie_header(text: str) -> Optional[str]:
    marker = SESSION_TOKEN_KEY + "="
    if marker not in text:
        return None
    seg = text.split(marker, 1)[1]
    # 值止于 ; 或空白/换行；ST 本身无空格
    value = seg.split(";", 1)[0].strip()
    if value:
        value = value.split()[0]
    return value or None


def _from_bare(text: str) -> Optional[str]:
    tokens = text.split()
    if tokens and tokens[0].startswith("eyJ"):
        return tokens[0]
    return None


def extract_session_token(raw: str) -> str:
    """从任意支持的格式中抽取 ST；失败抛 ValueError。"""
    if not raw or not raw.strip():
        raise ValueError("空输入：未提供任何内容")
    text = raw.strip()

    candidate = (
        _from_json(text)
        or _from_netscape(text)
        or _from_cookie_header(text)
        or _from_bare(text)
    )

    if not candidate:
        raise ValueError(
            f"未能从输入中找到 {SESSION_TOKEN_KEY}。"
            f"请粘贴 cookies.txt 全文、Cookie 头、JSON 导出或裸 ST 值。"
        )
    candidate = candidate.strip()
    if len(candidate) < MIN_ST_LEN:
        raise ValueError(
            f"提取到的 ST 过短 (len={len(candidate)} < {MIN_ST_LEN})，疑似无效或已损坏。"
        )
    return candidate
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cookie_extractor.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
git add src/core/cookie_extractor.py tests/test_cookie_extractor.py
git commit -m "feat(token): smart cookies.txt/header/json ST extractor"
```

---

## Task 3: flow_client 捕获轮换 ST

**Files:**
- Modify: `src/services/flow_client.py`（`st_to_at` 约 790-811；`_make_request` 签名约 325-339 与主路径 return 约 498）
- Test: `tests/test_st_rotation.py`

- [ ] **Step 1: 写失败测试**

向 `tests/test_st_rotation.py` 追加：

```python
from src.services.flow_client import FlowClient

LONG_ST = "eyJ" + "C" * 1100


class StToAtRotationTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_rotated_st_helper(self):
        client = FlowClient(proxy_manager=None)
        headers = [
            "__Host-next-auth.csrf-token=abc; Path=/; HttpOnly",
            f"__Secure-next-auth.session-token={LONG_ST}; Path=/; Expires=Thu, 16 Jul 2026 00:00:00 GMT; HttpOnly; Secure",
        ]
        self.assertEqual(client._extract_rotated_st_from_set_cookie(headers), LONG_ST)

    def test_extract_rotated_st_ignores_short(self):
        client = FlowClient(proxy_manager=None)
        headers = ["__Secure-next-auth.session-token=undefined; Path=/"]
        self.assertIsNone(client._extract_rotated_st_from_set_cookie(headers))

    async def test_st_to_at_attaches_rotated_st(self):
        client = FlowClient(proxy_manager=None)

        async def fake_make_request(**kwargs):
            cap = kwargs.get("capture_set_cookie")
            if cap is not None:
                cap.append(f"__Secure-next-auth.session-token={LONG_ST}; Path=/")
            return {"access_token": "AT", "expires": "2026-07-16T00:00:00.000Z", "user": {"email": "x@y.com"}}

        client._make_request = AsyncMock(side_effect=fake_make_request)
        result = await client.st_to_at("old-st-value")
        self.assertEqual(result["access_token"], "AT")
        self.assertEqual(result["rotated_st"], LONG_ST)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_st_rotation.py::StToAtRotationTests -v`
Expected: FAIL（`AttributeError: ... _extract_rotated_st_from_set_cookie`）

- [ ] **Step 3a: 在 `FlowClient` 增加纯静态方法**（放到 `st_to_at` 上方，认证相关区块内）

```python
    @staticmethod
    def _extract_rotated_st_from_set_cookie(set_cookie_headers) -> Optional[str]:
        """从响应的 Set-Cookie 头里解析轮换后的 __Secure-next-auth.session-token。

        labs.google /auth/session 每次会回发一个滚动续期 ~30 天的新 ST。
        长度护栏 >= 200，防止把异常短值当成有效 ST。
        """
        key = "__Secure-next-auth.session-token"
        for raw in set_cookie_headers or []:
            if isinstance(raw, str) and raw.startswith(key + "="):
                value = raw.split("=", 1)[1].split(";", 1)[0].strip()
                if len(value) >= 200:
                    return value
        return None
```

- [ ] **Step 3b: 修改 `_make_request` 签名**，在参数列表（约 `force_urllib: bool = False,` 之后）加入：

```python
        capture_set_cookie: Optional[List[str]] = None,
```

- [ ] **Step 3c: 在 `_make_request` 主 curl_cffi 路径 `return response.json()`（约 498 行）之前插入**：

```python
                if capture_set_cookie is not None:
                    try:
                        capture_set_cookie.extend(response.headers.get_list("set-cookie"))
                    except Exception:
                        single = response.headers.get("set-cookie")
                        if single:
                            capture_set_cookie.append(single)

                return response.json()
```

（仅替换那一行 `return response.json()`，前面补上 capture 块；urllib/captcha-browser 分支不回传 cookie，对 ST 续命无影响。）

- [ ] **Step 3d: 重写 `st_to_at` 以捕获 rotated_st**：

```python
    async def st_to_at(self, st: str) -> dict:
        """ST转AT；并捕获 labs.google 回发的轮换新 ST (rotated_st)。

        Returns:
            {
                "access_token": "AT",
                "expires": "2025-11-15T04:46:04.000Z",
                "user": {...},
                "rotated_st": "<新 ST 或不存在该键>"
            }
        """
        url = f"{self.labs_base_url}/auth/session"
        set_cookies: List[str] = []
        result = await self._make_request(
            method="GET",
            url=url,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
            capture_set_cookie=set_cookies,
        )
        rotated_st = self._extract_rotated_st_from_set_cookie(set_cookies)
        if rotated_st and rotated_st != st:
            result["rotated_st"] = rotated_st
        return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_st_rotation.py::StToAtRotationTests -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 跑一遍现有 flow_client 测试确认无回归**

Run: `.venv/bin/python -m pytest tests/test_flow_client_upload.py -v`
Expected: PASS（全部）

- [ ] **Step 6: Commit**

```bash
git add src/services/flow_client.py tests/test_st_rotation.py
git commit -m "feat(flow_client): capture rotated ST from /auth/session Set-Cookie"
```

---

## Task 4: token_manager 回写轮换 ST + 关闭危险的浏览器 ST 刷新 + ST_REVOKED

**Files:**
- Modify: `src/services/token_manager.py`（`_do_refresh_at` 约 477-536；`_try_refresh_st` 约 538-592；新增 `keepalive_rotate_st`）
- Test: `tests/test_st_rotation.py`

- [ ] **Step 1: 写失败测试**

向 `tests/test_st_rotation.py` 追加（用轻量假 db + 假 flow_client）：

```python
from src.services.token_manager import TokenManager
from src.core.models import Token


class FakeDB:
    def __init__(self, token):
        self._token = token
        self.updates = []

    async def get_token(self, token_id):
        return self._token

    async def update_token(self, token_id, **kwargs):
        self.updates.append(kwargs)
        for k, v in kwargs.items():
            setattr(self._token, k, v)


class PersistRotatedStTests(unittest.IsolatedAsyncioTestCase):
    def _make_manager(self, rotated_st):
        token = Token(id=7, st="old-st", email="ruby@gmail.com", at="old-at", credits=1000)
        db = FakeDB(token)
        tm = TokenManager(db=db, flow_client=None)
        fake_flow = AsyncMock()
        fake_flow.st_to_at = AsyncMock(return_value={
            "access_token": "new-at",
            "expires": "2026-07-16T00:00:00.000Z",
            "user": {"email": "ruby@gmail.com"},
            "rotated_st": rotated_st,
        })
        fake_flow.get_credits = AsyncMock(return_value={"credits": 1000, "userPaygateTier": "PAYGATE_TIER_ONE"})
        tm.flow_client = fake_flow
        return tm, db

    async def test_do_refresh_at_persists_rotated_st(self):
        new_st = "eyJ" + "D" * 1100
        tm, db = self._make_manager(new_st)
        ok = await tm._do_refresh_at(7, "old-st")
        self.assertTrue(ok)
        self.assertTrue(any(u.get("st") == new_st for u in db.updates))

    async def test_do_refresh_at_skips_when_rotated_equals_current(self):
        tm, db = self._make_manager("old-st")  # 与当前相同
        await tm._do_refresh_at(7, "old-st")
        self.assertFalse(any("st" in u for u in db.updates))


class KeepaliveConservativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_does_not_disable(self):
        token = Token(id=7, st="old-st", email="r@x.com", is_active=True)
        db = FakeDB(token)
        tm = TokenManager(db=db, flow_client=AsyncMock())
        tm._do_refresh_at = AsyncMock(return_value=False)  # 刷新失败，但 ban_reason 未变
        tm.disable_token = AsyncMock()
        tm._send_st_alert = AsyncMock()
        ok = await tm.keepalive_rotate_st(7)
        self.assertFalse(ok)
        tm.disable_token.assert_not_called()  # 瞬时错误不禁用
        tm._send_st_alert.assert_not_called()

    async def test_revoked_disables_and_alerts(self):
        token = Token(id=7, st="old-st", email="r@x.com", is_active=True)
        db = FakeDB(token)
        tm = TokenManager(db=db, flow_client=AsyncMock())

        async def fake_refresh(tid, st):
            token.ban_reason = "ST_REVOKED"  # 模拟 _do_refresh_at 在确认 401 时标记
            return False

        tm._do_refresh_at = AsyncMock(side_effect=fake_refresh)
        tm.disable_token = AsyncMock()
        tm._send_st_alert = AsyncMock()
        ok = await tm.keepalive_rotate_st(7)
        self.assertFalse(ok)
        tm.disable_token.assert_called_once()
        tm._send_st_alert.assert_called_once()
```

> 注：若 `TokenManager.__init__` 形参名不同，按实际签名调整 `TokenManager(...)` 的构造。执行者应先 Read `token_manager.py` 顶部确认构造参数与锁初始化。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_st_rotation.py::PersistRotatedStTests -v`
Expected: FAIL（rotated st 未被回写）

- [ ] **Step 3a: 在 `_do_refresh_at` 中回写 rotated_st，并在“确认是 401/auth 失败”的两处精确标记 ST_REVOKED（不在瞬时错误处标记）。**

(i) 在 `_do_refresh_at` 内 `result = await self.flow_client.st_to_at(st)` 之后新增 rotated_st 回写：

```python
            # 捕获并回写轮换后的新 ST（滚动续期 ~30 天的关键）
            rotated_st = result.get("rotated_st")
            if rotated_st and rotated_st != st and len(rotated_st) >= 200:
                await self.db.update_token(token_id, st=rotated_st)
                debug_logger.log_info(f"[ST_ROTATE] Token {token_id}: ST 已滚动续期并回写库")
```

(ii) 在 get_credits 验证失败的 **401 分支**（现约 526-528 行 `if "401" in error_msg or "UNAUTHENTICATED" ...`）里，标记 ST_REVOKED：

```python
                if "401" in error_msg or "UNAUTHENTICATED" in error_msg:
                    debug_logger.log_warning(f"[AT_REFRESH] Token {token_id}: AT 验证失败 (401)，ST 可能已过期")
                    await self.db.update_token(token_id, ban_reason="ST_REVOKED")
                    return False
```

(iii) 在最外层 `except Exception as e:`（st_to_at 抛错处，现约 534-536 行）里，**仅当错误是 401/auth 时**标记 ST_REVOKED，瞬时网络错误不标记：

```python
        except Exception as e:
            error_msg = str(e)
            if "401" in error_msg or "UNAUTHENTICATED" in error_msg:
                await self.db.update_token(token_id, ban_reason="ST_REVOKED")
            debug_logger.log_error(f"[AT_REFRESH] Token {token_id}: AT刷新失败 - {error_msg}")
            return False
```

> 关键：ST_REVOKED 只在**确认鉴权失败**时打；超时/5xx/网络瞬断**绝不**打，从而避免健康账号被误判。

- [ ] **Step 3b: 关闭危险的浏览器 ST 刷新。** 修改 `_try_refresh_st`，在方法体最前面（`try:` 之前）加 config 门控（默认 False → 直接返回 None，不再用共享 profile 写错号）：

```python
        from ..core.config import config

        if not config.st_browser_refresh_enabled:
            debug_logger.log_info(
                f"[ST_REFRESH] Token {token_id}: 浏览器 ST 刷新已禁用 "
                f"(st_browser_refresh_enabled=false，多账号下会写错号)，跳过"
            )
            return None
```

（保留原有后续逻辑，仅在前面加这道门。）

- [ ] **Step 3c: 不修改 `_refresh_at_inner`。** 它的「失败即 disable」是既有 on-use 行为；ST_REVOKED 的精确标记已在 3a 的 `_do_refresh_at`（被 on-use 与保活共用）完成，无需也不应在 inner 里盲目 disable+告警（会把瞬时错误也当撤销）。

- [ ] **Step 3d: 新增 `_send_st_alert` 与 `keepalive_rotate_st`（保守版：只在确认撤销时 disable+告警）。** 加到 TokenManager 内，建议靠近 `_try_refresh_st`：

```python
    async def _send_st_alert(self, token_id: int) -> None:
        """ST 被撤销时尽力发一条 webhook 告警；无 URL 则只记日志。"""
        from ..core.config import config
        try:
            token = await self.db.get_token(token_id)
            email = token.email if token else str(token_id)
        except Exception:
            email = str(token_id)
        url = config.st_alert_webhook_url
        if not url:
            debug_logger.log_warning(f"[ST_ALERT] Token {token_id} ({email}) ST 已失效，需重新注入")
            return
        try:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as session:
                await session.post(
                    url,
                    json={"event": "ST_REVOKED", "token_id": token_id, "email": email},
                    timeout=10,
                )
        except Exception as e:
            debug_logger.log_warning(f"[ST_ALERT] webhook 发送失败: {e}")

    async def keepalive_rotate_st(self, token_id: int) -> bool:
        """每日保活：滚动 ST 续期。直接走 _do_refresh_at（它会捕获并回写 rotated ST、
        并仅在确认 401 时标记 ST_REVOKED），**不经过会“失败即 disable”的 _refresh_at_inner**。

        只有当刷新失败且确认是 ST 被撤销 (ban_reason=ST_REVOKED) 时才 disable+告警；
        瞬时网络错误保留账号，待下一轮重试。Returns True 表示该号仍健康。
        """
        token = await self.db.get_token(token_id)
        if not token or not token.is_active:
            return False
        ok = await self._do_refresh_at(token_id, token.st)
        if ok:
            return True
        refreshed = await self.db.get_token(token_id)
        if refreshed and refreshed.ban_reason == "ST_REVOKED":
            debug_logger.log_error(f"[ST_KEEPALIVE] Token {token_id}: ST 已被撤销，禁用并告警")
            await self._send_st_alert(token_id)
            await self.disable_token(token_id)
        else:
            debug_logger.log_warning(f"[ST_KEEPALIVE] Token {token_id}: 瞬时刷新失败，保留账号待下轮")
        return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_st_rotation.py::PersistRotatedStTests tests/test_st_rotation.py::KeepaliveConservativeTests -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 全量跑 token 相关测试无回归**

Run: `.venv/bin/python -m pytest tests/test_st_rotation.py -v`
Expected: PASS（全部）

- [ ] **Step 6: Commit**

```bash
git add src/services/token_manager.py tests/test_st_rotation.py
git commit -m "feat(token): persist rotated ST; disable unsafe browser ST refresh; ST_REVOKED alert"
```

---

## Task 5: 每日保活后台任务

**Files:**
- Modify: `src/main.py`（lifespan 内，仿 `auto_unban_task` 约 116-134 / 145-149）

- [ ] **Step 1: 在 `auto_unban_task` 定义之后、`auto_unban_task_handle = ...` 附近，新增保活任务**

```python
    async def st_keepalive_task():
        """定时任务：滚动续期所有活跃 token 的 ST（纯 HTTP，无浏览器）"""
        interval = max(1, config.st_keepalive_interval_hours) * 3600
        while True:
            try:
                await asyncio.sleep(interval)
                if not config.st_keepalive_enabled:
                    continue
                tokens = await db.get_active_tokens()
                for token in tokens:
                    try:
                        await token_manager.keepalive_rotate_st(token.id)
                    except Exception as e:
                        print(f"⚠ ST keepalive failed for token {token.id}: {e}")
            except Exception as e:
                print(f"❌ ST keepalive task error: {e}")

    st_keepalive_handle = None
    if config.st_keepalive_enabled:
        st_keepalive_handle = asyncio.create_task(st_keepalive_task())
```

- [ ] **Step 2: 在 shutdown 段（`auto_unban_task_handle.cancel()` 附近）增加取消逻辑**

```python
    if st_keepalive_handle is not None:
        st_keepalive_handle.cancel()
        try:
            await st_keepalive_handle
        except asyncio.CancelledError:
            pass
```

- [ ] **Step 3: 在启动日志区追加一行**

```python
    print(f"✓ ST keepalive task: {'started' if config.st_keepalive_enabled else 'disabled'} (every {config.st_keepalive_interval_hours}h)")
```

- [ ] **Step 4: 冒烟——确认进程能启动（语法/导入正确）**

Run: `.venv/bin/python -c "import src.main"`
Expected: 无异常退出（exit 0）

- [ ] **Step 5: Commit**

```bash
git add src/main.py
git commit -m "feat(main): daily ST keepalive background task"
```

---

## Task 6: Admin API —— 支持 raw 粘贴注入

**Files:**
- Modify: `src/api/admin.py`（`AddTokenRequest` 约 479-489；`add_token` 路由约 684-725）
- Test: `tests/test_cookie_extractor.py`（追加 API 解析集成点的轻量测试）

- [ ] **Step 1: 写失败测试（解析集成：raw→st 的解析函数被正确调用）**

向 `tests/test_cookie_extractor.py` 追加：

```python
class ResolveStFromRequestTests(unittest.TestCase):
    def test_resolve_prefers_raw(self):
        from src.api.admin import resolve_st_from_request
        raw = f"x=1; {KEY}={VALID}"
        self.assertEqual(resolve_st_from_request(st=None, raw=raw), VALID)

    def test_resolve_uses_st_when_no_raw(self):
        from src.api.admin import resolve_st_from_request
        self.assertEqual(resolve_st_from_request(st=VALID, raw=None), VALID)

    def test_resolve_none_raises(self):
        from src.api.admin import resolve_st_from_request
        with self.assertRaises(ValueError):
            resolve_st_from_request(st=None, raw=None)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_cookie_extractor.py::ResolveStFromRequestTests -v`
Expected: FAIL（`ImportError: cannot import name 'resolve_st_from_request'`）

- [ ] **Step 3a: 在 `src/api/admin.py` 顶部 import 区加入**

```python
from ..core.cookie_extractor import extract_session_token
```

- [ ] **Step 3b: 在 `AddTokenRequest` 增加可选字段，并把 `st` 改为可选**

```python
class AddTokenRequest(BaseModel):
    st: Optional[str] = None
    raw: Optional[str] = None  # 粘贴 cookies.txt 全文 / Cookie 头 / JSON，自动抽取 ST
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
```

- [ ] **Step 3c: 在 `AddTokenRequest` 定义之后新增模块级辅助函数**

```python
def resolve_st_from_request(st: Optional[str], raw: Optional[str]) -> str:
    """优先用 raw 粘贴内容抽取 ST；否则用直接传入的 st。两者皆空抛 ValueError。"""
    if raw and raw.strip():
        return extract_session_token(raw)
    if st and st.strip():
        return st.strip()
    raise ValueError("必须提供 st 或 raw（cookies.txt/Cookie头/JSON）之一")
```

- [ ] **Step 3d: 在 `add_token` 路由开头解析 st**（`try:` 内第一步）

```python
    try:
        try:
            resolved_st = resolve_st_from_request(request.st, request.raw)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        new_token = await token_manager.add_token(
            st=resolved_st,
            project_id=request.project_id,
            ...原有其余参数不变...
        )
```

（把原来的 `st=request.st` 改为 `st=resolved_st`，其余参数保持不变。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_cookie_extractor.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add src/api/admin.py tests/test_cookie_extractor.py
git commit -m "feat(admin): accept raw cookies.txt paste in POST /api/tokens"
```

---

## Task 7: 前端粘贴框 + 跳转/复制网址

**Files:**
- Modify: `static/manage.html`

> 执行者先 Read `static/manage.html`，定位现有「添加 Token」表单/模态（搜索 `/api/tokens`、`添加`、`st`），仿其样式与提交方式接入。

- [ ] **Step 1: 在添加 Token 表单区加入操作区与文本框**

在现有添加表单内（ST 输入控件附近）插入：

```html
<div class="add-token-paste">
  <p>第 1 步：登录 Google 账号 ↓</p>
  <a id="labsFlowLink" href="https://labs.google/fx/tools/flow" target="_blank" rel="noopener">🔗 打开 labs.google/fx/tools/flow</a>
  <button type="button" id="copyLabsUrlBtn">📋 复制网址</button>
  <p>第 2 步：导出 cookies.txt，把全文整段粘到下面（也支持 Cookie 头 / JSON / 裸 ST）</p>
  <textarea id="rawCookiesInput" rows="6" placeholder="# Netscape HTTP Cookie File ... 直接粘全文，服务器自动抽取 __Secure-next-auth.session-token"></textarea>
  <small>提示：该 cookie 是 HttpOnly，需用「Get cookies.txt LOCALLY」类扩展导出，或 DevTools → Application → Cookies → labs.google 复制单条值。</small>
</div>
```

- [ ] **Step 2: 复制按钮 + 改造现有 `submitAddToken()` 走 `apiRequest`（不要用裸 fetch/手填 Bearer）**

复制按钮（可放在 `<script>` 任意位置或 inline `onclick`）：

```javascript
document.getElementById('copyLabsUrlBtn').addEventListener('click', async () => {
  const url = document.getElementById('labsFlowLink').href;
  try { await navigator.clipboard.writeText(url); alert('网址已复制'); }
  catch (e) { prompt('手动复制以下网址：', url); }
});
```

改造既有 `submitAddToken()`（约 L854）：在构造请求体处，**优先取粘贴框 `#rawCookiesInput`**；非空则发 `{raw, ...}`，否则维持原 `{st: addTokenST.value, ...}`。复用现有 `apiRequest`（它自动从 `localStorage.adminToken` 注入 `Authorization: Bearer` 与 `Content-Type`）：

```javascript
  const rawCookies = (document.getElementById('rawCookiesInput')?.value || '').trim();
  const st = document.getElementById('addTokenST').value.trim();
  if (!rawCookies && !st) { alert('请粘贴 cookies.txt 全文，或填写 ST'); return; }

  // 其余字段沿用原 submitAddToken 已读取的值（remark/project/并发/开关等）
  const body = {
    remark: document.getElementById('addTokenRemark').value.trim() || undefined,
    // ...原有 project_id / project_name / *_enabled / *_concurrency / captcha_proxy_url 保持不变...
  };
  if (rawCookies) { body.raw = rawCookies; } else { body.st = st; }

  const resp = await apiRequest('/api/tokens', { method: 'POST', body: JSON.stringify(body) });
  // ...原有成功/失败处理、刷新列表、closeAddModal() 不变...
```

并在 `closeAddModal()`（约 L848）里清空粘贴框：

```javascript
  const rawEl = document.getElementById('rawCookiesInput');
  if (rawEl) rawEl.value = '';
```

> 执行者先 Read `static/manage.html` 的 `submitAddToken` / `closeAddModal` / `apiRequest`（约 L831/L848/L854）与各 `#addToken*` 元素，按其真实结构落地，保持与其它请求一致（绝不手填 Bearer）。

- [ ] **Step 3: 冒烟——HTML 能被静态服务返回**

Run: `.venv/bin/python -c "p=open('static/manage.html',encoding='utf-8').read(); assert 'rawCookiesInput' in p and 'labs.google/fx/tools/flow' in p; print('ok')"`
Expected: 打印 `ok`

- [ ] **Step 4: Commit**

```bash
git add static/manage.html
git commit -m "feat(ui): paste cookies.txt box with labs.google jump/copy"
```

---

## Task 8: 额度感知调度（跳过额度耗尽账号）

**Files:**
- Modify: `src/services/load_balancer.py`（`select_token` 过滤循环约 164-206；构造函数读取 config）
- Test: `tests/test_load_balancer_credits.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_load_balancer_credits.py`：

```python
import unittest
from unittest.mock import AsyncMock

from src.services.load_balancer import LoadBalancer
from src.core.models import Token


class CreditsFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_drained_tokens(self):
        tokens = [
            Token(id=1, st="a", email="a@x.com", credits=0, user_paygate_tier="PAYGATE_TIER_ONE"),
            Token(id=2, st="b", email="b@x.com", credits=500, user_paygate_tier="PAYGATE_TIER_ONE"),
        ]
        tm = AsyncMock()
        tm.get_active_tokens = AsyncMock(return_value=tokens)
        tm.needs_at_refresh = lambda t: False
        lb = LoadBalancer(token_manager=tm, concurrency_manager=None)
        lb._get_token_load = AsyncMock(return_value=(0, 999))
        selected = await lb.select_token(for_image_generation=True)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, 2)  # 额度为 0 的被跳过
```

> 执行者先 Read `load_balancer.py` 顶部确认 `LoadBalancer.__init__` 形参与现有属性名，按实际调整构造与属性。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_load_balancer_credits.py -v`
Expected: FAIL（额度 0 的 token 仍被选中或抛错）

- [ ] **Step 3a: 在 `LoadBalancer.__init__` 读取阈值**（构造函数内，配合现有属性）

```python
        from ..core.config import config
        self.min_credits_to_select = config.min_credits_to_select
```

- [ ] **Step 3b: 在 `select_token` 过滤循环里，tier 检查之后加入额度过滤**

在 `for token in active_tokens:` 循环内、`if model and not supports_model_for_tier(...)` 之后插入：

```python
            if token.credits is not None and token.credits <= self.min_credits_to_select:
                filtered_reasons[token.id] = f"额度不足 (credits={token.credits})"
                continue
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_load_balancer_credits.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: Commit**

```bash
git add src/services/load_balancer.py tests/test_load_balancer_credits.py
git commit -m "feat(load_balancer): skip credit-drained tokens via min_credits_to_select"
```

---

## Task 9: 全量回归 + 文档同步

**Files:**
- Modify: `README.md`
- 可能 Modify: `config/setting_example.toml`（确认 Task 1 已含新键）

- [ ] **Step 1: 跑全量测试**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 全部 PASS（含既有 4 个测试文件 + 3 个新文件）

- [ ] **Step 2: 更新 `README.md`**

在「持久化登录 profile / token 管理」相关章节补充（执行者搜索 `persistent` / `token` / `插件` 定位）：
- ST 自我续命机制：每次 AT 刷新捕获 labs.google 回发的轮换 ST（滚动续期 ~30 天），无需常驻浏览器；
- 每日保活任务 `st_keepalive_*` 配置说明；
- 智能粘贴注入：admin 后台粘贴 cookies.txt 全文（也支持 Cookie 头/JSON/裸 ST），目标网址 `https://labs.google/fx/tools/flow`；
- Chrome 扩展降级为「开新号时一次性使用」；
- `min_credits_to_select` 额度跳过、`st_alert_webhook_url` 告警、`st_browser_refresh_enabled=false`（多账号安全）说明；
- 注意事项：服务停机 > 30 天会导致 ST 自然过期需重新注入。

- [ ] **Step 3: Commit**

```bash
git add README.md config/setting_example.toml
git commit -m "docs: ST self-renewal, smart paste injection, credits-aware scheduling"
```

---

## Task 10: 端到端冒烟测试（真实账号，重启生产服务）

> 用户提供的 Ruby 账号 cookies.txt 仅用于本地测试，**严禁写入仓库**（放 `.wm_dev/ruby_cookies.txt`，测试后删除）。用户已授权重启生产服务器、当前无生产任务。

- [ ] **Step 1: 解析器对真实 cookies.txt 生效**

把用户给的 cookies.txt 存为 `.wm_dev/ruby_cookies.txt`，运行：

```bash
.venv/bin/python -c "from src.core.cookie_extractor import extract_session_token; raw=open('.wm_dev/ruby_cookies.txt',encoding='utf-8').read(); st=extract_session_token(raw); print('ST len=', len(st), 'head=', st[:16])"
```
Expected: 打印出 `ST len= ~1064`，head 以 `eyJ` 开头。

- [ ] **Step 2: 实测 st_to_at 捕获 rotated_st（真实打 Google）**

```bash
.venv/bin/python -c "
import asyncio
from src.core.cookie_extractor import extract_session_token
from src.services.flow_client import FlowClient
raw=open('.wm_dev/ruby_cookies.txt',encoding='utf-8').read()
st=extract_session_token(raw)
async def main():
    c=FlowClient(proxy_manager=None)
    r=await c.st_to_at(st)
    print('email=', r.get('user',{}).get('email'))
    print('has_rotated=', 'rotated_st' in r, 'rotated_len=', len(r.get('rotated_st','')))
asyncio.run(main())
"
```
Expected: 打印 ruby 邮箱、`has_rotated= True`。若返回 401（账号已被 Google 撤销），记录该结果——这本身验证了 ST_REVOKED 路径；与用户确认后改用在用的 Pro 账号（DB id=15）复测。

- [ ] **Step 3: 重启生产服务并确认启动日志**

```bash
# 按项目既有方式重启（systemd 或 docker-compose.headed.yml）。执行者先确认部署方式：
systemctl --user restart flow2api 2>/dev/null || docker compose -f docker-compose.headed.yml restart 2>/dev/null || echo "需确认重启方式"
```
启动日志应出现：`✓ ST keepalive task: started (every 24h)`。

- [ ] **Step 4: 通过 admin API 走粘贴注入全链路**

```bash
# 取 connection/admin 鉴权后（执行者按现有 admin 登录拿 token），POST raw：
curl -sS -X POST http://127.0.0.1:<port>/api/tokens \
  -H "Authorization: Bearer <admin_token>" -H "Content-Type: application/json" \
  --data @<(python -c "import json;print(json.dumps({'raw':open('.wm_dev/ruby_cookies.txt',encoding='utf-8').read(),'remark':'e2e-smoke'}))")
```
Expected: 返回 `success:true`，email=ruby291464@gmail.com。随后 `GET /api/tokens` 能看到该号；DB 中该号 `st` 经一次刷新后发生变化（轮换回写生效）。

- [ ] **Step 5: 真实生成不回归（用在用的 Pro 号 id=15）**

通过现有生成接口（如 `/v1/chat/completions` 或既有 smoke 脚本）发起一次最小图片/视频生成，确认成功，证明 ST 轮换回写未破坏提交链路。

- [ ] **Step 6: 清理临时凭证文件**

```bash
rm -f .wm_dev/ruby_cookies.txt
```

- [ ] **Step 7: 把测试结果汇总**（成功/失败、关键日志）记录到对话，供 review 阶段判断。

---

## Task 11: 代码 Review + 迭代修复

- [ ] **Step 1:** 用 `code-reviewer` subagent 审查本分支全部 diff（`git diff main...HEAD`），重点：rotated ST 回写的并发与锁正确性、`_make_request` 改动对热路径无副作用、cookie 解析器边界、前端 XSS/转义、配置默认值安全（`st_browser_refresh_enabled=false`）。
- [ ] **Step 2:** 对 review 提出的每条问题，用 `superpowers:receiving-code-review` 的标准判断（先核实再改），需要修的写测试→修复→重跑。
- [ ] **Step 3:** 重跑全量测试 `.venv/bin/python -m pytest tests/ -v` 全绿。
- [ ] **Step 4:** 迭代直到 review 与测试全部通过。

---

## Self-Review（计划编写者自查）

- **Spec 覆盖：** 组件1=Task3+4；组件2=Task4(`keepalive_rotate_st`)+Task5；组件3=Task2+6+7；组件4=Task4(ST_REVOKED+告警)+Task8(额度)+Task1(配置)。新发现的「浏览器 ST 刷新多账号写错号」风险=Task4 门控。文档=Task9。E2E=Task10。Review=Task11。✅ 全覆盖。
- **占位符扫描：** 无 TODO/TBD；每个代码步骤含完整代码或精确插入点；机械型步骤（前端/路由整合）给了完整片段并指明「先 Read 对齐现有模式」。
- **类型/签名一致：** `extract_session_token`、`resolve_st_from_request`、`_extract_rotated_st_from_set_cookie`、`capture_set_cookie`、`keepalive_rotate_st`、`min_credits_to_select`、`st_keepalive_*`、`st_browser_refresh_enabled`、`st_alert_webhook_url` 全程命名一致。
- **风险点提示：** Task4/Task8 的构造函数与锁结构需执行者先 Read 实际签名再落地（已在步骤中注明）。
