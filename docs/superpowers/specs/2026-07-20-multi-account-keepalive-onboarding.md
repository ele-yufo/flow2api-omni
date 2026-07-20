# 多账号保活入库隧道 — 设计文档 (v2)

> 日期：2026-07-20
> 仓库：`/opt/Projects/flow2api`
> 目标：让用户能把新/旧 Google-Flow 账号安全入库成 persistent 浏览器保活，绕开已知的 onboarding 状态机灾难。
> 相关：`docs/browser-account-lifecycle-handoff-2026-07-20.md`（交接文档，记录之前失败）、`KEEPALIVE_TASK.md`（原始根因）。
> v2 变更：经一轮严格设计评审修订 5 个 BLOCKER（publisher 原子性、新号 NOT NULL、XRDP 串窗口、复用 setup_profile、facade 方法真实性）+ 5 个 HIGH。

---

## 1. 背景与目标

### 1.1 真实需求
用户有约 5 个 Google/Flow 账号要入库保活（新旧都有），加上现有 Token 23/21，总计约 7 个 persistent 保活号。管理界面 = 与 Agent 对话（用户不直接敲 CLI），Agent 后端调 CLI/API 完成操作。

### 1.2 根因（已实证，来自 KEEPALIVE_TASK.md + memory）
- ST 本身能续期 30 天，不是问题。
- 真正会死的是 ST 背后的 **Google OAuth 授权（AT），约 1 小时一换**。
- 纯 HTTP 服务端流程刷不动 Google 授权，必须靠**浏览器 profile 走正常 Flow 流程刷新**。
- Token 23 (Ruby) 的 persistent 浏览器 sidecar 已验证可行（生产持续 success）。

### 1.3 之前灾难的根源（交接文档第 7 节）
1. 过早平台化（2810 行 onboarding 状态机 + admin UI），没先交付"第二账号可用"。
2. 固定读 `Default/Cookies`，而有效 session 在 `Profile 1`，导致用户反复登录。
3. warm one-shot 破坏了 Token 21 的有效 session（导航触发 OAuth cookie 轮换）。
4. finalize 不停 Chrome 进程树，留僵尸。
5. 操作了错误的 XRDP Chrome 窗口（多窗口并存时选错）。

### 1.4 本次目标
交付一条**最小、可回滚、绕开所有已知坑**的入库隧道 + 一组 Agent 可调的管理命令。**保活引擎不动、onboarding 状态机绕开不删、不做新 UI。**

### 1.5 成功标准
- 7 个账号全部 `persistent` 保活稳定运行（每 20 分钟 success，AT 持续刷新）。
- 入库一个新号 ≤ 一次 XRDP 登录（profile 活着的旧号免登录）。
- 任何入库验证失败 → 不写库、profile 不动、Agent 拿到清晰错误码。
- Token 23 全程不受影响。
- 全量测试通过。

---

## 2. 现状基线（2026-07-20 已确证）

| 项 | 状态 |
|---|---|
| `flow2api.service` / `flow2api-keepalive.service` / `xvfb@10.service` | 全 active |
| DB integrity | ok |
| Token 23 (Ruby) | persistent, keepalive_enabled=1, 每 20 分钟 success |
| Token 21 | persistent, keepalive_enabled=**0**, 有过一次 success（与交接文档不符，统一走新路径重验证）|
| 18/19/22/24/26 | unprovisioned, keepalive_enabled=0；18/24/26 profile 空；19/22 有 Default/Cookies |
| 服务器资源 | 30G 内存 / 15G 可用 / 16 核；Token 23 一个 persistent 号 ≈ 887 MB |
| profile 归一 | 全部 `Default`，无 `Profile 1` 漂移（当前不阻塞）|
| 现有可复用编排 | `scripts/setup_keepalive_profile.py:setup_profile()` 已实现 acquire lease→build_browser_command→前台 subprocess→email 校验→释放 lease |

### 2.1 现有 repository 架构约束（评审确认，决定 publisher 设计）
- `TokenRepository.update_token` 用 autocommit 连接（`token_repository.py:197-210`），每调用立即提交。
- `TokenLifecycleRepository.apply_verified_snapshot` / `set_desired_state` 各自开 `engine.transaction()`（`token_lifecycle_repository.py:197, 382`），不接外部连接。
- `engine.transaction()` 本身就是 `BEGIN IMMEDIATE`（`src/shared/db/engine.py:45-61`）。
- 结论：**无法用外层事务把现有子方法包成原子**。publisher 必须是一个新方法，在单个 `engine.transaction()` 内直接执行所有 SQL，不调自开事务的子方法。

---

## 3. 设计原则

1. **不碰能用的**：保活引擎（keepalive package 核心）Token 23 证明可用，不动。
2. **绕开会坏的**：onboarding 状态机是灾难源，保留代码但完全不调用，其 admin API 禁用。
3. **只发 persistent**：永不发 warm，绕开 warm 破坏 session 的 P0。
4. **从源头堵 profile 漂移**：启动 Chrome 显式 `--profile-directory=Default`（直接复用 `build_browser_command`，不自造 argv）。
5. **验证不过 = 什么都不改**：所有发布走单一 DB 事务，验证全过后才提交；项目池（网络）在事务外前置完成且幂等。
6. **前台等用户关 Chrome + 超时**：不用 pidfd 杀主 PID；加 30 分钟超时，超时/崩溃 kill 整个进程组。
7. **全局 onboard 串行**：同一时刻只允许一个 onboard 在 XRDP display 上跑，防串窗口（7.3）。
8. **CLI 是 Agent 工具**：只输出 JSON、`--dry-run`、清晰错误码；用户通过 Agent 间接操作。
9. **复用现有编排**：`setup_keepalive_profile.setup_profile` 已实现浏览器+验证编排，提取为库函数，不重写。

