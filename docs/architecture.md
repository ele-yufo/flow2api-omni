# Flow2API 架构与模块地图

本文档描述当前 `main` 工作树中的运行时分层、数据库生命周期、浏览器保活与服务器入库架构。面向维护者与运维人员；账号入库、部署和故障处理步骤见 [浏览器保活运维手册](operations/browser-keepalive.md)。

## 分层与依赖方向

```text
src/
├── shared/          通用配置、SQLite 引擎、存储、鉴权与基础工具
├── core/            Flow2API 数据模型、schema、迁移与 repositories
├── services/        生成、Flow 客户端、Token、验证码、保活与入库业务逻辑
├── api/             FastAPI 路由、管理 API 与协议转换
└── main.py          应用组合根、lifespan、依赖装配与中间件
```

主要依赖方向为 `api → services → core → shared`。`src/shared/` 不反向依赖 Flow2API 业务层，这一边界由 `tests/characterization/test_shared_extractability.py` 守卫。

## 运行时拓扑

```text
                         ┌─────────────────────────────┐
HTTP / 管理后台 ───────▶ │ 主服务 flow2api.service     │
                         │ FastAPI + TokenManager      │
                         │ OnboardingService           │
                         └──────────────┬──────────────┘
                                        │
                                        │ SQLite/WAL
                                        ▼
                         ┌─────────────────────────────┐
                         │ data/flow.db                │
                         │ tokens / token_lifecycle    │
                         │ onboarding_jobs / projects  │
                         └──────────────┬──────────────┘
                                        │
                                        │ 动态 reconcile
                                        ▼
┌───────────────────┐    ┌─────────────────────────────┐
│ Xvfb :10          │◀───│ flow2api-keepalive.service │
│ 保活显示器        │    │ 浏览器 sidecar             │
└───────────────────┘    └─────────────────────────────┘

┌───────────────────┐    ┌─────────────────────────────┐
│ 可选 XRDP :11     │◀───│ OnboardingService Chrome   │
│ 人工 Google 登录  │    │ 仅入库/重新登录时启动       │
└───────────────────┘    └─────────────────────────────┘
```

- **主服务**负责 schema 迁移、业务 API、Token 管理、项目池和 `OnboardingService`。主服务启动时恢复或安全检查未完成的入库任务。
- **保活 sidecar**运行 `scripts/keepalive_browser.py --daemon`，读取 `token_lifecycle.keepalive_enabled=1` 的账号，而不是只读取 `tokens.is_active=1`。数据库中的 desired state 改动可在 reconcile 周期内生效，无需重启 sidecar。systemd unit 可选读取 `/etc/flow2api-keepalive.env`；该 root-owned `0600` 文件用于在仓库外注入 `FLOW2API_ALERT_WEBHOOK_URL`，unit 本身不包含 webhook 值。
- **Xvfb `:10`**是有头保活 Chrome 的运行显示器。保活实现始终以 `headless=False` 启动浏览器。
- **XRDP `:11`**是可选的人工入库显示器，只在创建或修复账号 profile 时使用，不是 sidecar 的 systemd 运行依赖。
- **每账号 profile**位于配置的 `browser_profile_base/<token_id>`。验证码 `personal` 模式使用的 captcha profile/共享标签页属于另一运行域，不能与某个账号的 keepalive profile 混用或并发占用。

## `shared/`：通用基础

关键模块包括：

| 模块 | 职责 |
|---|---|
| `shared/config/provider.py` | TOML 与环境变量配置 provider，包括保活、XRDP 和 CORS 配置 |
| `shared/config/cors.py` | 解析精确的 Web/Chrome extension Origin allowlist，拒绝通配符和带路径 Origin |
| `shared/db/engine.py` | `SqliteEngine` 连接、busy timeout、外键与事务管理 |
| `shared/storage/` | 文件缓存、媒体类型与缓存辅助逻辑 |
| `shared/auth/` | HTTP 鉴权基础能力 |
| `shared/gpu/watermark_client.py` | 去水印服务客户端 |

### 跨进程写事务

`SqliteEngine.transaction()` 在进程内先获取写锁，再执行 `BEGIN IMMEDIATE`。这样主服务与 sidecar 的不同 `Database` 实例通过 SQLite 自身的写锁和 busy timeout 串行化，而不是依赖仅对单进程有效的 `asyncio.Lock`。

事务块异常时 rollback；成功时只 commit 一次。Token 凭据、生命周期状态、入库 claim 与 terminal state 等多行写入均复用该事务边界。

## `core/`：schema、模型与 repositories

