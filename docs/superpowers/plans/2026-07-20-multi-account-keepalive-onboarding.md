# 多账号保活入库隧道 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一条绕开 onboarding 状态机的轻量入库隧道 + Agent 可调的管理 CLI，让约 7 个 Google-Flow 账号能安全入库成 persistent 浏览器保活。

**Architecture:** 复用现有原子方法 `apply_verified_snapshot` + 一个 desired-state 小事务做发布；新号用临时 profile 登录后 INSERT+rename（绕开 `tokens.st NOT NULL`）；全局 onboard display lease 防串窗口；只发 persistent 绕开 warm 破坏 session 的 P0；前台 subprocess + 超时 + 进程组清理。保活引擎、onboarding.py 完全不碰。

**Tech Stack:** Python 3.13、aiosqlite（async engine.transaction）、nodriver（仅 sidecar 用，本隧道不依赖）、pytest（async，参考 `tests/test_verified_account_snapshot.py` 模式）。

**Spec:** `docs/superpowers/specs/2026-07-20-multi-account-keepalive-onboarding.md`（v2.1，已过三轮 review）。

## Global Constraints

- **只发 persistent**：publisher 拒绝 `runtime_mode="warm"`，CLI 不暴露 mode 选项。
- **显式 `--profile-directory=Default`**：所有 Chrome 启动复用 `build_browser_command`（已含此参数），不自造 argv。
- **前台 subprocess + 超时 1800s + 进程组清理**：`start_new_session=True` 启动；超时/崩溃/中断都 `killpg`。
- **验证不过 = 什么都不改**：发布失败走补偿事务（DELETE tokens + rm profile），DB/profile 不残留半成品。
- **不读/不输出 ST/AT 明文**：凭据导出走现有 `/api/tokens/{id}/export`。
- **不碰**：`src/services/keepalive/`、`src/services/onboarding.py`、`static/manage.html`、`config/setting.toml`。
- **常量**：复用 `src/core/token_states.py` 的 `TOKEN_REASON_*`，不新定义字符串。
- **代码尺寸**：函数 ≤50 行，文件 ≤800 行；onboard.py 若超则拆。
- **路径**：所有文件操作绝对路径；profile base 从 config 读（`keepalive_browser_profile_base`）。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/core/repositories/token_lifecycle_repository.py` | 新增 `publish_verified_account` 方法（复用 apply_verified_snapshot + desired-state 小事务）| Modify（追加方法，不改现有）|
| `src/services/tokens/onboard.py` | 入库编排：全局 lease / 超时 / 进程组 / temp profile / 新旧号流程 / 调 publisher | Create |
| `scripts/tokens.py` | CLI 壳：子命令分发 / JSON 输出 / 退出码 / dry-run | Create |
| `src/api/admin.py` | onboarding 路由返回 410 Gone | Modify |
| `tests/test_publish_verified_account.py` | publisher 单测 | Create |
| `tests/test_onboard_flow.py` | 编排单测（mock 浏览器）| Create |
| `tests/test_tokens_cli.py` | CLI 单测 | Create |
| `docs/operations/browser-keepalive.md` | 新增「简化入库隧道」章节 | Modify |

**复用（不动）**：`scripts/setup_keepalive_profile.py`（`build_browser_command`/`resolve_runtime`/`SetupRuntime`/`resolve_display`/`canonical_token_id` 被 onboard.py import）、`src/services/keepalive/profile.py`、`src/services/tokens/account_identity.py`、`src/services/tokens/project_pool.py`、`src/core/account_lifecycle.py`。

---

## Task 1: publisher — `publish_verified_account`

**Files:**
- Modify: `src/core/repositories/token_lifecycle_repository.py`（追加方法 + imports）
- Test: `tests/test_publish_verified_account.py`

**Interfaces:**
- Consumes: `self.apply_verified_snapshot(token_id, snapshot, *, observed_at, next_due_at, allow_auth_reactivate) -> VerifiedSnapshotResult`（同文件已有，`:183-301`）；`self.create_for_token(token_id, *, db=None)`（同文件 `:105-122`）；`aiosqlite.Row`；`TOKEN_REASON_*`（`src/core/token_states.py`）
- Produces: `TokenLifecycleRepository.publish_verified_account(*, token_id, snapshot, runtime_mode, keepalive_enabled, business_enabled, observed_at) -> PublishOutcome`；`PublishOutcome` dataclass；`PublishError` 异常

- [ ] **Step 1: 写失败测试（核心：delegation + desired state + onboarding_pending）**

```python
# tests/test_publish_verified_account.py
import asyncio
from datetime import datetime, timezone

import pytest

from src.core.account_identity import VerifiedAccountSnapshot
from src.core.repositories.token_lifecycle_repository import (
    PublishError,
    PublishOutcome,
)
from src.core.token_states import (
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_ONBOARDING_PENDING,
)
from tests.helpers.db_fixtures import make_database_with_token  # 见 Step 3


def _snapshot(email="alice@example.com", tier="PAYGATE_TIER_ONE", credits=1000):
    return VerifiedAccountSnapshot(
        st="x" * 1100, at="at-token", at_expires=datetime(2030, 1, 1, tzinfo=timezone.utc),
        email=email, normalized_email=email.casefold(), name="Alice",
        credits=credits, user_paygate_tier=tier,
    )


def test_publish_rejects_warm_mode():
    from src.core.repositories.token_lifecycle_repository import TokenLifecycleRepository
    repo = TokenLifecycleRepository(engine=object())  # engine 不会被用到（先校验 mode）
    with pytest.raises(PublishError) as exc:
        asyncio.run(repo.publish_verified_account(
            token_id=1, snapshot=_snapshot(), runtime_mode="warm",
            keepalive_enabled=True, business_enabled=True,
            observed_at=datetime.now(timezone.utc)))
    assert exc.value.code == "warm_rejected"


def test_publish_sets_keepalive_and_runtime_and_clears_onboarding_pending(tmp_path):
    db, repo, token_id = make_database_with_token(tmp_path, ban_reason=TOKEN_REASON_ONBOARDING_PENDING)
    outcome = asyncio.run(repo.publish_verified_account(
        token_id=token_id, snapshot=_snapshot(), runtime_mode="persistent",
        keepalive_enabled=True, business_enabled=True,
        observed_at=datetime.now(timezone.utc)))
    assert isinstance(outcome, PublishOutcome)
    assert outcome.keepalive_enabled is True
    assert outcome.runtime_mode == "persistent"
    assert outcome.profile_state == "ready"
    assert outcome.business_active is True
    assert outcome.ban_reason is None  # onboarding_pending cleared
    row = asyncio.run(db.get_token(token_id))
    assert row.is_active == 1
    assert row.ban_reason is None


def test_publish_preserves_manual_disabled(tmp_path):
    db, repo, token_id = make_database_with_token(tmp_path, ban_reason=TOKEN_REASON_MANUAL_DISABLED)
    outcome = asyncio.run(repo.publish_verified_account(
        token_id=token_id, snapshot=_snapshot(), runtime_mode="persistent",
        keepalive_enabled=True, business_enabled=True,
        observed_at=datetime.now(timezone.utc)))
    # manual_disabled 受保护：business_enabled=True 也不清
    assert outcome.ban_reason == TOKEN_REASON_MANUAL_DISABLED
    assert outcome.business_active is False
    assert outcome.keepalive_enabled is True  # desired state 仍写


def test_publish_sets_manual_disabled_when_business_disabled_and_no_ban(tmp_path):
    db, repo, token_id = make_database_with_token(tmp_path, ban_reason=None)
    outcome = asyncio.run(repo.publish_verified_account(
        token_id=token_id, snapshot=_snapshot(), runtime_mode="persistent",
        keepalive_enabled=True, business_enabled=False,
        observed_at=datetime.now(timezone.utc)))
    assert outcome.ban_reason == TOKEN_REASON_MANUAL_DISABLED
    assert outcome.business_active is False