---

## 4. 架构与边界

```
                         ┌─────────────────────────────────────┐
                         │  保活引擎（不动）                     │
                         │  keepalive sidecar + persistent      │
                         │  runner —— Token 23 在跑              │
                         └─────────────────────────────────────┘
                                       ▲ reconcile 每 15s 扫
                                       │ token_lifecycle.keepalive_enabled=1
                                       │
┌──────────────────┐    发布     ┌─────┴──────────────────────────┐
│ 入库隧道(新增)   │ ──────────► │  tokens / token_lifecycle 表    │
│ scripts/tokens.py│  单事务     └─────────────────────────────────┘
│  onboard 子命令  │
└──────────────────┘
        │ 调用
        ▼
┌────────────────────────────────────────────────────────────┐
│ src/services/tokens/onboard.py (新增, 编排层)               │
│  复用 setup_keepalive_profile 提取出的核心:                 │
│   build_browser_command / acquire_profile_lease /           │
│   read_session_token / inspect_account_identity             │
│  + 新增: 全局 onboard display lease / 超时 /                │
│         临时 profile→rename(新号) / 旧号前置停 keepalive    │
│  → 调 publisher.publish_verified_account                    │
└────────────────────────────────────────────────────┬───────┘
                                                     │
        ┌────────────────────────────────────────────▼───────┐
        │ TokenLifecycleRepository.publish_verified_account   │
        │ (新增方法, 单 engine.transaction() 内直接写 SQL)    │
        │  UPDATE tokens(st/at/email/tier/credits/pool) +     │
        │  迟滞纯函数 transition_account_lifecycle +          │
        │  resolve_pool_state 纯函数 +                        │
        │  UPDATE token_lifecycle(membership/desired/telemetry)│
        │  项目池(网络)由调用方前置完成                         │
        └─────────────────────────────────────────────────────┘

        ✗ onboarding.py(2810行) —— 保留代码, 入库隧道完全不调用
        ✗ onboarding admin API —— 禁用(返回 410 Gone)
```

### 新增文件
- `scripts/tokens.py` — 统一 CLI（子命令：status / onboard / enable / disable / keepalive）
- `src/services/tokens/onboard.py` — 入库编排层（复用 setup_profile 提取的核心 + 新增 lease/超时/rename）
- `tests/test_publish_verified_account.py` — publisher 原子方法单测
- `tests/test_tokens_cli.py` — CLI 子命令单测
- `tests/test_onboard_flow.py` — 入库编排单测（mock 浏览器）

### 修改文件
- `src/core/repositories/token_lifecycle_repository.py` — 新增 `publish_verified_account` 原子方法 + 提取 `resolve_pool_state` 为纯函数
- `src/core/account_lifecycle.py` — `transition_account_lifecycle` 已是纯函数，直接复用（可能微调导出）
- `scripts/setup_keepalive_profile.py` — 核心逻辑提取到 `onboard.py` 后，本脚本改为薄 wrapper 调用库（保持"只验证不发布"语义，兼容运维文档 8.4）
- `src/api/admin.py` — onboarding 相关路由改为返回 410 Gone；lifecycle 路由保留
- `docs/operations/browser-keepalive.md` — 新增「简化入库隧道」章节，标注 onboarding deprecated

### 完全不碰
- `src/services/keepalive/`（保活引擎核心）
- `src/services/onboarding.py`（保留不删，仅绕开）
- `src/core/token_states.py`、迟滞常量
- `static/manage.html`、`manage-account-*.js`

---

## 5. 账号生命周期管理模型

### 5.1 两条独立状态线（复用现有，不改）

**业务池**（`tokens.is_active` + `ban_reason`）：
- `active`（is_active=1，ban_reason=NULL）— 路由会用
- `manual_disabled` — 人工停用
- `membership_expired` — 自动退役（连续两次 free）
- `429_rate_limit` / `consecutive_errors` / `ST_REVOKED` / `GRANT_EXPIRED` / `onboarding_pending`

**保活**（`token_lifecycle.keepalive_enabled` + `runtime_mode`）：
- `keepalive_enabled=1, runtime_mode=persistent` — 持续保活（本设计唯一模式）
- `keepalive_enabled=0` — 不保活

两条线独立：可"出业务池但继续保活"（养号），不可"在池但不保活"（AT 会死）。

### 5.2 自动化（已有逻辑，复用）

| 事件 | 系统自动做 |
|---|---|
| 保活刷新成功 | 续 AT、更新 credits/tier、清 `ST_REVOKED`/`GRANT_EXPIRED` |
| 连续两次 free | 退役 → 出业务池、降频保活 43200s |
| 连续两次 paid | 恢复 → 回业务池 |

### 5.3 用户（经 Agent）的手动动作

| 动作 | CLI 子命令 | 何时用 |
|---|---|---|
| 新号入库 | `tokens onboard --email X` | 新 Google 账号 |
| 旧号入库/重启用 | `tokens onboard --token-id N` | profile 在的旧号（先只读验证，活着免登录）|
| ST 失效重登录 | `tokens onboard --token-id N` | patrol 报 ST_REVOKED（同上）|
| 停用（出池养号）| `tokens disable --token-id N` | 不想接业务但保活 |
| 启用（回池）| `tokens enable --token-id N` | 重新接业务 |
| 关保活 | `tokens keepalive --token-id N off` | 维护/省内存 |
| 开保活 | `tokens keepalive --token-id N on` | 恢复保活 |
| 查全局健康 | `tokens status` | 日常（复用 patrol 逻辑）|