`core/database.py` 是 Flow2API 数据层组合根，负责建表、增量迁移、配置回灌以及对 repositories 的薄委托。

### 核心表

#### `tokens`

保存账号凭据、邮箱、credits、tier、业务池状态、禁用原因、当前项目指针和业务统计。`tokens.is_active` 只表达业务负载均衡资格。

#### `token_lifecycle`

与 `tokens` 一对一，保存：

- `keepalive_enabled` 与 `runtime_mode`（`persistent` / `warm`）；
- `profile_state` 与 `verified_email`；
- confirmed membership、候选观察与计数；
- `next_due_at`、最近尝试/成功、失败代码与失败次数；
- 退休、恢复、最近 tier 观察；
- 告警 episode 与投递去重状态。

新增 Token 时会在同一数据库事务中创建默认 `keepalive_enabled=0`、`runtime_mode='warm'`、`profile_state='unprovisioned'` 的 lifecycle 行。升级迁移使用 `INSERT OR IGNORE` 回填现有 Token，不覆盖运维人员已修改的 desired state。旧 `browser_token_ids` 只用于首次 bootstrap；被列出的既有账号回填为 `persistent` / `ready`，其他账号保持禁用和未配置。该 legacy 回填不自动复制 `tokens.email` 到 `verified_email`；首次成功的身份验证快照负责建立绑定，而 preflight 会拒绝尚未绑定的 enabled profile。因此 legacy ID 23 的部署顺序是先运行手工 `--once --token-id 23` 写入验证绑定，再运行 `--preflight`，最后启动 systemd sidecar。

#### `onboarding_jobs`

保存可恢复的服务器 XRDP 入库状态。表中只允许 job ID、目标/解析 Token ID、state/phase、发现的邮箱/tier/credits/有效期、项目数、profile 就绪状态、冲突策略、请求的业务/保活选择、受管 PID 身份、安全错误码和时间戳。

该表不保存 ST、AT、cookie、profile 路径、浏览器命令或任意请求参数。外键删除 Token 时只解除 job 与 Token 的关联；未完成任务会阻止服务层直接删除关联 Token。

### repositories

- `core/repositories/token_repository.py`：Token CRUD、业务状态与删除约束。
- `core/repositories/token_lifecycle_repository.py`：desired state、原子验证快照、会员转换、保活遥测和告警状态。
- `core/repositories/onboarding_job_repository.py`：安全字段 CRUD、单活动任务原子 claim 与可恢复 state/phase 更新。
- `core/repositories/project_repository.py`：账号项目池持久化。
- 其余 repositories 管理请求日志、统计、任务和配置。

## `services/tokens/`：身份、会员与项目池

### 原子验证快照

浏览器保活和凭据替换最终都进入 `apply_verified_account_snapshot()`：

1. 以 `BEGIN IMMEDIATE` 重新读取 Token 与 lifecycle；
2. 使用 `strip().casefold()` 归一化邮箱，校验观察身份同时匹配 `tokens.email` 与已有 `verified_email`；
3. 检查轮换 ST 未被其他 Token 占用；
4. 对精确 tier 执行会员状态转换；
5. 原子更新 ST、AT、有效期、credits、tier、profile/保活成功状态与会员遥测；
6. 保留业务调用的 `last_used_at` 与 `use_count`，保活不计作业务使用。

任何身份冲突、ST collision 或事务错误都会阻止部分写入。

### 会员状态与禁用原因所有权

精确 `PAYGATE_TIER_ONE` / `PAYGATE_TIER_TWO` 归类为 paid，精确 `PAYGATE_TIER_NOT_PAID` 归类为 free；缺失或未知值为 unknown。

- 活跃会员连续两次 free 观察后进入 retired；只有业务行当时仍 active 且没有其他 ban owner 时，才写入 `membership_expired`。
- 退休会员连续两次 paid 观察后进入 active；只有业务行仍满足 `is_active=0 AND ban_reason='membership_expired'` 才恢复业务资格。
- `manual_disabled`、`429_rate_limit`、`consecutive_errors`、`ST_REVOKED`、`GRANT_EXPIRED` 和 `onboarding_pending` 各自表达不同所有者，不被会员状态转换随意清除。

因此 keepalive health、membership 和 business eligibility 是三个相关但独立的状态维度。

### 项目池修复

`services/tokens/project_pool.py::ensure_project_pool()`：

- 只创建配置数量中缺少的 active 项目；
- 按持久化 row ID 稳定排序；
- 当前项目仍在目标池中时不旋转指针；
- 当前指针缺失或指向无效项目时，修复为池中的第一个项目；
- pool size 被限制在支持范围内。