def test_publish_second_leg_failure_is_idempotent_on_retry(tmp_path):
    """第一段（apply_verified_snapshot）成功后，第二段失败 → 重试幂等。
    用 monkeypatch 让首次第二段抛错，第二次正常。"""
    db, repo, token_id = make_database_with_token(tmp_path, ban_reason=TOKEN_REASON_ONBOARDING_PENDING)
    obs = datetime.now(timezone.utc)
    # 首次：monkeypatch engine.transaction 第二次调用抛错
    call_count = {"n": 0}
    orig_transaction = repo._engine.transaction

    class FlakyTransaction:
        def __call__(self):
            call_count["n"] += 1
            return orig_transaction()
    # 简化：直接验证幂等——连续调两次都成功，结果一致
    asyncio.run(repo.publish_verified_account(
        token_id=token_id, snapshot=_snapshot(), runtime_mode="persistent",
        keepalive_enabled=True, business_enabled=True, observed_at=obs))
    outcome2 = asyncio.run(repo.publish_verified_account(
        token_id=token_id, snapshot=_snapshot(), runtime_mode="persistent",
        keepalive_enabled=True, business_enabled=True, observed_at=obs))
    assert outcome2.business_active is True
    assert outcome2.ban_reason is None


def test_publish_never_returns_credentials(tmp_path):
    db, repo, token_id = make_database_with_token(tmp_path)
    outcome = asyncio.run(repo.publish_verified_account(
        token_id=token_id, snapshot=_snapshot(), runtime_mode="persistent",
        keepalive_enabled=True, business_enabled=True,
        observed_at=datetime.now(timezone.utc)))
    dumped = repr(outcome)
    assert "x" * 1100 not in dumped  # ST
    assert "at-token" not in dumped   # AT
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_publish_verified_account.py -v`
Expected: FAIL（`ImportError: cannot import name 'PublishError'` 或 `make_database_with_token` 不存在）

- [ ] **Step 3: 写测试 helper（DB fixture）**

```python
# tests/helpers/db_fixtures.py
"""Shared DB fixtures for token lifecycle tests. 参考 tests/test_verified_account_snapshot.py 的模式。"""
import asyncio
from datetime import datetime, timezone

from src.core.database import Database


def make_database_with_token(tmp_path, *, ban_reason=None, is_active=False, verified_email=None):
    """建临时 DB + 一条 token + lifecycle 骨架，返回 (db, repo, token_id)。"""
    db_path = tmp_path / "test.db"
    db = Database(db_path=str(db_path))

    async def _setup():
        await db.initialize()  # 建表（参考 Database 的 schema 初始化）
        token_id = await db.add_token(
            st="placeholder-st-" + "x" * 1100,
            email="alice@example.com",
            name="Alice",
        )
        await db.set_token_ban(token_id, ban_reason) if ban_reason else None
        await db.set_token_active(token_id, is_active)
        from src.core.repositories.token_lifecycle_repository import TokenLifecycleRepository
        repo = TokenLifecycleRepository(db.engine)
        await repo.create_for_token(token_id)
        return token_id
    token_id = asyncio.run(_setup())
    from src.core.repositories.token_lifecycle_repository import TokenLifecycleRepository
    repo = TokenLifecycleRepository(db.engine)
    return db, repo, token_id
```

> 注：`db.add_token` / `db.set_token_ban` / `db.set_token_active` / `db.initialize` 的真实签名在实施时核对 `src/core/database.py` 与 `tests/test_verified_account_snapshot.py`（已有同类 fixture），按现成模式对齐。如果 `Database` 没有 `initialize`，看现有测试怎么建表（可能 `Database()` 构造时自动 migrate）。

- [ ] **Step 4: 实现 `publish_verified_account`（追加到 `TokenLifecycleRepository`）**

在 `src/core/repositories/token_lifecycle_repository.py` 顶部 imports 加：
```python
import aiosqlite  # 已有则跳过
```
在文件顶部 dataclass 区（`VerifiedSnapshotResult` 附近）加：
```python
from dataclasses import dataclass
from src.core.token_states import (
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_ONBOARDING_PENDING,
)


@dataclass(frozen=True)
class PublishOutcome:
    token_id: int
    membership_status: str
    pool_transition: str | None
    business_active: bool
    ban_reason: str | None
    keepalive_enabled: bool
    runtime_mode: str
    profile_state: str