> v2 砍掉了 `retire`/`restore`（自动迟滞已够）、`--mode`（只 persistent）、`disable --reason`（固定 manual_disabled）、人类可读表格输出（只 JSON）。

---

## 6. 入库隧道流程

### 6.0 通用前置（所有 onboard）
1. **acquire 全局 onboard display lease**（`flock` on `<base>/.flow2api-locks/onboarding-global.lock`）。同一时刻只允许一个 onboard 进程，彻底防 7.3 串窗口。获取不到 → 报 `onboard_busy`，Agent 排队。
2. 校验 display 格式（`:N` 或 `:N.M`）。
3. 校验代理 URL（若有）无 embedded userinfo（复用现有 credential-free 校验）。

### 6.1 新号流程（`tokens onboard --email xxx@gmail.com`）

新号无 token_id，但 `tokens.st` 是 `NOT NULL UNIQUE`，不能先建空行。**用临时 profile 登录，验证拿到真实 ST 后再 INSERT + rename**：

```
1. 通用前置（全局 lease 等）
2. session_uuid = uuid4().hex
3. temp_profile = <base>/.onboarding/<session_uuid>；mkdir -m 0700
4. 启动 Chrome（build_browser_command, --user-data-dir=temp_profile,
              --profile-directory=Default, 打开 Flow URL）—— 前台 subprocess.run
5. 输出 awaiting_login JSON（含 display）→ Agent 引导用户登录
6. subprocess.run 阻塞至 Chrome 退出，超时 30 分钟（见 §6.5）
7. 等 cookie flush（settle_seconds）
8. read_session_token(temp_profile) + inspect_account_identity 验证
     任一步失败 → rm -rf temp_profile, 释放 lease, 报错码退出（§9）
9. 校验 verified_email == 输入 email（normalized），不匹配 → identity_mismatch
10. --dry-run → 输出 would_do，退出 0，不写库、不 rename
11. 事务A（async engine.transaction()）：INSERT tokens(st=真实ST, email, name,
        is_active=0, ban_reason='onboarding_pending') → 拿 token_id；
        同事务内 create_for_token(token_id, db=...) 建 lifecycle 骨架
        （apply_verified_snapshot 要求 lifecycle 行已存在，否则 KeyError；FK ON DELETE CASCADE 保证补偿 DELETE tokens 时级联清 lifecycle）
12. ensure_project_pool(token_id, pool_size=4) —— 网络，独立事务，幂等
13. os.rename(temp_profile, <base>/<token_id>) —— 同文件系统 atomic
14. publisher.publish_verified_account(token_id, snapshot,
        runtime_mode="persistent", keepalive_enabled=True,
        business_enabled=True, observed_at=now)
15. 释放全局 lease
16. 输出 published JSON → Agent 汇报
```

**补偿路径（v2.1 修订评审 HIGH-2，保证 §9.2 "DB 不写 / profile 不动"）**：
事务A 在步骤 11 已提交，后续步骤 12/13/14 任一失败必须补偿，使最终效果等价于"什么都没发生"：
- **步骤 12 或 13 失败**（temp_profile 仍在旧路径）：
  `DELETE FROM tokens WHERE id=token_id`（补偿事务）+ `rm -rf temp_profile` + 释放 lease + 报错码退出。
- **步骤 14 publish 失败**（profile 已 rename 到 `<base>/<id>`）：
  `DELETE FROM tokens WHERE id=token_id` + `rm -rf <base>/<token_id>` + 释放 lease + 报 `publish_failed`。
- **补偿 DELETE 自身失败**（极小概率，如 DB 临时不可用）：
  tokens 残留 `is_active=0 / ban_reason='onboarding_pending'` 行，但**无 lifecycle 行**（publisher 没跑成功）→ sidecar `WHERE keepalive_enabled=1` 不 pick（安全）。记 error 日志告警；下次同邮箱 onboard 命中 §6.1 步骤 3"查 DB 有无该 email → 有 → 用其 id"，复用该残留行重试。
- 全程补偿用 `try/except` 包裹步骤 12~14，`finally` 释放全局 lease。

### 6.2 旧号流程（`tokens onboard --token-id 19`）

```
1. 通用前置
2. 读 tokens.email 作为期望邮箱
3. 若 token_lifecycle.keepalive_enabled=1:
     set keepalive_enabled=0（让 sidecar 下次 reconcile 释放 runner + profile lease）
4. 轮询 acquire profile lease（<base>/<token_id>）：每 1s 重试，最多 40s
     （真实释放上限 = reconcile_interval 15s + shutdown_timeout 20s = 35s，留 5s 余量；证据 supervisor.py:558,595-598 + 129,166-171）
     超时仍抢不到 → profile_busy 报错（提示手动确认 sidecar runner 已停）
5. 【先只读验证，不启动 Chrome】
     read_session_token + inspect_account_identity（含真实 st_to_at + get_credits 网络调用）
     ✓ 通过 + email 匹配 → 跳到 9（免登录发布）       ← 19/22 可能走这条
     ✗ 失败 → 进入重登录（步骤 6~8）
6. 启动 Chrome（build_browser_command, --user-data-dir=<base>/<id>,
              --profile-directory=Default）—— 在原 profile 上重登录，不 archive 不覆盖
7~8. 等用户关 Chrome + 超时 + flush + 读验证（同新号 6~8）
9. 校验 email 匹配
10. --dry-run 检查
11. ensure_project_pool(token_id) —— 网络，幂等
12. publisher.publish_verified_account(...)
13. 释放 profile lease + 全局 lease
14. 输出结果
```

**关键**：旧号在原 profile 重登录，不用 onboarding 的 archive_and_replace / renameat2 / migration blocker 那套。