`OnboardingService` 在账号身份验证后调用该幂等路径，所以重新入库不会因为“补齐项目池”而无条件切换当前业务项目。

## `services/keepalive/`：浏览器保活包

| 模块 | 职责 |
|---|---|
| `models.py` | `RuntimeMode`、稳定 failure code 与 `RefreshOutcome` |
| `profile.py` | profile canonical path、跨进程 lease、SingletonLock/PID 所有权、SQLite cookie 读取及 Chrome proxy userinfo 拒绝 |
| `refresher.py` | Flow 导航、`/auth/session`、身份校验、cookie ST、credits 与原子快照 |
| `scheduler.py` | 稳定 stagger、成功周期、指数退避和 human-action 重试 |
| `alerts.py` | 失败 episode 与恢复告警去重 |
| `supervisor.py` | 动态 reconcile、per-token runner、并发限制与有界停止 |

### 刷新链路

`KeepaliveRefresher` 访问账号当前 Flow 项目（没有项目时访问 Flow 首页），等待页面就绪，再访问 `/fx/api/auth/session` 获取浏览器会话 AT 和邮箱。身份校验通过后，从 Chrome `Default/Cookies` 中确定性选择 `labs.google` 的 `__Secure-next-auth.session-token`，拒绝短于 100 字节的值，然后以 AT 调用真实 credits 接口。

成功数据作为一个 `VerifiedAccountSnapshot` 原子写入。会话拒绝、身份不匹配、cookie 缺失和授权过期会标记为需要人工处理；网络/浏览器错误按策略重试。错误遥测不包含 ST、AT、项目 ID 或 profile 路径。

### 调度与运行模式

- active 成功周期默认 1200 秒；retired 成功周期默认 43200 秒；初始延迟默认 120 秒。
- 普通失败采用 60 秒起步、1800 秒封顶的指数退避；需要人工处理的失败默认 21600 秒后重试。
- Token ID 产生稳定 stagger，避免账号在同一时刻集中刷新。
- `persistent` 在成功后保留浏览器和 lease；`warm` 每次到期启动并在尝试后释放。
- 浏览器 launch 与 refresh 默认全局并发均为 1。
- reconcile 默认每 15 秒重新查询数据库。移除账号时停止 runner；模式从 persistent 改为 warm、profile 不再 ready 或代理变化时释放旧浏览器资源。
- runner 收到停止请求后取消活动刷新并在 20 秒边界内排空浏览器/profile 资源；systemd 的 `TimeoutStopSec=45s` 为整个 sidecar 留出更大的退出窗口。
- `--preflight` 在 `browser_enabled=false` 时不接触依赖、display、数据库或 profile，直接报告 disabled 并以 0 退出。
- preflight 与 setup 的运维输出不包含配置的 profile/browser 绝对路径；canonical path 校验失败会在输出前转换为不含路径的通用错误。
- read-only patrol 对已记录成功执行 freshness 检查：active 和 retired 分别读取配置的成功 interval；grace 为对应 interval 的一半，并限制在 300–3600 秒。最近成功超过 interval + grace，或 `next_due_at` 超过 grace，均为 unhealthy；无法解析的 telemetry 是 probe error。

### profile 与进程所有权

sidecar 不使用 `pkill -f`。每个 profile 先获取 `.flow2api-locks/<token_id>.lock` 的非阻塞 `flock`，再检查 Chrome `SingletonLock`：

- 精确 PID 的 cmdline 拥有 canonical `--user-data-dir` 时判定 busy；
- PID 不存在或不再拥有该 profile 时才可判定 stale；
- foreign hostname、格式异常或检查竞态判定 unsafe，拒绝清理；
- 删除 stale Singleton artifacts 前再次比较 inode、device 与 symlink target，避免 TOCTOU 误删；
- sidecar launcher 与 setup helper 在生成 `--proxy-server` 前拒绝含 username/password userinfo 的代理 URL，错误信息不回显用户名或密码。认证代理应由不暴露凭据的本地代理端点承接。

## `OnboardingService`：服务器 XRDP 入库

`services/onboarding.py::OnboardingService` 由主服务 lifespan 构造，所有浏览器 executable、display、proxy、Flow URL 和 profile base 都来自服务端配置，不接受请求提供路径或命令参数。

### 实际 state 与 phase

- state：`pending`、`running`、`failed`、`cancelled`、`completed`。
- 正常 phase：`created` → `browser_start` → `awaiting_login` → `validating_destination` → `account_commit` → `commit_complete` → `completed`。
- 安全失败会记录发生阶段，例如 `stop_browser`、`verify_account`、`migrate_profile`、`final_validation`、`account_commit`、`cancel` 或 `recovery`。