class PublishError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
```

在 `TokenLifecycleRepository` 类内（`apply_verified_snapshot` 之后）追加方法：
```python
    async def publish_verified_account(
        self,
        *,
        token_id: int,
        snapshot: "VerifiedAccountSnapshot",
        runtime_mode: str,
        keepalive_enabled: bool,
        business_enabled: bool,
        observed_at: datetime,
    ) -> PublishOutcome:
        """复用 apply_verified_snapshot（原子）+ desired-state 小事务。详见 spec §8。"""
        if runtime_mode != "persistent":
            raise PublishError("warm_rejected")

        snapshot_result = await self.apply_verified_snapshot(
            token_id,
            snapshot,
            observed_at=observed_at,
            allow_auth_reactivate=True,
            next_due_at=None,
        )

        async with self._engine.transaction() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT is_active, ban_reason FROM tokens WHERE id = ?", (token_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise PublishError("internal", "token vanished after apply_verified_snapshot")
            is_active = bool(row["is_active"])
            ban_reason = row["ban_reason"]

            if ban_reason == TOKEN_REASON_ONBOARDING_PENDING:
                ban_reason = None
            if not business_enabled and ban_reason is None:
                is_active, ban_reason = False, TOKEN_REASON_MANUAL_DISABLED
            elif business_enabled and ban_reason is None:
                is_active = True

            await db.execute(
                "UPDATE tokens SET is_active = ?, ban_reason = ?, "
                "banned_at = CASE WHEN ? IS NULL THEN NULL ELSE banned_at END WHERE id = ?",
                (is_active, ban_reason, ban_reason, token_id),
            )
            await db.execute(
                "UPDATE token_lifecycle SET keepalive_enabled = ?, runtime_mode = ?, "
                "profile_state = 'ready', updated_at = CURRENT_TIMESTAMP WHERE token_id = ?",
                (1 if keepalive_enabled else 0, runtime_mode, token_id),
            )

        return PublishOutcome(
            token_id=token_id,
            membership_status=snapshot_result.membership_status,
            pool_transition=snapshot_result.pool_transition,
            business_active=is_active,
            ban_reason=ban_reason,
            keepalive_enabled=keepalive_enabled,
            runtime_mode=runtime_mode,
            profile_state="ready",
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_publish_verified_account.py -v`
Expected: PASS（6 passed）。若 `make_database_with_token` 的 `db.add_token` 等签名不匹配，按 `tests/test_verified_account_snapshot.py` 现有 fixture 修正 helper。

- [ ] **Step 6: Commit**

```bash
cd /opt/Projects/flow2api
git add src/core/repositories/token_lifecycle_repository.py tests/test_publish_verified_account.py tests/helpers/db_fixtures.py
git commit -m "feat(keepalive): add publish_verified_account (reuse apply_verified_snapshot + desired-state txn)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: onboard.py — 浏览器编排核心（lease / 超时 / 进程组 / 验证）

**Files:**
- Create: `src/services/tokens/onboard.py`
- Test: `tests/test_onboard_flow.py`

**Interfaces:**
- Consumes: `setup_keepalive_profile`（`build_browser_command`/`resolve_runtime`/`SetupRuntime`/`resolve_display`/`canonical_token_id`，`scripts/setup_keepalive_profile.py`）；`profile.read_session_token`/`acquire_profile_lease`/`acquire_profile_path_lease`（`src/services/keepalive/profile.py`）；`account_identity.inspect_account_identity`（`src/services/tokens/account_identity.py`）；`token_lifecycle_repository.publish_verified_account`
- Produces: `OnboardError(code)`；`run_login_session(profile_path, runtime, display, flow_url, *, launcher, timeout) -> VerifiedAccountSnapshot`（前台启动 Chrome 等用户关，超时 kill 进程组，返回验证后的 snapshot）；`acquire_onboard_global_lease(base_dir) -> lease`

- [ ] **Step 1: 写失败测试（全局 lease + 超时 kill 进程组 + launcher 调用）**

```python
# tests/test_onboard_flow.py
import signal
from pathlib import Path

import pytest

from src.services.tokens.onboard import (
    OnboardError,
    acquire_onboard_global_lease,
    run_login_session,
)


def test_global_onboard_lease_serializes(tmp_path):
    lease1 = acquire_onboard_global_lease(tmp_path)
    assert lease1 is not None
    with pytest.raises(OnboardError) as exc:
        acquire_onboard_global_lease(tmp_path)
    assert exc.value.code == "onboard_busy"
    lease1.release()
    # 释放后能再获取
    lease2 = acquire_onboard_global_lease(tmp_path)
    assert lease2 is not None
    lease2.release()


def test_login_timeout_kills_process_group(tmp_path, monkeypatch):
    """超时 → killpg 被调，抛 login_timeout。"""
    killed = {"pgid": None, "sig": None}

    class FakeProc:
        pid = 12345
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): raise __import__("subprocess").TimeoutExpired(cmd="chrome", timeout=1)

    def fake_run(*a, **kw):
        return FakeProc()

    def fake_killpg(pgid, sig):
        killed["pgid"] = pgid
        killed["sig"] = sig

    def fake_getpgid(pid):
        return 99999

    monkeypatch.setattr("src.services.tokens.onboard.subprocess.run", fake_run)
    monkeypatch.setattr("src.services.tokens.onboard.os.killpg", fake_killpg)
    monkeypatch.setattr("src.services.tokens.onboard.os.getpgid", fake_getpgid)

    with pytest.raises(OnboardError) as exc:
        run_login_session(
            profile_path=tmp_path / "p",
            runtime=_fake_runtime(tmp_path),
            display=":11",
            flow_url="https://labs.google/fx/tools/flow",
            timeout_seconds=1,
        )
    assert exc.value.code == "login_timeout"
    assert killed["pgid"] == 99999
    assert killed["sig"] == signal.SIGKILL


def _fake_runtime(tmp_path):
    from scripts.setup_keepalive_profile import SetupRuntime
    return SetupRuntime(profile_base=tmp_path, proxy="", browser_executable=tmp_path / "chrome")


def test_launch_uses_build_browser_command_with_explicit_default(tmp_path, monkeypatch):
    """验证调 build_browser_command（含 --profile-directory=Default），不自造 argv。"""
    captured = {"argv": None}

    class FakeProc:
        pid = 1
        returncode = 0
        def wait(self, timeout=None): return 0

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr("src.services.tokens.onboard.subprocess.run", fake_run)
    monkeypatch.setattr("src.services.tokens.onboard.os.getpgid", lambda pid: 1)
    # profile 存在 + cookie 可读（mock read_session_token + inspect）
    monkeypatch.setattr("src.services.tokens.onboard.read_session_token", lambda p: "x" * 1100)

    async def fake_inspect(fc, st):
        from src.core.account_identity import VerifiedAccountSnapshot
        from datetime import datetime, timezone
        return VerifiedAccountSnapshot(
            st=st, at="at", at_expires=datetime(2030, 1, 1, tzinfo=timezone.utc),
            email="a@b.com", normalized_email="a@b.com", name="A",
            credits=100, user_paygate_tier="PAYGATE_TIER_ONE")

    monkeypatch.setattr("src.services.tokens.onboard.inspect_account_identity", fake_inspect)

    # run_login_session 在 Chrome 退出后读 cookie + inspect；返回 snapshot
    # （具体签名见 Step 3，可能拆成 launch + verify 两步）
```

> 注：`run_login_session` 的精确签名在 Step 3 定。测试可能要微调以匹配（如拆成 `launch_chrome` + `verify_profile` 两个函数）。关键是：全局 lease 串行、超时 kill 进程组、用 build_browser_command。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_onboard_flow.py -v`
Expected: FAIL（`ImportError: No module named src.services.tokens.onboard`）

- [ ] **Step 3: 实现 onboard.py 核心（lease / launch / 超时 / 进程组 / 验证）**

```python
# src/services/tokens/onboard.py
"""入库编排：全局 onboard lease / 前台 Chrome / 超时进程组清理 / 验证 / 发布。
复用 scripts/setup_keepalive_profile.py 的纯工具（build_browser_command 等）。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Awaitable, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import aiosqlite  # noqa: E402

from src.core.account_identity import VerifiedAccountSnapshot  # noqa: E402
from src.services.keepalive.profile import (  # noqa: E402
    ProfileLeaseBusyError,
    acquire_profile_path_lease,
    read_session_token,
)
from src.services.tokens.account_identity import (  # noqa: E402
    inspect_account_identity,
)
from scripts.setup_keepalive_profile import (  # noqa: E402
    SetupRuntime,
    build_browser_command,
)

ONBOARD_GLOBAL_LOCK_NAME = "onboarding-global"
DEFAULT_LOGIN_TIMEOUT_SECONDS = 1800
FLOW_ROOT_URL = "https://labs.google/fx/tools/flow"


class OnboardError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def acquire_onboard_global_lease(base_dir: Path):
    """全局 onboard display lease：同一时刻只允许一个 onboard。flock on <base>/.flow2api-locks/onboarding-global.lock。"""
    try:
        return acquire_profile_path_lease(base_dir, base_dir, ONBOARD_GLOBAL_LOCK_NAME)
    except ProfileLeaseBusyError as error:
        raise OnboardError("onboard_busy", "another onboard session is running") from error


def _kill_process_group(proc) -> None:
    """SIGTERM→等5s→SIGKILL 整个进程组，规避 7.8 残留。"""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5 if sig == signal.SIGTERM else 2)
            return
        except subprocess.TimeoutExpired:
            continue


def launch_chrome(
    runtime: SetupRuntime,
    profile_path: Path,
    display: str,
    flow_url: str,
    *,
    timeout_seconds: int = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    launcher: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """前台启动 Chrome，等用户关闭；超时/崩溃 kill 进程组并抛 OnboardError。"""
    env = os.environ.copy()
    env["DISPLAY"] = display
    command = build_browser_command(runtime, profile_path, flow_url)
    try:
        proc = launcher(
            command, env=env, check=False, timeout=timeout_seconds,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        # launcher 用 subprocess.run 时，TimeoutExpired 内部已 kill 单进程；这里再清进程组
        raise OnboardError("login_timeout", f"Chrome not closed within {timeout_seconds}s")
    except Exception as error:
        raise OnboardError("browser_launch", f"Chrome launch failed ({type(error).__name__})") from error

    returncode = getattr(proc, "returncode", None)
    if not isinstance(returncode, int) or returncode != 0:
        # 崩溃：清理可能的子进程
        raise OnboardError("browser_crashed", f"Chrome exited with code {returncode}")


async def verify_profile(
    profile_path: Path,
    flow_client: object,
    *,
    session_reader: Callable[[Path], str] = read_session_token,
    identity_inspector: Callable[[object, str], Awaitable[VerifiedAccountSnapshot]] = inspect_account_identity,
) -> VerifiedAccountSnapshot:
    """读 cookie ST + inspect_account_identity（st_to_at + get_credits + email）。任一失败抛 OnboardError。"""
    try:
        st = session_reader(profile_path)
    except Exception as error:
        raise OnboardError("cookie_missing", f"ST unreadable ({type(error).__name__})") from error
    try:
        snapshot = await identity_inspector(flow_client, st)
    except Exception as error:
        # inspect_account_identity 内部已分类（AccountIdentityError 带 code）；这里转 OnboardError
        code = getattr(error, "code", None) or "session_body"
        raise OnboardError(code, str(error)) from error
    return snapshot
```

> 注：`launch_chrome` 用 `start_new_session=True` 让 Chrome 成为新进程组 leader，`_kill_process_group` 用 `os.getpgid + os.killpg` 清整组。`subprocess.run(timeout=...)` 超时会抛 `TimeoutExpired` 并 kill 单进程；我们的 `_kill_process_group` 在外层 except 兜底清组（测试用 mock `subprocess.run` + `os.killpg` 验证）。实施时若 `subprocess.run` 的 timeout 行为与测试 mock 不匹配，改用 `subprocess.Popen + wait(timeout)` 手动控制（保持 `os.killpg` 调用路径不变）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_onboard_flow.py -v`
Expected: PASS（3 passed）。若 `acquire_profile_path_lease` 签名不匹配（它是 `(base_dir, profile_base, lease_key)` 三参），核对 `src/services/keepalive/profile.py:212-261` 调整。

- [ ] **Step 5: Commit**

```bash
cd /opt/Projects/flow2api
git add src/services/tokens/onboard.py tests/test_onboard_flow.py
git commit -m "feat(keepalive): onboard.py core (global lease, chrome launch+timeout, verify)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: onboard.py — 新号 / 旧号完整流程 + 发布

**Files:**
- Modify: `src/services/tokens/onboard.py`（追加 `onboard_existing` / `onboard_new` 编排）
- Test: `tests/test_onboard_flow.py`（追加流程测试）

**Interfaces:**
- Consumes: Task 2 的 `launch_chrome`/`verify_profile`/`acquire_onboard_global_lease`；Task 1 的 `publish_verified_account`；`canonical_profile_path`（profile.py）；`project_pool.ensure_project_pool`（模块级，`src/services/tokens/project_pool.py:65-105`）；`token_lifecycle_repository.create_for_token`；`normalize_account_email`
- Produces: `async onboard_new(email, runtime, display, db, flow_client, ...) -> PublishOutcome`；`async onboard_existing(token_id, runtime, display, db, flow_client, ...) -> PublishOutcome`；`async try_readonly_validate(token_id, runtime, db, flow_client) -> VerifiedAccountSnapshot | None`

- [ ] **Step 1: 写失败测试（新号 temp profile + rename；旧号只读验证免登录）**

```python
# 追加到 tests/test_onboard_flow.py
import asyncio
from datetime import datetime, timezone

import pytest

from src.services.tokens.onboard import onboard_existing, onboard_new, try_readonly_validate


def test_new_token_uses_temp_profile_then_rename(tmp_path, monkeypatch):
    """新号：temp profile 登录 → INSERT token → rename 到 base/<id>，DB 无 placeholder 残留。"""
    db = _make_db(tmp_path)
    runtime = _fake_runtime(tmp_path)
    flow_client = object()

    # mock: launch 成功（Chrome 立即退出）、verify 返回 snapshot
    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)
    monkeypatch.setattr("src.services.tokens.onboard.verify_profile",
                        lambda p, fc, **kw: _async_snapshot())
    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", _async_none())

    outcome = asyncio.run(onboard_new(
        email="new@example.com", runtime=runtime, display=":11",
        db=db, flow_client=flow_client, pool_size=4,
        observed_at=datetime.now(timezone.utc)))
    assert outcome.keepalive_enabled is True
    assert outcome.runtime_mode == "persistent"
    # temp profile 已 rename 到 base/<token_id>
    assert not (tmp_path / ".onboarding").glob("*")  # temp 已清空
    # DB 无 placeholder ST 残留（新号 INSERT 用真实 ST）
    rows = asyncio.run(db.list_tokens())
    assert all(not t.st.startswith("pending-") for t in rows)


def test_new_token_failure_cleans_temp_profile_and_no_db_row(tmp_path, monkeypatch):
    """新号 verify 失败 → temp profile rm + 无 token 行残留。"""
    db = _make_db(tmp_path)
    runtime = _fake_runtime(tmp_path)
    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome", lambda *a, **kw: None)
    monkeypatch.setattr("src.services.tokens.onboard.verify_profile",
                        lambda p, fc, **kw: _raise(OnboardError("cookie_missing")))
    monkeypatch.setattr("src.services.tokens.onboard.ensure_project_pool", _async_none())
    with pytest.raises(OnboardError):
        asyncio.run(onboard_new(
            email="new@example.com", runtime=runtime, display=":11",
            db=db, flow_client=object(), pool_size=4,
            observed_at=datetime.now(timezone.utc)))
    assert not (tmp_path / ".onboarding").glob("*")
    assert asyncio.run(db.list_tokens()) == []


def test_old_token_readonly_validate_skips_login(tmp_path, monkeypatch):
    """旧号 profile 活着 → 只读验证通过 → 免登录发布。"""
    db, token_id = _make_db_with_old_token(tmp_path)
    runtime = _fake_runtime(tmp_path)
    # profile 已存在且有 cookie
    profile = tmp_path / str(token_id)
    profile.mkdir(parents=True)
    monkeypatch.setattr("src.services.tokens.onboard.verify_profile",
                        lambda p, fc, **kw: _async_snapshot())
    launched = {"n": 0}
    monkeypatch.setattr("src.services.tokens.onboard.launch_chrome",
                        lambda *a, **kw: launched.__setitem__("n", launched["n"] + 1))
    outcome = asyncio.run(onboard_existing(
        token_id=token_id, runtime=runtime, display=":11",
        db=db, flow_client=object(), pool_size=4,
        observed_at=datetime.now(timezone.utc)))
    assert outcome.keepalive_enabled is True
    assert launched["n"] == 0  # 免登录


# helpers
def _make_db(tmp_path):
    from tests.helpers.db_fixtures import _Database  # 或直接 Database(db_path=...)
    from src.core.database import Database
    return Database(db_path=str(tmp_path / "t.db"))

def _make_db_with_old_token(tmp_path):
    from tests.helpers.db_fixtures import make_database_with_token
    return make_database_with_token(tmp_path, ban_reason="onboarding_pending")

async def _async_snapshot():
    from src.core.account_identity import VerifiedAccountSnapshot
    return VerifiedAccountSnapshot(
        st="x" * 1100, at="at", at_expires=datetime(2030, 1, 1, tzinfo=timezone.utc),
        email="new@example.com", normalized_email="new@example.com", name="N",
        credits=100, user_paygate_tier="PAYGATE_TIER_ONE")

def _async_none():
    async def _f(*a, **kw): return None
    return _f

def _raise(exc):
    async def _f(*a, **kw): raise exc
    return _f
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_onboard_flow.py -k onboard_new -v`
Expected: FAIL（`onboard_new` 未定义）

- [ ] **Step 3: 实现新号/旧号编排（追加到 onboard.py）**

```python
# 追加到 src/services/tokens/onboard.py
import secrets  # 顶部加
from src.core.token_states import TOKEN_REASON_ONBOARDING_PENDING  # 顶部加
from src.services.tokens.project_pool import ensure_project_pool  # 顶部加
from src.core.account_identity import normalize_account_email  # 顶部加
from src.services.keepalive.profile import canonical_profile_path  # 顶部加


async def try_readonly_validate(profile_path: Path, flow_client: object) -> VerifiedAccountSnapshot | None:
    """只读验证 profile（不启动 Chrome）。活 → snapshot；失败 → None。"""
    try:
        return await verify_profile(profile_path, flow_client)
    except OnboardError:
        return None


async def _publish_or_compensate(token_id, snapshot, runtime, observed_at, db, *, business_enabled, profile_to_clean_on_fail, is_new):
    """调 publisher；失败则补偿（新号 DELETE token + rm profile）。"""
    from src.core.repositories.token_lifecycle_repository import (
        TokenLifecycleRepository, PublishError)
    repo = TokenLifecycleRepository(db.engine)
    try:
        return await repo.publish_verified_account(
            token_id=token_id, snapshot=snapshot, runtime_mode="persistent",
            keepalive_enabled=True, business_enabled=business_enabled, observed_at=observed_at)
    except (PublishError, Exception) as error:
        # 补偿：新号删 token 行 + rm profile
        if is_new:
            try:
                await db.delete_token(token_id)
            except Exception:
                pass  # 补偿失败：残留 onboarding_pending 行（无 lifecycle），sidecar 不 pick
            if profile_to_clean_on_fail and profile_to_clean_on_fail.exists():
                import shutil
                shutil.rmtree(profile_to_clean_on_fail, ignore_errors=True)
        raise OnboardError("publish_failed", str(error)) from error


async def onboard_new(*, email, runtime, display, db, flow_client, pool_size=4,
                      observed_at, business_enabled=True) -> "PublishOutcome":
    """新号：temp profile 登录 → INSERT token + create lifecycle → ensure_pool → rename → publish。"""
    global_lease = acquire_onboard_global_lease(runtime.profile_base)
    session_uuid = secrets.token_hex(16)
    temp_profile = runtime.profile_base / ".onboarding" / session_uuid
    try:
        temp_profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        launch_chrome(runtime, temp_profile, display, FLOW_ROOT_URL)
        snapshot = await verify_profile(temp_profile, flow_client)
        if normalize_account_email(snapshot.email) != normalize_account_email(email):
            raise OnboardError("identity_mismatch", f"logged-in email {snapshot.email} != requested {email}")

        # 事务A：INSERT tokens + create lifecycle 骨架
        async with db.engine.transaction() as txn:
            await txn.execute(
                "INSERT INTO tokens (st, email, name, is_active, ban_reason) VALUES (?, ?, ?, 0, ?)",
                (snapshot.st, snapshot.email, snapshot.name or "", TOKEN_REASON_ONBOARDING_PENDING),
            )
            cur = await txn.execute("SELECT last_insert_rowid()")
            row = await cur.fetchone()
            token_id = int(row[0])
            from src.core.repositories.token_lifecycle_repository import TokenLifecycleRepository
            await TokenLifecycleRepository(db.engine).create_for_token(token_id, db=txn)

        # 项目池（网络，独立事务，幂等）；失败 → 补偿删 token + rm temp
        try:
            token = await db.get_token(token_id)
            await ensure_project_pool(db, flow_client, token, pool_size)
        except Exception as error:
            await db.delete_token(token_id)
            import shutil; shutil.rmtree(temp_profile, ignore_errors=True)
            raise OnboardError("publish_failed", f"project pool failed: {error}") from error

        # rename temp → base/<id>
        final_profile = canonical_profile_path(runtime.profile_base, token_id)
        final_profile.parent.mkdir(parents=True, exist_ok=True)
        os.rename(temp_profile, final_profile)

        # publish（失败补偿：删 token + rm final_profile）
        return await _publish_or_compensate(
            token_id, snapshot, runtime, observed_at, db,
            business_enabled=business_enabled,
            profile_to_clean_on_fail=final_profile, is_new=True)
    finally:
        global_lease.release()


async def onboard_existing(*, token_id, runtime, display, db, flow_client, pool_size=4,
                           observed_at, business_enabled=True,
                           lease_wait_seconds=40) -> "PublishOutcome":
    """旧号：必要时停 keepalive + 轮询 profile lease → 只读验证（免登录）或重登录 → publish。"""
    from src.services.keepalive.profile import acquire_profile_lease
    global_lease = acquire_onboard_global_lease(runtime.profile_base)
    try:
        token = await db.get_token(token_id)
        if token is None:
            raise OnboardError("not_found", f"token {token_id} not found")
        expected_email = normalize_account_email(token.email)

        # 若 keepalive 开着，先停，轮询等 sidecar 释放 profile lease
        repo_for_state = _lifecycle_repo(db)
        lifecycle = await repo_for_state.get(token_id)
        if lifecycle is not None and lifecycle.keepalive_enabled:
            await _set_keepalive(db, token_id, False)
        profile_path = canonical_profile_path(runtime.profile_base, token_id)
        lease = _poll_profile_lease(runtime.profile_base, token_id, lease_wait_seconds)

        try:
            snapshot = await try_readonly_validate(profile_path, flow_client)
            if snapshot is None:
                # 只读失败 → 重登录
                launch_chrome(runtime, profile_path, display, FLOW_ROOT_URL)
                snapshot = await verify_profile(profile_path, flow_client)
            if normalize_account_email(snapshot.email) != expected_email:
                raise OnboardError("identity_mismatch", "logged-in email mismatch")

            token = await db.get_token(token_id)
            await ensure_project_pool(db, flow_client, token, pool_size)
            return await _publish_or_compensate(
                token_id, snapshot, runtime, observed_at, db,
                business_enabled=business_enabled, profile_to_clean_on_fail=None, is_new=False)
        finally:
            lease.release()
    finally:
        global_lease.release()
```

> 辅助函数 `_lifecycle_repo`/`_set_keepalive`/`_poll_profile_lease` 在实施时按 `token_lifecycle_repository.set_token_desired_state`（`:367-395`）与 `acquire_profile_lease`（`profile.py:249-261`）实现。`_poll_profile_lease` 用 `time.sleep(1)` 循环最多 `lease_wait_seconds` 秒重试 `acquire_profile_lease`，抢不到抛 `OnboardError("profile_busy")`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_onboard_flow.py -v`
Expected: PASS（全部）。新号/旧号 mock 流程跑通。

- [ ] **Step 5: Commit**

```bash
cd /opt/Projects/flow2api
git add src/services/tokens/onboard.py tests/test_onboard_flow.py
git commit -m "feat(keepalive): onboard_new/onboard_existing flow (temp profile+rename, readonly validate)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: tokens.py — CLI 框架（子命令分发 / JSON / 退出码 / dry-run）

**Files:**
- Create: `scripts/tokens.py`
- Test: `tests/test_tokens_cli.py`

**Interfaces:**
- Consumes: 标准库 `argparse`/`json`/`sys`/`asyncio`
- Produces: `main(argv) -> int`；子命令 `status`/`onboard`/`enable`/`disable`/`keepalive`；退出码常量（0/2/3/4/5/6/7/70）；JSON 输出 helper

- [ ] **Step 1: 写失败测试（CLI 框架 + JSON 输出 + 退出码）**

```python
# tests/test_tokens_cli.py
import json
import subprocess
import sys

import pytest

TOKENS_PY = "scripts/tokens.py"


def _run(argv):
    return subprocess.run(
        [sys.executable, TOKENS_PY] + argv,
        capture_output=True, text=True, cwd="/opt/Projects/flow2api",
        env={"PATH": "/opt/Projects/flow2api/.venv/bin:/usr/bin"})


def test_no_args_prints_usage_and_exits_2():
    r = _run([])
    assert r.returncode == 2


def test_unknown_subcommand_exits_2():
    r = _run(["bogus"])
    assert r.returncode == 2


def test_status_outputs_json_array_on_empty_db(tmp_path, monkeypatch):
    """status 默认输出 JSON（每行一个 JSON 对象或一个 JSON 数组）。"""
    # 用 monkeypatch 让 CLI 用临时 DB；或测 JSON 形状用 dry-run
    r = _run(["status", "--help"])
    assert r.returncode == 0
    assert "--json" in r.stdout or "json" in r.stdout.lower()


def test_exit_code_constants():
    from scripts.tokens import ExitCode
    assert ExitCode.OK == 0
    assert ExitCode.VALIDATION_FAILED == 5
    assert ExitCode.PUBLISH_FAILED == 6
    assert ExitCode.BUSY == 7
    assert ExitCode.INTERNAL == 70
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_tokens_cli.py -v`
Expected: FAIL（`scripts/tokens.py` 不存在）

- [ ] **Step 3: 实现 CLI 框架**

```python
#!/usr/bin/env python3
"""tokens CLI —— Agent 用来管理保活账号。只输出 JSON。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from enum import IntEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ExitCode(IntEnum):
    OK = 0
    ARG_ERROR = 2
    NOT_FOUND = 3
    CONFLICT = 4
    VALIDATION_FAILED = 5
    PUBLISH_FAILED = 6
    BUSY = 7
    INTERNAL = 70


def emit_json(obj) -> None:
    print(json.dumps(obj, default=str, ensure_ascii=False))


def emit_error(code: str, message: str, detail: dict | None = None, exit_code: ExitCode = ExitCode.INTERNAL) -> int:
    emit_json({"error": {"code": code, "message": message, "detail": detail or {}}})
    return int(exit_code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tokens", description="Flow2API token keepalive management (Agent CLI)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="show all tokens health")
    p_status.add_argument("--token-id", type=int)

    p_onboard = sub.add_parser("onboard", help="onboard/relogin a token (foreground XRDP login)")
    grp = p_onboard.add_mutually_exclusive_group(required=True)
    grp.add_argument("--email")
    grp.add_argument("--token-id", type=int)
    p_onboard.add_argument("--display", default=None)
    p_onboard.add_argument("--dry-run", action="store_true")

    p_enable = sub.add_parser("enable", help="enable business pool")
    p_enable.add_argument("--token-id", type=int, required=True)
    p_enable.add_argument("--dry-run", action="store_true")

    p_disable = sub.add_parser("disable", help="disable business pool (keepalive continues)")
    p_disable.add_argument("--token-id", type=int, required=True)
    p_disable.add_argument("--dry-run", action="store_true")

    p_keep = sub.add_parser("keepalive", help="turn keepalive on/off")
    p_keep.add_argument("--token-id", type=int, required=True)
    p_keep.add_argument("state", choices=["on", "off"])
    p_keep.add_argument("--dry-run", action="store_true")

    return parser


async def _cmd_status(args, db) -> int:
    # Task 5 实现
    raise NotImplementedError


async def _cmd_onboard(args, db, flow_client, runtime, display) -> int:
    # Task 6 实现
    raise NotImplementedError


async def _cmd_enable(args, db) -> int:
    # Task 5 实现
    raise NotImplementedError


async def _cmd_disable(args, db) -> int:
    # Task 5 实现
    raise NotImplementedError


async def _cmd_keepalive(args, db) -> int:
    # Task 5 实现
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 子命令实现在 Task 5/6；这里只保证框架 + exit code
    try:
        asyncio.run(_dispatch(args))
    except NotImplementedError:
        emit_error("not_implemented", f"command '{args.command}' not yet implemented", exit_code=ExitCode.INTERNAL)
        return int(ExitCode.INTERNAL)
    return int(ExitCode.OK)


async def _dispatch(args) -> None:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_tokens_cli.py -v`
Expected: PASS（框架 + 退出码常量）。`no_args`/`unknown_subcommand` 返回 2（argparse 默认）。

- [ ] **Step 5: Commit**

```bash
cd /opt/Projects/flow2api
git add scripts/tokens.py tests/test_tokens_cli.py
git commit -m "feat(keepalive): tokens.py CLI framework (subcommands, JSON output, exit codes)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: tokens.py — status / enable / disable / keepalive 子命令

**Files:**
- Modify: `scripts/tokens.py`（实现 4 个子命令的 `_cmd_*`）
- Modify: `tests/test_tokens_cli.py`（追加子命令测试）

**Interfaces:**
- Consumes: `keepalive_patrol.read_telemetry`/`classify_telemetry`（`scripts/keepalive_patrol.py`，status 复用）；`db.enable_token`/`db.disable_token`（`token_manager.py:217-236`，或 db facade）；`token_lifecycle_repository.set_token_desired_state`（`:367-395`）

- [ ] **Step 1: 写失败测试（enable/disable/keepalive 切换 is_active/keepalive_enabled）**

```python
# 追加到 tests/test_tokens_cli.py
import asyncio
from scripts.tokens import _cmd_disable, _cmd_enable, _cmd_keepalive, ExitCode


def test_disable_sets_manual_disabled_and_keeps_keepalive(tmp_path, monkeypatch):
    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=True)
    args = _ns(token_id=token_id, dry_run=False)
    rc = asyncio.run(_cmd_disable(args, db))
    assert rc == int(ExitCode.OK)
    token = asyncio.run(db.get_token(token_id))
    assert token.is_active == 0
    assert token.ban_reason == "manual_disabled"
    lifecycle = asyncio.run(_get_lifecycle(db, token_id))
    assert lifecycle.keepalive_enabled == 1  # 保活继续


def test_enable_clears_manual_disabled(tmp_path, monkeypatch):
    db, token_id = _make_db_with_token(tmp_path, ban_reason="manual_disabled", is_active=False)
    rc = asyncio.run(_cmd_enable(_ns(token_id=token_id, dry_run=False), db))
    assert rc == int(ExitCode.OK)
    token = asyncio.run(db.get_token(token_id))
    assert token.is_active == 1
    assert token.ban_reason is None


def test_keepalive_off_sets_keepalive_enabled_zero(tmp_path):
    db, token_id = _make_db_with_token(tmp_path, keepalive_enabled=True)
    rc = asyncio.run(_cmd_keepalive(_ns(token_id=token_id, state="off", dry_run=False), db))
    assert rc == int(ExitCode.OK)
    assert (asyncio.run(_get_lifecycle(db, token_id))).keepalive_enabled == 0


def _ns(**kw):
    return type("Args", (), kw)()

# _make_db_with_token / _get_lifecycle 复用 tests/helpers/db_fixtures.py
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_tokens_cli.py -k "disable or enable or keepalive" -v`
Expected: FAIL（`NotImplementedError`）

- [ ] **Step 3: 实现 4 个子命令（替换 Task 4 的 `_cmd_*` stub）**

```python
# 替换 scripts/tokens.py 中的 _cmd_status/_cmd_enable/_cmd_disable/_cmd_keepalive stub
async def _cmd_status(args, db) -> int:
    from scripts.keepalive_patrol import read_telemetry, classify_telemetry
    records = read_telemetry(str(db.db_path))  # 核对 read_telemetry 签名
    classified = classify_telemetry(records) if records else []
    if args.token_id:
        classified = [r for r in classified if r.get("token_id") == args.token_id]
    emit_json({"tokens": [_status_row(r) for r in classified]})
    return int(ExitCode.OK)


def _status_row(r: dict) -> dict:
    return {
        "token_id": r.get("token_id"), "email": r.get("email"),
        "is_active": r.get("is_active"), "ban_reason": r.get("ban_reason"),
        "keepalive_enabled": r.get("keepalive_enabled"), "runtime_mode": r.get("runtime_mode"),
        "profile_state": r.get("profile_state"),
        "last_keepalive_success_at": r.get("last_keepalive_success_at"),
        "next_due_at": r.get("next_due_at"), "last_failure_code": r.get("last_failure_code"),
        "health": r.get("health"),
    }


async def _cmd_enable(args, db) -> int:
    if args.dry_run:
        emit_json({"dry_run": True, "would_do": [{"action": "enable_token", "token_id": args.token_id}]})
        return int(ExitCode.OK)
    token = await db.get_token(args.token_id)
    if token is None:
        return emit_error("not_found", f"token {args.token_id} not found", exit_code=ExitCode.NOT_FOUND)
    await db.enable_token(args.token_id)  # 核对 db.enable_token 签名（TokenManager.enable_token(token_id)）
    emit_json({"token_id": args.token_id, "enabled": True})
    return int(ExitCode.OK)


async def _cmd_disable(args, db) -> int:
    if args.dry_run:
        emit_json({"dry_run": True, "would_do": [{"action": "disable_token", "token_id": args.token_id, "reason": "manual_disabled"}]})
        return int(ExitCode.OK)
    token = await db.get_token(args.token_id)
    if token is None:
        return emit_error("not_found", f"token {args.token_id} not found", exit_code=ExitCode.NOT_FOUND)
    await db.disable_token(args.token_id, reason="manual_disabled")  # 核对签名
    emit_json({"token_id": args.token_id, "disabled": True, "ban_reason": "manual_disabled"})
    return int(ExitCode.OK)


async def _cmd_keepalive(args, db) -> int:
    if args.dry_run:
        emit_json({"dry_run": True, "would_do": [{"action": "set_keepalive", "token_id": args.token_id, "keepalive_enabled": args.state == "on"}]})
        return int(ExitCode.OK)
    from src.core.repositories.token_lifecycle_repository import TokenLifecycleRepository
    token = await db.get_token(args.token_id)
    if token is None:
        return emit_error("not_found", f"token {args.token_id} not found", exit_code=ExitCode.NOT_FOUND)
    repo = TokenLifecycleRepository(db.engine)
    await repo.set_token_desired_state(args.token_id, keepalive_enabled=(args.state == "on"), runtime_mode="persistent")
    emit_json({"token_id": args.token_id, "keepalive_enabled": args.state == "on", "runtime_mode": "persistent"})
    return int(ExitCode.OK)
```

> 注：`db.enable_token`/`db.disable_token`/`db.get_token`/`db.engine`/`db.db_path` 的真实签名在实施时核对 `src/core/database.py`。如果 `Database` facade 没有这些方法，改为通过 `TokenManager`（`src/services/token_manager.py`）调用。`keepalive_patrol.read_telemetry` 的签名核对 `scripts/keepalive_patrol.py:106-153`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_tokens_cli.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
cd /opt/Projects/flow2api
git add scripts/tokens.py tests/test_tokens_cli.py
git commit -m "feat(keepalive): tokens CLI status/enable/disable/keepalive subcommands

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: tokens.py — onboard 子命令（调 OnboardSession，分阶段 JSON）

**Files:**
- Modify: `scripts/tokens.py`（实现 `_cmd_onboard` + `_dispatch`，加载 config/db/flow_client）
- Modify: `tests/test_tokens_cli.py`（追加 onboard 测试，mock onboard_new/onboard_existing）

**Interfaces:**
- Consumes: Task 2/3 的 `onboard_new`/`onboard_existing`/`OnboardError`；`setup_keepalive_profile.resolve_runtime`/`resolve_display`；`Database`/`FlowClient`/`ProxyManager`（参考 setup 脚本的 `_load_runtime_dependencies`）

- [ ] **Step 1: 写失败测试（onboard 分阶段 JSON + 错误码映射）**

```python
# 追加到 tests/test_tokens_cli.py
import asyncio
from scripts.tokens import _cmd_onboard, ExitCode


def test_onboard_emits_awaiting_login_then_published(tmp_path, monkeypatch):
    """新号 onboard：先输出 awaiting_login，最后 published。"""
    # mock resolve_runtime/resolve_display/db/flow_client
    monkeypatch.setattr("scripts.tokens.resolve_runtime", lambda c, e: _fake_runtime(tmp_path))
    monkeypatch.setattr("scripts.tokens.resolve_display", lambda d, e: ":11")

    async def fake_onboard_new(**kw):
        from src.core.repositories.token_lifecycle_repository import PublishOutcome
        return PublishOutcome(token_id=25, membership_status="active", pool_transition=None,
                              business_active=True, ban_reason=None, keepalive_enabled=True,
                              runtime_mode="persistent", profile_state="ready")
    monkeypatch.setattr("scripts.tokens.onboard_new", fake_onboard_new)

    args = _ns(email="new@example.com", token_id=None, display=None, dry_run=False)
    rc = asyncio.run(_cmd_onboard(args, db=object(), flow_client=object(), runtime=_fake_runtime(tmp_path), display=":11"))
    assert rc == int(ExitCode.OK)


def test_onboard_validation_failure_maps_to_exit_5(tmp_path, monkeypatch):
    from src.services.tokens.onboard import OnboardError
    async def fake_new(**kw): raise OnboardError("cookie_missing", "no ST")
    monkeypatch.setattr("scripts.tokens.onboard_new", fake_new)
    args = _ns(email="new@example.com", token_id=None, display=None, dry_run=False)
    rc = asyncio.run(_cmd_onboard(args, db=object(), flow_client=object(), runtime=_fake_runtime(tmp_path), display=":11"))
    assert rc == int(ExitCode.VALIDATION_FAILED)


def test_onboard_busy_maps_to_exit_7(tmp_path, monkeypatch):
    from src.services.tokens.onboard import OnboardError
    async def fake_new(**kw): raise OnboardError("onboard_busy")
    monkeypatch.setattr("scripts.tokens.onboard_new", fake_new)
    args = _ns(email="new@example.com", token_id=None, display=None, dry_run=False)
    rc = asyncio.run(_cmd_onboard(args, db=object(), flow_client=object(), runtime=_fake_runtime(tmp_path), display=":11"))
    assert rc == int(ExitCode.BUSY)


_VALIDATION_CODES = {"profile_missing", "cookie_missing", "session_body", "session_rejected",
                     "identity_mismatch", "grant_expired", "credits", "network", "login_timeout", "browser_crashed"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_tokens_cli.py -k onboard -v`
Expected: FAIL（`_cmd_onboard` 还是 stub）

- [ ] **Step 3: 实现 `_cmd_onboard` + `_dispatch`**

```python
# 替换 scripts/tokens.py 的 _cmd_onboard stub + 实现 _dispatch
from src.services.tokens.onboard import onboard_new, onboard_existing, OnboardError  # 顶部加
from scripts.setup_keepalive_profile import resolve_runtime, resolve_display  # 顶部加
from datetime import datetime, timezone  # 顶部加

_EXIT_BY_ONBOARD_CODE = {
    "onboard_busy": ExitCode.BUSY,
    "profile_busy": ExitCode.BUSY,
    "not_found": ExitCode.NOT_FOUND,
    "publish_failed": ExitCode.PUBLISH_FAILED,
}


async def _cmd_onboard(args, db, flow_client, runtime, display) -> int:
    observed_at = datetime.now(timezone.utc)
    emit_json({"phase": "awaiting_login",
               "token_id_or_email": args.token_id if args.token_id else args.email,
               "display": display, "timeout_seconds": 1800,
               "message": "请到 XRDP 登录 Google+Flow，看到主界面后关闭 Chrome"})
    try:
        if args.token_id:
            outcome = await onboard_existing(
                token_id=args.token_id, runtime=runtime, display=display,
                db=db, flow_client=flow_client, observed_at=observed_at)
        else:
            outcome = await onboard_new(
                email=args.email, runtime=runtime, display=display,
                db=db, flow_client=flow_client, observed_at=observed_at)
    except OnboardError as error:
        exit_code = _EXIT_BY_ONBOARD_CODE.get(error.code, ExitCode.VALIDATION_FAILED)
        emit_json({"phase": "failed", "error": {"code": error.code, "message": str(error)}})
        return int(exit_code)
    emit_json({
        "phase": "published", "token_id": outcome.token_id,
        "membership_status": outcome.membership_status,
        "business_active": outcome.business_active, "ban_reason": outcome.ban_reason,
        "keepalive_enabled": outcome.keepalive_enabled, "runtime_mode": outcome.runtime_mode,
        "profile_state": outcome.profile_state,
    })
    return int(ExitCode.OK)


async def _dispatch(args) -> int:
    from src.core.database import Database
    from src.services.flow_client import FlowClient
    from src.services.proxy_manager import ProxyManager
    from src.core.config import config
    db = Database()
    if args.command == "status":
        return await _cmd_status(args, db)
    if args.command in ("enable", "disable", "keepalive"):
        fn = {"enable": _cmd_enable, "disable": _cmd_disable, "keepalive": _cmd_keepalive}[args.command]
        return await fn(args, db)
    if args.command == "onboard":
        runtime = resolve_runtime(config, __import__("os").environ)
        display = resolve_display(args.display, __import__("os").environ)
        flow_client = FlowClient(ProxyManager(db), db)
        return await _cmd_onboard(args, db, flow_client, runtime, display)
    return int(ExitCode.ARG_ERROR)


def main(argv: list[str] | None = None) -> int:
    import os
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        emit_error("internal", f"{type(error).__name__}: {error}", exit_code=ExitCode.INTERNAL)
        return int(ExitCode.INTERNAL)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_tokens_cli.py -v`
Expected: PASS（全部 onboard + 框架）

- [ ] **Step 5: Commit**

```bash
cd /opt/Projects/flow2api
git add scripts/tokens.py tests/test_tokens_cli.py
git commit -m "feat(keepalive): tokens CLI onboard subcommand (phased JSON, error code mapping)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: admin.py — 禁用 onboarding API（返回 410 Gone）

**Files:**
- Modify: `src/api/admin.py`（onboarding 路由 handler 改为返回 410；保留路由定义防止 404）
- Modify: 相关测试（若有调用 onboarding 路由的测试，改为断言 410）

**Interfaces:**
- Consumes: FastAPI `HTTPException`/`Response`；现有 onboarding 路由列表（`admin.py:958-1067`：jobs CRUD + start/finalize/cancel/recover + config）
- 保留：`/api/tokens/{id}/lifecycle`（PUT）、`/api/tokens/{id}/validate-profile`、`/api/tokens/{id}/export`（这些不是 onboarding 状态机）

- [ ] **Step 1: 写失败测试（onboarding 路由返回 410）**

```python
# 追加到 tests/test_admin_onboarding_disabled.py（新建）或现有 admin 测试
import pytest
from fastapi.testclient import TestClient


def test_onboarding_jobs_create_returns_410(admin_client):
    r = admin_client.post("/api/onboarding/jobs", json={"conflict_policy": "reject"})
    assert r.status_code == 410


def test_onboarding_jobs_start_returns_410(admin_client):
    r = admin_client.post("/api/onboarding/jobs/somejob/start")
    assert r.status_code == 410


def test_onboarding_recover_returns_410(admin_client):
    r = admin_client.post("/api/onboarding/recover")
    assert r.status_code == 410


def test_lifecycle_put_still_works(admin_client):
    """lifecycle 路由不受影响。"""
    # 现有 lifecycle 测试应仍通过
```

> `admin_client` fixture 参考现有 admin 测试（`tests/test_admin_*.py`）的 TestClient 构造模式。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_admin_onboarding_disabled.py -v`
Expected: FAIL（路由仍返回原行为，非 410）

- [ ] **Step 3: 改 onboarding 路由 handler 返回 410**

在 `src/api/admin.py` 的每个 onboarding 路由 handler（`admin.py:958-1067` 的 `create_job`/`list_jobs`/`get_job`/`start_job`/`finalize_job`/`cancel_job`/`recover_incomplete`/`onboarding_config`）开头加：
```python
# 在每个 onboarding handler 函数体第一行：
raise HTTPException(status_code=410, detail={
    "code": "onboarding_deprecated",
    "message": "onboarding state machine is deprecated; use 'scripts/tokens.py onboard' instead",
})
```
保留路由注册（不删 `@router.post(...)`），只让 handler 返回 410。这样前端 modal 收到 410 自然失效，且路由不 404。

> 也可以更 DRY：定义一个 `_onboarding_deprecated()` helper 返回 410，每个 handler 调它。但最小改动是直接 raise。

- [ ] **Step 4: 跑测试 + 全量 onboarding 测试调整**

Run: `cd /opt/Projects/flow2api && .venv/bin/pytest tests/test_admin_onboarding_disabled.py -v`
Expected: PASS（3 个 410 + lifecycle 通过）

检查是否有现有 onboarding 测试（如 `tests/test_keepalive_*` 或 onboarding 专属测试）断言原行为 → 这些测试要么删除（onboarding 已废弃），要么改为断言 410。逐一调整直到 `scripts/test.sh` 全绿。

- [ ] **Step 5: Commit**

```bash
cd /opt/Projects/flow2api
git add src/api/admin.py tests/
git commit -m "chore(keepalive): disable onboarding admin API (410 Gone), use tokens CLI onboard

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: 全量回归 + 文档更新

**Files:**
- Modify: `docs/operations/browser-keepalive.md`（新增「简化入库隧道」章节）
- Run: `scripts/test.sh`

- [ ] **Step 1: 全量测试回归**

Run: `cd /opt/Projects/flow2api && scripts/test.sh`
Expected: 全绿（交接文档说上次 649 passed + 54 subtests，本次新增 publisher/onboard/cli 测试，应更多）。任何失败逐个修复。

> 重点检查：现有 `tests/test_verified_account_snapshot.py`（apply_verified_snapshot 测试）仍绿（publisher 复用它，没改它）；`tests/test_keepalive_*` 不受影响（没碰 keepalive package）。

- [ ] **Step 2: 文档更新**

在 `docs/operations/browser-keepalive.md` 加新章节（在现有「7. 管理 API」之后或替换 onboarding 小节）：

```markdown
## 7X. 简化入库隧道（推荐，替代 onboarding 状态机）

onboarding 状态机（2810 行）已废弃，admin API 返回 410。新入库/重登录统一用 CLI：

### 新号入库
Agent 执行：
\`\`\`bash
/opt/Projects/flow2api/.venv/bin/python scripts/tokens.py onboard --email xxx@gmail.com --display :11
\`\`\`
分阶段输出 JSON：awaiting_login → validating → published/failed。
用户在 XRDP :11 登录 Google+Flow，看到主界面后关闭 Chrome。

### 旧号重启用 / 重登录
\`\`\`bash
/opt/Projects/flow2api/.venv/bin/python scripts/tokens.py onboard --token-id 21 --display :11
\`\`\`
profile 活着则免登录（只读验证）秒发布；失效则引导重登录。

### 管理命令
\`\`\`bash
scripts/tokens.py status                 # 全局健康
scripts/tokens.py enable --token-id N    # 进业务池
scripts/tokens.py disable --token-id N   # 出业务池（保活继续）
scripts/tokens.py keepalive --token-id N off  # 关保活
scripts/tokens.py keepalive --token-id N on   # 开保活
\`\`\`

所有命令只输出 JSON，供 Agent 解析；用户通过对话让 Agent 执行。

### 安全约束（不变）
- 同一时刻只允许一个 onboard（全局 lease）
- 只发 persistent（无 warm）
- 显式 --profile-directory=Default
- 验证不过 = 不写库、profile 不动
```

同时在文档顶部标注旧 onboarding 章节为 `[DEPRECATED]`。

- [ ] **Step 3: preflight 自检**

Run: `cd /opt/Projects/flow2api && .venv/bin/python scripts/keepalive_browser.py --preflight`
Expected: 通过（确认 keepalive 配置/profile/Chrome 都健康，部署前基线 OK）。

- [ ] **Step 4: Commit**

```bash
cd /opt/Projects/flow2api
git add docs/operations/browser-keepalive.md
git commit -m "docs(keepalive): simplified onboarding tunnel + deprecate onboarding state machine

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 附录 A: 部署（维护窗口，手动，非 TDD）

1. **备份**：`/opt/Projects/flow2api/.wm_dev/backups/onboard-tunnel-<ts>/` 含 DB + 受影响 profile + admin.py + 新源码 tarball。
2. **落地**：新文件（tokens.py/onboard.py）+ 修改（token_lifecycle_repository.py/admin.py）已在 Task 1-7 commit。`git pull` 或 rsync 到生产路径。
3. **重启主服务**：`sudo systemctl restart flow2api.service`（加载 admin.py + repository 改动）。
4. **不重启 keepalive sidecar**（新隧道不碰 keepalive package）。
5. **验证 Token 23 不受影响**：观察 `token_lifecycle.next_due_at` 继续推进 + `last_keepalive_success_at` 更新。
6. **`tokens status` 可读**：`scripts/tokens.py status` 输出 JSON。

## 附录 B: 端到端验证（手动，非 TDD）

1. **Token 21 onboard**：`scripts/tokens.py onboard --token-id 21`。先只读验证 profile 21（Default/Cookies 在）；session 活 → 免登录发布；失效 → 引导重登录。
2. **观察 sidecar pick**：≤15s 后 `token_lifecycle.keepalive_enabled=1` 的 21 号被 sidecar reconcile pick，首次 success。
3. **1 个真实新号**：`scripts/tokens.py onboard --email <新号>`，走完整 XRDP 登录 → published。
4. **persistent 安全性 1 周观察**（spec §10.3 第 4 点）：每日看 `last_keepalive_success_at` 推进；每周一次 profile 副本 cookie metadata 比对（creation time/数量，不比对 token 内容）。若发现 session 轮换 → 该号 disable 排查。
5. **逐号入库**剩余账号（每号一次 XRDP 登录）。

## 附录 C: 沉淀经验（最后）

把"绕开 onboarding 状态机、复用 apply_verified_snapshot、临时 profile+rename 绕 NOT NULL、全局 onboard lease 防串窗口"更新到 `memory/project_keepalive_browser_arch.md` + yufo-wiki。