### 6.3 Chrome argv —— 直接复用 build_browser_command（v2.1 修订真实签名）
不自造 argv。复用 `scripts/setup_keepalive_profile.py` 提取到 `src/services/tokens/onboard.py` 的 `build_browser_command(runtime, profile_path, flow_url)`——**真实签名首参是 `SetupRuntime` NamedTuple**（含 `profile_base`/`proxy`/`browser_executable`，由 `resolve_runtime()` 从 config 构造），不是裸 proxy。它产出：
```
chrome --user-data-dir=<profile> --profile-directory=Default --no-first-run
       --no-default-browser-check --disable-sync --disable-background-mode
       [--proxy-server=...] <flow_url>
```
onboard.py 新增 `resolve_runtime_for_onboard()`：新号用临时 profile_path 覆盖 runtime 的 profile_base，且 `flow_url` 恒为 `https://labs.google/fx/tools/flow` 根路径（**不读 token.current_project_id**，避免跳项目内页触发 cookie 轮换风险，关联 warm 灾难机理）。旧号沿用 setup helper 现有 `_flow_url(token.current_project_id)` 行为。**不传 `--password-store=basic`**（setup helper 没有，保持一致）。

### 6.4 登录完成判定
不依赖自动判定。Agent 引导用户："看到 Flow 主界面后**关闭整个 Chrome 窗口**"。`subprocess.run` 在 Chrome 主进程退出时返回。

### 6.5 超时与崩溃恢复（应对评审 H3/H4）
- `subprocess.run(..., timeout=1800)`（30 分钟）。
- 超时 → `proc.terminate()` → 等 5s → `proc.kill()` → `os.killpg(os.getpgid(proc.pid), SIGKILL)`（**停整个进程组**，规避 7.8 残留）→ rm temp_profile（新号）/ 释放 lease → 报 `login_timeout`。
- Chrome 非零退出（崩溃）→ 同样 kill 进程组 + 清理 → 报 `browser_crashed`。
- Agent 进程被中断：subprocess 用 `start_new_session=True` 启动，onboard 捕获 `KeyboardInterrupt`/`SIGTERM` 时先 kill 子进程组再退出，不留孤儿。

---

## 7. CLI 命令组设计（Agent 工具）

统一入口 `scripts/tokens.py`，子命令架构。

### 7.1 通用约定
- **只输出 JSON**（默认即 JSON，无人类表格）。
- 退出码：0=成功；2=参数错误；3=未找到；4=状态冲突；5=验证失败；6=发布失败；7=lease/busy 冲突；70=内部错误。
- 所有写操作支持 `--dry-run`（预览不执行）。
- 不读/不输出 ST/AT 明文（凭据导出走现有 `/api/tokens/{id}/export`）。
- 错误输出：`{"error": {"code": "...", "message": "...", "detail": {...}}}` 到 stderr。

### 7.2 子命令清单

```bash
python scripts/tokens.py status [--token-id N]
  → 每号: id/email/is_active/ban_reason/keepalive_enabled/runtime_mode/
        profile_state/last_success/next_due/last_failure_code/健康判定

python scripts/tokens.py onboard --email X [--display :N] [--dry-run]
python scripts/tokens.py onboard --token-id N [--display :N] [--dry-run]

python scripts/tokens.py enable  --token-id N [--dry-run]
python scripts/tokens.py disable --token-id N [--dry-run]

python scripts/tokens.py keepalive --token-id N {on|off} [--dry-run]
```

### 7.3 onboard 分阶段 JSON 输出
onboard 是长流程，分阶段输出（每阶段一行 JSON 到 stdout）让 Agent 追踪：

```jsonc
// 阶段1：启动后
{"phase": "awaiting_login", "token_id_or_email": "xxx@gmail.com", "display": ":11",
 "timeout_seconds": 1800, "message": "请到 XRDP :11 登录 Google+Flow，看到主界面后关闭 Chrome"}

// 阶段2：Chrome 退出后验证
{"phase": "validating", ...}

// 阶段3a：成功
{"phase": "published", "token_id": 25, "email": "...", "tier": "PAYGATE_TIER_ONE",
 "credits": 1000, "at_expires": "...", "keepalive_enabled": true,
 "runtime_mode": "persistent"}

// 阶段3b：失败
{"phase": "failed", "error": {"code": "grant_expired", "message": "credits 返回 401，需重新授权"}}
```

进程在阶段1后阻塞等 Chrome（带超时）。Agent 读到 awaiting_login 后告知用户。

### 7.4 dry-run 输出示例

> **2026-07-20 更正**：本节最初的示例把 CLI JSON 输出字段写成了
> `business_enabled`；实现落地时统一为 `business_active`（`business_enabled`
> 只是 `publish_verified_account()` 的输入参数名，见 §8.2/§8.3，不是 CLI 面向
> Agent 的输出字段名 —— 一个概念在 CLI 输出侧只用一个 key，见 `scripts/tokens.py`
> `_status_row()` 的注释）。实现也把 dry-run 输出简化为单个 `would_do` 项，不
> 逐步列出 insert_token/rename_profile/ensure_project_pool/publish。以下为
> `scripts/tokens.py onboard --dry-run` 的真实输出：

```jsonc
{"dry_run": true, "would_do": [
  {"action": "onboard_new", "target": "xxx@gmail.com", "display": ":11",
   "runtime_mode": "persistent"}
]}
```

（`--token-id N` 重登录时 `action` 为 `"onboard_existing"`，`target` 为该
token id。发布成功后 `published` 阶段的真实输出字段名同样是
`business_active`，见 §6.1/§6.2 与 `scripts/tokens.py::_cmd_onboard`。）

---

## 8. 发布方法设计（`TokenLifecycleRepository.publish_verified_account`）