`claim_onboarding_job()` 在 `BEGIN IMMEDIATE` 事务中保证同一时刻只有一个 running XRDP job。Chrome 使用参数数组、`shell=False`、`umask 077` 和 `<profile_base>/.onboarding/<job-id>` 私有目录启动。

如果操作员关闭 Chrome 后 finalize 因缺少 Flow 登录 cookie 停在 `failed/verify_account`，或安全停浏览器阶段停在 `failed/stop_browser`，且任务尚无 resolved token、身份发现结果或迁移元数据，原有 `POST .../start` 可以重新打开同一个临时 profile。恢复启动先持有 onboarding profile service lease，拒绝 BUSY/UNSAFE 或所有权不确定状态，仅清理由稳定检查证明 stale 的 Singleton artifacts；随后用 compare-and-swap 在没有其他 running/failed job 时原子认领该 failed job，并在同一次事务写入中把 `expires_at` 刷新为当前时间加服务端 session TTL。原截止时间已经过期不阻止满足全部安全条件的 failed job 恢复；pending 过期任务仍按原行为取消。迁移、final validation 与 `account_commit` 失败不可通过 start 重启浏览器。

Finalize 只停止记录的 PID，且同时校验 procfs start ticks 和 canonical `--user-data-dir`。随后读取 cookie、验证真实身份、匹配现有账号或创建 `onboarding_pending` 的新账号、补齐项目池、在同一文件系统使用 `os.rename` 迁移 profile，再从目标目录二次验证相同身份。

目标 profile 已存在时默认 `reject`。显式 `archive_and_replace` 会先把旧目录移动到 `<profile_base>/.archive/<token_id>/<job-id>`，再采用新目录；第二次 rename 失败会把旧 profile 恢复。账号状态提交完成后用 `commit_complete` 作为可恢复标记，避免进程崩溃造成重复创建或回滚已发布凭据。

新账号只有在精确 paid tier 且操作员请求业务启用时才会进入业务池。已有账号保留原有 business state 与 ban owner；重新登录不会清除人工或 429 禁用。

## 管理 API 与浏览器安全边界

所有入库、profile validation、lifecycle 修改和凭据导出端点都要求管理员 session Bearer token。入库相关响应设置 `Cache-Control: no-store`，并排除 ST、AT、内部 row ID、browser PID/start ticks、路径和命令。

主要安全端点：

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/onboarding/config` | 返回 UI 所需的安全显示器标识 |
| `POST` | `/api/onboarding/jobs` | 创建 allowlisted 入库任务 |
| `GET` | `/api/onboarding/jobs` | 按安全字段筛选任务 |
| `GET` | `/api/onboarding/jobs/{job_id}` | 读取一个任务 |
| `POST` | `/api/onboarding/jobs/{job_id}/start` | 启动 pending job，或安全恢复允许重新登录的 pre-migration failed job |
| `POST` | `/api/onboarding/jobs/{job_id}/finalize` | 验证、迁移并提交账号 |
| `POST` | `/api/onboarding/jobs/{job_id}/cancel` | 停止已验证归属的进程并取消 |
| `POST` | `/api/onboarding/recover` | 恢复或安全收敛未完成任务 |
| `POST` | `/api/tokens/{token_id}/validate-profile` | 只读验证 retained profile，不写 Token/lifecycle |
| `PUT` | `/api/tokens/{token_id}/lifecycle` | 只改 keepalive desired state/mode |
| `POST` | `/api/tokens/{token_id}/export` | 显式、不可缓存地导出凭据 |

普通 `GET /api/tokens` 仅返回 `has_st` / `has_at` 等状态，不返回原始凭据。

同源管理页面不需要 CORS。跨域 Web 控制台与 Chrome extension 只能使用 `[server].cors_allowed_origins` 或 `FLOW2API_CORS_ALLOWED_ORIGINS` 中的精确 Origin；`*` 被拒绝。插件端点仍使用独立 connection token Bearer 认证，CORS 不构成授权。

## 测试与架构守卫

项目统一通过以下入口运行离线测试：

```bash
/opt/Projects/flow2api/scripts/test.sh
```

测试默认使用临时 SQLite 数据库，不启动真实 Chrome、不访问生产 profile，也不访问真实 Google API。重点守卫包括 shared 可提取性、未定义名称、数据库迁移/事务、原子身份快照、profile/PID 所有权、scheduler/supervisor、入库恢复、安全 API、CORS 与运维脚本契约。