### 8.1 设计：复用 apply_verified_snapshot + desired-state 小事务（v2.1 简化）

评审任务1 确认：现有 `apply_verified_snapshot(token_id, snapshot, *, observed_at, allow_auth_reactivate, next_due_at)`（`token_lifecycle_repository.py:195-301`）**本身就是原子方法**——在单个 `engine.transaction()` 内完成 publisher 90% 的工作（identity 校验、ST collision、membership 迟滞、`_resolve_pool_state` 的 auth recovery + retired/restored、两表 UPDATE）。

v2 原"新增方法 + 内联 SQL + 自写 resolve_pool_state"是重复造轮子（且自写版本被发现顺序 bug）。**改为复用 apply_verified_snapshot**，publisher 只补 3 件它不做的事：`keepalive_enabled`/`runtime_mode`（desired state）、清 `onboarding_pending`、`business_enabled`。publisher 仍放在 `TokenLifecycleRepository`（与 apply_verified_snapshot 同类，共享 engine）。

### 8.2 接口

```python
# src/core/repositories/token_lifecycle_repository.py 新增

@dataclass(frozen=True)
class PublishOutcome:
    token_id: int
    membership_status: str          # 'active' | 'retired'（来自 apply_verified_snapshot）
    pool_transition: str | None     # 'retired' | 'restored' | None
    business_active: bool
    ban_reason: str | None
    keepalive_enabled: bool
    runtime_mode: str
    profile_state: str              # 'ready'

class PublishError(Exception):
    code: str   # 'warm_rejected' | 'internal'

async def publish_verified_account(
    self,
    *,
    token_id: int,
    snapshot: VerifiedAccountSnapshot,   # 来自 inspect_account_identity
    runtime_mode: str,                   # 必须为 "persistent"
    keepalive_enabled: bool,
    business_enabled: bool,
    observed_at: datetime,
) -> PublishOutcome:
    """
    复用 apply_verified_snapshot（原子）+ desired-state 小事务。
    前提：调用方已完成 INSERT tokens（新号）+ create_for_token + ensure_project_pool。
    本方法只写 tokens + token_lifecycle 两表，不触碰网络。
    """
```

### 8.3 实现（v2.1：复用 apply_verified_snapshot + desired-state 小事务）

> 接口真相（评审任务1 确认）：`engine.transaction()` 是 `@asynccontextmanager`，必须 `async with`；`db.execute()` 返回 awaitable cursor，需 `await cursor.fetchone()`；要先设 `db.row_factory = aiosqlite.Row`。BEGIN IMMEDIATE 跨进程独占，异常自动回滚、正常提交（`engine.py:54,57-61`）。

```python
async def publish_verified_account(self, *, token_id, snapshot, runtime_mode,
                                   keepalive_enabled, business_enabled,
                                   observed_at) -> PublishOutcome:
    if runtime_mode != "persistent":
        raise PublishError("warm_rejected")   # 防 P0

    # 前提：调用方已完成 INSERT tokens（新号）+ create_for_token + ensure_project_pool。
    # 1. 复用现有原子方法 apply_verified_snapshot —— 它在单个 engine.transaction() 内完成：
    #    identity 校验 / ST collision / membership 迟滞（transition_account_lifecycle）
    #    / _resolve_pool_state（auth recovery 清 ST_REVOKED/GRANT_EXPIRED + retired/restored；
    #      天然保护 manual_disabled/429/consecutive_errors，因为它只动这两类）
    #    / UPDATE tokens(st/at/email/name/credits/tier/is_active/ban_reason/banned_at)
    #    / UPDATE lifecycle(membership/profile_state='ready'/verified_email/
    #      last_keepalive_*/failure_count=0/retired_at/restored_at/next_due_at COALESCE)
    snapshot_result = await self.apply_verified_snapshot(
        token_id, snapshot,
        observed_at=observed_at,
        allow_auth_reactivate=True,   # 保活验证成功 = 清 ST_REVOKED/GRANT_EXPIRED
        next_due_at=None,             # 留 NULL，sidecar 首次 reconcile 用 evaluate_due 填
    )

    # 2. 小事务：补 apply_verified_snapshot 不做的 3 件事
    async with self._engine.transaction() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT is_active, ban_reason FROM tokens WHERE id = ?", (token_id,))
        row = await cur.fetchone()
        if row is None:
            raise PublishError("internal")
        is_active, ban_reason = bool(row["is_active"]), row["ban_reason"]

        # 2a. 清 onboarding_pending（新号 INSERT 标记；_resolve_pool_state 不处理这个 ban）
        if ban_reason == TOKEN_REASON_ONBOARDING_PENDING:
            ban_reason = None
        # 2b. business_enabled → is_active/manual_disabled（仅当无其他 ban 时）
        if not business_enabled and ban_reason is None:
            is_active, ban_reason = False, TOKEN_REASON_MANUAL_DISABLED
        elif business_enabled and ban_reason is None:
            is_active = True

        await db.execute(
            "UPDATE tokens SET is_active = ?, ban_reason = ?, "
            "banned_at = CASE WHEN ? IS NULL THEN NULL ELSE banned_at END WHERE id = ?",
            (is_active, ban_reason, ban_reason, token_id))
        # 2c. desired state（apply_verified_snapshot 不写 keepalive_enabled/runtime_mode）
        await db.execute(
            "UPDATE token_lifecycle SET keepalive_enabled = ?, runtime_mode = ?, "
            "profile_state = 'ready', updated_at = CURRENT_TIMESTAMP WHERE token_id = ?",
            (1 if keepalive_enabled else 0, runtime_mode, token_id))

    # 3. 组装 outcome（无 next_due_at——sidecar 填）
    return PublishOutcome(
        token_id=token_id,
        membership_status=snapshot_result.membership_status,
        pool_transition=snapshot_result.pool_transition,
        business_active=is_active, ban_reason=ban_reason,
        keepalive_enabled=keepalive_enabled, runtime_mode=runtime_mode,
        profile_state="ready",
    )
```

### 8.4 原子性与一致性（v2.1：复用方案，应对评审 B2/HIGH-1）

**不再自写 resolve_pool_state 纯函数**——v2 的自写版本被发现顺序 bug（先 membership 后 auth recovery，与真实 `_resolve_pool_state` 相反，会在"GRANT_EXPIRED + 同时退休"场景产生不同结果）。方案 Y 直接复用 apply_verified_snapshot 内置的、经测试的 `_resolve_pool_state`，消除偏离真实逻辑的风险。

两段事务：apply_verified_snapshot（自身原子）+ desired-state 小事务。
- **apply_verified_snapshot 内部异常** → 它自己回滚，publisher 抛错传播，第一段都没写完 = 等于什么都没写（满足 §9.2 "发布失败 DB 不写"）。
- **第一段成功、第二段失败**（极小概率，如 DB 临时不可用）：
  - tokens 已更新（真实 ST/AT/email/tier/credits + auth recovery/membership 后的 is_active/ban_reason）
  - lifecycle profile_state=ready / verified_email 已设
  - 但 keepalive_enabled 仍=默认 0（骨架）或旧值
  - **一致性安全**：sidecar `WHERE keepalive_enabled=1` 不 pick（不会拿半成品号去保活）；Agent 重试 `publish_verified_account` 幂等（apply_verified_snapshot 可重入，desired-state UPDATE 幂等）。
- 比内联 100 行 SQL 重复 apply_verified_snapshot 更 DRY，且不会偏离真实逻辑。

**受保护状态天然成立**：apply_verified_snapshot 的 `_resolve_pool_state` 只动 auth recovery（ST_REVOKED/GRANT_EXPIRED）+ membership（membership_expired）；manual_disabled/429/consecutive_errors 在这两步都不匹配 → is_active/ban_reason 保持原值。publisher 第二段也只在 `ban_reason is None` 时才写 manual_disabled，绝不覆盖已有受保护 ban。

### 8.5 边界
- `runtime_mode` 恒为 `"persistent"`；传 `"warm"` → `PublishError("warm_rejected")`。
- `next_due_at` 不由 publisher 写（NULL）；sidecar 首次 reconcile 时 `evaluate_due` 填。`PublishOutcome` 无此字段。
- 受保护 ban owner（manual_disabled/429/consecutive_errors）永不被覆盖（见 §8.4）。
- identity mismatch（verified_email 已绑定但不符）→ apply_verified_snapshot 抛 ValueError（`token_lifecycle_repository.py:218-219`），publisher 传播为发布失败，apply_verified_snapshot 事务自动回滚。
- ST collision（snapshot.st 已属于其他 token）→ apply_verified_snapshot 抛 ValueError（行 226），同上。
- **前提**：新号调用前必须 INSERT tokens + `create_for_token`，否则 apply_verified_snapshot 抛 KeyError（行 212）。
- 不读/不返回 ST/AT 明文。

---

## 9. 错误码与失败退路

### 9.1 错误码表

| code | 触发 | 退出码 | 含义 |
|---|---|---|---|
| `onboard_busy` | 全局 onboard lease 抢不到 | 7 | 已有 onboard 在跑，排队 |
| `profile_busy` | 旧号 profile lease 抢不到（sidecar 还没释放）| 7 | 等 reconcile 或手动停 keepalive |
| `profile_missing` | profile 目录不存在/无 Cookies | 5 | 先 setup profile |
| `cookie_missing` | ST 读不到/解密失败/< 100 字节 | 5 | 重登录 |
| `session_body` | `/auth/session` 无有效 JSON/AT | 5 | 重登录或查网络 |
| `session_rejected` | session 返回 401/403 | 5 | 重登录 |
| `identity_mismatch` | 登录邮箱 ≠ token 邮箱 | 5 | **登录错号，检查** |
| `grant_expired` | credits 401 | 5 | 重授权（重登录）|
| `credits` | credits 响应格式错 | 5 | 重试/查网络 |
| `network` | DNS/代理/timeout | 5 | 查代理 |
| `login_timeout` | 用户 30 分钟未关 Chrome | 5 | 重新 onboard |
| `browser_crashed` | Chrome 非零退出 | 5 | 重试，查 X display |
| `publish_failed` | 发布事务失败 | 6 | 看日志 |
| `warm_rejected` | 内部误传 warm | 70 | 代码 bug |

### 9.2 一致原则
**任何验证或发布失败 → DB 不写、lifecycle 不变、profile 文件不动（新号 rm temp_profile，旧号原 profile 不改）、sidecar 不会 pick。** 错误码进 JSON。

### 9.3 ST 长度两层语义（澄清评审 3.6）
- `read_session_token` 用 `MIN_SESSION_TOKEN_LENGTH=100`（cookie 读取宽松门槛）。
- `inspect_account_identity` 用 `MIN_ST_LEN=200`（验证严格门槛）。
- 两层防护，正常 ST 实测 ~1064，两层都通过。100~200 之间的异常短 ST 会被第二层拒绝——这是 feature。**不改任何常量。** onboard 路径同时过这两层才算验证通过。

---

## 10. 测试策略

### 10.1 单测（CI 必过）

**`tests/test_publish_verified_account.py`**（publisher，方案 Y 复用 apply_verified_snapshot）

> membership 迟滞 / auth recovery / identity 校验 / ST collision / retired_at/restored_at 由 apply_verified_snapshot 完成，其行为已被 `tests/test_verified_account_snapshot.py` 覆盖。publisher 测试聚焦 delegation + desired state + onboarding_pending + business_enabled + 两段事务语义，不重复 apply_verified_snapshot 的测试。

- `test_publish_rejects_warm_mode` — 传 warm → PublishError("warm_rejected")
- `test_publish_delegates_to_apply_verified_snapshot` — 验证以 allow_auth_reactivate=True 调用
- `test_publish_sets_keepalive_enabled_and_persistent_runtime_mode` — desired state 写入
- `test_publish_sets_profile_state_ready`
- `test_publish_clears_onboarding_pending_when_business_enabled` — 新号发布清 onboarding_pending + is_active=1
- `test_publish_sets_manual_disabled_when_business_disabled_and_no_ban`
- `test_publish_preserves_manual_disabled` — 不覆盖已有 manual_disabled
- `test_publish_preserves_429_and_consecutive_errors`
- `test_publish_propagates_apply_verified_snapshot_failure` — identity/collision 失败 → publisher 抛错，apply_verified_snapshot 事务已回滚
- `test_publish_second_leg_failure_is_idempotent_on_retry` — 第一段成功第二段失败后，重试幂等（**评审 B2**）
- `test_publish_never_returns_credentials` — PublishOutcome 无 ST/AT

**`tests/test_onboard_flow.py`**（编排，mock 浏览器/cookie/inspect）
- `test_old_token_readonly_validate_skips_login` — profile 活着 → 免登录
- `test_old_token_falls_back_to_login_on_stale_cookie`
- `test_new_token_uses_temp_profile_then_rename` — 临时 profile → INSERT → rename 到 base/<id>
- `test_new_token_does_not_leave_placeholder_st` — DB 无 pending-<uuid> 残留（评审 B3）
- `test_identity_mismatch_does_not_publish_and_cleans_temp_profile`
- `test_dry_run_writes_nothing_and_no_rename`
- `test_launch_calls_build_browser_command_with_explicit_default` — 复用 argv，含 `--profile-directory=Default`
- `test_global_onboard_lease_serializes_concurrent_runs` — 第二个 onboard 立即 onboard_busy（**评审 B4**）
- `test_old_token_disables_keepalive_before_acquiring_profile_lease` — 防 sidecar 撞
- `test_login_timeout_kills_process_group_and_releases_lease` — 30min 超时（**评审 H3/H4**）
- `test_browser_crash_kills_process_group`（**评审 2.2**）
- `test_sigterm_cleanup_kills_child_chrome` — onboard 被中断不留孤儿（**评审 3.1**）

**`tests/test_tokens_cli.py`**（CLI 壳）
- `test_status_json_shape`
- `test_enable_disable_toggle_is_active`
- `test_keepalive_on_off_sets_lifecycle`
- `test_disable_does_not_stop_keepalive`
- `test_exit_codes_match_error_codes`
- `test_dry_run_emits_would_do_not_writes`
- `test_no_human_output_only_json`

### 10.2 集成测试（临时 DB，不启动真 Chrome）
- 临时 SQLite（`Database(db_path=...)` 注入，参考现有 `test_db_token_crud`）+ 临时 profile 目录 fixture。
- mock `inspect_account_identity` 返回固定 snapshot，mock `build_browser_command` 不真启 Chrome。
- 跑新号 + 旧号完整 onboard 流程，验证 INSERT/rename/publish 全链路。

### 10.3 端到端（手动，维护窗口，非 CI）
1. **Token 21** 先走 `onboard --token-id 21`：只读验证，profile 活则免登录发布；否则引导重登录。
2. 1 个真实新号走完整 onboard。
3. 观察 sidecar ≤15s pick 起 runner + 首次 success。
4. **persistent 安全性观察**（评审 H5）：新号入库后，连续观察 1 周的 `last_keepalive_success_at` 推进 + 每周一次 profile 副本 cookie metadata 比对（只比对 creation time/数量，不比对 token 内容），确认 persistent 刷新不轮换 session。若发现轮换 → 回退该号到 disabled 并排查。
5. Token 23 全程不重启、流量不受影响。

### 10.4 回归
- `scripts/test.sh` 全量绿（交接文档说修改 profile.py 后没重跑全量；本次必须）。

---

## 11. 部署与回滚

### 11.1 部署（维护窗口）
1. 全量测试通过。
2. 备份：DB + 受影响 profile + admin.py + **新源码快照**（`.wm_dev/backups/onboard-tunnel-<ts>/`，评审 7.10 补）。
3. 落地新文件（tokens.py、onboard.py、token_lifecycle_repository.py 改动、setup_keepalive_profile.py 改动）。
4. 改 admin.py：onboarding 路由返回 410（保留 lifecycle 路由）。
5. 重启 `flow2api.service`（加载 admin.py + repository 改动）。**不重启 keepalive sidecar**（新隧道不碰 keepalive package；sidecar 用的 repository 读方法不受 publish 新增方法影响）。
6. 验证：Token 23 仍每 20 分钟 success；`tokens status` 可读。

### 11.2 回滚
1. 恢复 admin.py + token_lifecycle_repository.py + setup_keepalive_profile.py + 移除新脚本（从备份）。
2. 重启主服务。
3. DB 无需回滚（新方法只写标准字段，不改 schema）。
4. profile 无需回滚（新隧道不破坏 profile 结构；新号 temp_profile 失败已 rm，成功的已 rename 为标准 base/<id>，与 sidecar 期望一致）。

---

## 12. Token 21 处理
当前 DB 与交接文档矛盾。统一走新路径：
- `tokens onboard --token-id 21 --dry-run` 预览。
- `tokens onboard --token-id 21`：前置停 keepalive（当前已=0，跳过）→ 只读验证 profile 21；session 活 → 免登录发布；失效 → XRDP 重登录。
- 发布后 keepalive_enabled=1，sidecar 自动 pick。
- 不依赖历史状态。

---

## 13. 范围边界（明确不做）
- ❌ 不动 keepalive package 核心。
- ❌ 不删 onboarding.py（保留绕开；admin API 禁用）。
- ❌ 不实现 warm 模式（publisher 直接拒绝）。
- ❌ 不做新 UI；不动 manage.html。
- ❌ 不修 onboarding 的进程树停止 bug（绕开）。
- ❌ 不轮换 Discord webhook（用户在 Discord 侧做）。
- ❌ 不做批量入库（逐号，每号人工登录）。
- ❌ 不改 setting.toml。
- ❌ 不做 retire/restore CLI（用自动迟滞）。

---

## 14. 风险与未决

| 风险 | 应对 |
|---|---|
| warm 破坏 session 根因未定位 | 绕开（只 persistent）；persistent 同导航路径的潜在风险靠 §10.3 第 4 点的 1 周观察 + cookie metadata 比对验证 |
| `_cmdline_profile_ownership` 对 nodriver argv 的 7.7 false-negative | 新隧道用前台 subprocess 不依赖它；sidecar 的 persistent runner 用同一函数但 Token 23 长期正常，观察 |
| `_chrome_cookie_files` 测试覆盖不全 + 未部署 | 入库路径优先只读 Default（当前全归一）；本次补 §10 测试；部署前全量回归 |
| persistent 刷新是否真不轮换 session | 无代码级证据（warm 和 persistent 共享 `_navigate_flow`），靠 §10.3 第 4 点经验证伪；Token 23 长期 success 是正向证据 |
| 用户在 XRDP 误切 Profile 1 | argv 显式 Default + build_browser_command 的 `--disable-sync` + Agent 引导"勿切换 profile" |
| 7 个 persistent 号内存 ~6GB | 在预算内；逐号入库观察 |
| pending/孤儿清理 | onboard 失败必 rm temp_profile + kill 进程组；新号 INSERT 后失败会在事务回滚 DELETE |
| cookie flush 时序 | 复用 setup helper 的 settle 等待 |

---

## 15. 实施顺序（写实施计划时用）
1. **publisher（复用 apply_verified_snapshot + desired-state 小事务）+ 单测**（评审 B1/B2 核心）→ `test_publish_verified_account.py` 全绿（含 delegation / 第二段失败幂等测试）
2. **提取 setup_keepalive_profile 核心到 onboard.py + 编排逻辑 + 单测**（评审 B5）→ `test_onboard_flow.py` 全绿（含全局 lease/超时/进程组）
3. **tokens.py CLI + 单测** → `test_tokens_cli.py` 全绿
4. **admin.py 禁用 onboarding API** + 测试调整
5. **全量 `scripts/test.sh`** 回归
6. **文档**：browser-keepalive.md 加新章节
7. **维护窗口部署**：备份（含源码快照）→ 落地 → 重启主服务 → 验证 Token 23 不受影响
8. **端到端**：Token 21 onboard → 1 个新号 onboard → 观察 sidecar pick + 首次 success + 启动 1 周 persistent 观察
9. **逐号入库**剩余账号
10. **沉淀经验**到 yufo-wiki / memory

---

## 附：复用函数清单（v2 已核对签名）

| 用途 | 函数 | 位置 | 核对 |
|---|---|---|---|
| 读 Chrome cookie ST | `read_session_token(profile_path)` | `src/services/keepalive/profile.py:654-697` | ✓ |
| 选 cookie 文件 | `_chrome_cookie_files(profile_path)` | 同上 `:632-651` | ✓ |
| profile lease | `acquire_profile_lease(base_dir, token_id)` | 同上 `:249-261` | ✓ |
| ST→AT + credits + email 验证 | `inspect_account_identity(flow_client, st)` | `src/services/tokens/account_identity.py:47-107` | ✓ |
| 邮箱规范化 | `normalize_account_email(email)` | `src/core/account_identity.py:31-33` | ✓ |
| 项目池幂等 | `ensure_project_pool(db, flow_client, token, pool_size, base_name=None)` | `src/services/tokens/project_pool.py:65-105` | ✓（模块级，非 db 方法）|
| Chrome argv | `build_browser_command(...)` | `scripts/setup_keepalive_profile.py:118-136`（提取到 onboard.py）| ✓ |
| 迟滞纯函数 | `transition_account_lifecycle(...)` | `src/core/account_lifecycle.py:29-64` | ✓（已是纯函数）|
| tier 分类 | `classify_account_tier(tier)` | 同上 `:20-26` | ✓ |
| 事务 | `engine.transaction()` | `src/shared/db/engine.py:45-61` | ✓（BEGIN IMMEDIATE）|
| 巡检 | `read_telemetry` / `classify_telemetry` | `scripts/keepalive_patrol.py` | ✓（status 复用）|

**publisher 核心（复用）**：`apply_verified_snapshot(token_id, snapshot, *, observed_at, allow_auth_reactivate, next_due_at)`（`token_lifecycle_repository.py:195-301`，自身原子）—— identity / collision / membership / auth recovery / 两表 UPDATE 全由它做。
**前提 API**：`create_for_token(token_id, *, db=None)`（同上 `:105-122`，支持外部事务注入）——新号 INSERT tokens 后建 lifecycle 骨架，否则 apply_verified_snapshot 抛 KeyError。
**不直接复用**（自开事务，无法外层包；publisher 不调）：`TokenRepository.update_token`、`set_desired_state`——publisher 的 desired state 在自己的小事务里直接 UPDATE。
