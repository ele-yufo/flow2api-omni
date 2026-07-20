# 浏览器保活运维手册

本文档用于运维 Flow2API 的数据库驱动浏览器保活、服务器 XRDP 账号入库、已有账号重新登录、健康检查、部署与回滚。

> **[DEPRECATED] 2026-07-20**：本文档中标注 `[DEPRECATED]` 的小节（§3.4、§7.3、
> §7.4）描述的是旧 `OnboardingService` 状态机（2810 行）+ 对应 admin HTTP
> API（`/api/onboarding/*`）。该状态机曾在生产引发事故（用户被迫反复重新登录、
> 一个有效会话被销毁、操作了错误的 XRDP Chrome 窗口），其 HTTP 层已永久禁用
> （固定返回 `410 Gone`，路由本身保留注册以避免 404 混淆）。新入库/重登录一律
> 使用 §7.5「简化入库隧道」描述的 `scripts/tokens.py onboard` CLI。旧状态机的
> service 层代码仍在仓库中（删除需要改动 admin.py 的 import，风险更高于收益），
> 但不会再被任何 HTTP 请求触发。

## 1. 适用范围与安全边界

浏览器 keepalive 解决的是纯 HTTP ST 轮换不能永久维持 Google OAuth 授权的问题。每个 Token 使用独立持久化 Chrome profile；sidecar 以有头浏览器刷新 Flow 会话，验证邮箱、SQLite cookie ST、AT 与 credits 后，才原子更新数据库。

必须同时遵守以下边界：

- `tokens.is_active` / `ban_reason` 是**业务池状态**；`token_lifecycle.keepalive_enabled` 是**保活 desired state**。两者不能互相替代。
- keepalive profile 路径固定为 `<browser_profile_base>/<token_id>`，一个账号一个目录。
- `personal` 验证码模式的 captcha profile 是共享打码资源，不是 keepalive profile。不要把 captcha profile 配给某个 Token 的保活、setup 或 XRDP 入库。
- sidecar、XRDP Chrome、setup helper 不得同时使用同一个 keepalive profile。
- 不要手工删除 `SingletonLock`，也不要用 `pkill -f` 清 Chrome。服务只在精确 PID、procfs start ticks 与 canonical `--user-data-dir` 校验通过时停止进程；只有证明 lock stale 后才清理 Singleton artifacts。
- nodriver 的控制端点由库在 `127.0.0.1` 上动态选择端口，不配置或暴露远程 debugging 端口。
- 管理 API、数据库、TOML、profile 与归档中均可能含敏感认证材料，只允许服务账号和授权管理员访问。

## 2. 运行拓扑

| 组件 | 默认位置/显示器 | 责任 |
|---|---|---|
| 主服务 `flow2api.service` | 应用 HTTP 端口 | FastAPI、迁移、Token 管理、`OnboardingService`、管理 API |
| 保活 sidecar `flow2api-keepalive.service` | Xvfb `:10` | 动态读取 `token_lifecycle`，运行 headed Chrome 刷新账号 |
| SQLite | `/opt/Projects/flow2api/data/flow.db` | `tokens`、`token_lifecycle`、`onboarding_jobs`、projects |
| keepalive profiles | `/opt/flow2api-profiles/<token_id>` | 每账号 Google/Flow 登录态 |
| XRDP | 默认 `:11`，可配置 | 操作员进行 Google 登录；只在入库/重登录时需要 |
| onboarding 临时 profile | `<profile_base>/.onboarding/<job-id>` | 受管登录任务的私有临时目录 |
| profile 归档 | `<profile_base>/.archive/<token_id>/<job-id>` | `archive_and_replace` 保留的旧 profile |

XRDP 是可选 provisioning 依赖，不是 sidecar 的 systemd runtime dependency。生产 sidecar 依赖主服务、网络就绪和 Xvfb `:10`。

## 3. 状态与原因

### 3.1 业务池禁用原因

| `ban_reason` | 含义 | 自动保活行为 | 操作建议 |
|---|---|---|---|
| `manual_disabled` | 管理员显式禁用 | keepalive 可继续运行 | 确认业务原因后手工启用 |
| `429_rate_limit` | Google 429 限流 | keepalive 不得清除该原因 | 等待限流策略解禁或人工复核 |
| `consecutive_errors` | 业务连续错误阈值触发 | keepalive 不得清除该原因 | 排查业务调用错误后手工处理 |
| `membership_expired` | 连续两次确认 free 后退休 | 以 43200 秒低频继续保活 | 续费后等待两次 paid 观察条件恢复 |
| `onboarding_pending` | 新账号入库尚未完成 | 未发布为 ready 前不应启动 | 完成或取消 onboarding job |
| `ST_REVOKED` | ST 本身被拒绝/撤销 | 需要人工重新登录 | 对目标 Token 发起 XRDP 重新登录 |
| `GRANT_EXPIRED` | ST 尚存在但 Google OAuth grant/AT 无法验证 | 需要浏览器重新授权 | 对目标 Token 发起 XRDP 重新登录 |

浏览器 sidecar 的 failure telemetry 使用稳定的小写 code，例如 `grant_expired`；业务 Token 的历史禁用原因保持大写 `GRANT_EXPIRED` / `ST_REVOKED`。巡检和排障时需要区分这两个字段。

### 3.2 会员状态

- confirmed status 为 `active` 或 `retired`。
- 精确 `PAYGATE_TIER_ONE`、`PAYGATE_TIER_TWO` 为 paid。
- 精确 `PAYGATE_TIER_NOT_PAID` 为 free。
- 缺失、未知、大小写不同或带额外空白的 tier 为 unknown，不计作 free。
- active 连续两次 free 才退休；第一次只记录候选。
- retired 连续两次 paid 才恢复；`TIER_ONE` 与 `TIER_TWO` 可以组成连续 paid 观察。
- unknown 不推进也不清除已有候选；与当前 confirmed status 同向的有效观察会清除反向候选。
- 退休只会在业务行没有其他 ban owner 时写入 `membership_expired`。
- 恢复只会更新仍满足 `is_active=0 AND ban_reason='membership_expired'` 的行；不会恢复人工、429 或连续错误禁用。

### 3.3 keepalive failure code

| code | 含义 | 重试类别 |
|---|---|---|
| `profile_missing` | profile 不存在、未 provision 或不是 ready | 人工处理，默认 21600 秒后再试 |
| `profile_busy` | service lease、Chrome profile 或 cookie DB 被占用 | 视具体所有权决定短重试或人工处理 |
| `identity_mismatch` | 浏览器邮箱不匹配 Token/profile 绑定 | 人工处理 |
| `browser_launch` | Chrome 启动失败 | 关闭当前资源，下次重启浏览器 |
| `navigation` | Flow 或 session 页面导航/页面目标失败 | 浏览器错误时重启；网络错误按网络重试 |
| `session_body` | `/auth/session` 未返回有效 JSON/AT | 普通重试或浏览器重启 |
| `cookie_missing` | ST cookie 缺失、无法解密或短于 100 字节 | 人工重新登录/修 keyring |
| `session_rejected` | 浏览器会话返回 401/403 或无身份 | 人工重新登录 |
| `grant_expired` | credits 认证返回 401 | 人工重新授权 |
| `credits` | credits 响应格式无效或请求非网络失败 | 指数退避 |
| `network` | DNS、代理、连接或 timeout | 指数退避 |
| `internal` | 输入、事务或内部一致性失败 | 指数退避并检查日志 |

### 3.4 [DEPRECATED] onboarding state 与 phase

> 本节描述旧 `OnboardingService` 状态机的内部模型，仅作历史/排障参考（现存
> `onboarding_jobs` 表历史行的 state/phase 仍可能是这些值）。其 HTTP 层已禁用
> （见文档顶部说明与 §7.3）；新入库/重登录见 §7.5。

state：`pending`、`running`、`failed`、`cancelled`、`completed`。

正常 phase：

```text
created → browser_start → awaiting_login → validating_destination
        → account_commit → commit_complete → completed
```

失败记录可能停在 `stop_browser`、`verify_account`、`migrate_profile`、`final_validation`、`account_commit`、`cancel` 或 `recovery`。`failed` job 可以在服务允许的 phase 上继续 finalize；如果 `stop_browser` / `verify_account` 尚无 resolved token、身份发现结果和迁移元数据，可以再次调用原 `start` 端点，用同一临时 profile 补做 Flow 登录。恢复启动持有 service lease，只清理已证明 stale 的 Singleton artifacts；BUSY、UNSAFE、所有权不确定、其他 failed/running job 或迁移后的 phase 都会拒绝。即使原 `expires_at` 已经过期，满足条件的 failed job 也可恢复，服务会在原子认领事务中把截止时间刷新为当前时间加 session TTL；pending 过期任务仍会取消。`commit_complete` 表示账号状态已经提交，只需安全补写 completed。

## 4. 调度、模式与动态 reconcile

### 4.1 运行模式

- `persistent`：到期刷新后继续保留 Chrome 与跨进程 profile lease。`scripts/tokens.py onboard` 入库/重登录发布的账号永远是该模式——新账号与部署兼容基线 ID 23 均适用；管理 API（`PUT /api/tokens/{id}/lifecycle`）与 CLI 也只接受把账号设置为该模式。
- `warm`：到期时启动 Chrome，完成一次成功或失败尝试后关闭并释放 lease。曾在生产事故中因一次性 one-shot 拆掉 resident Chrome 并重新导航，把一个有效会话的 session cookie 轮换进未授权状态（见文档顶部说明）；因此该值仅作为尚未入库（`profile_state='unprovisioned'`）账号的 DB 默认行存在，任何管理入口都不再允许把账号显式设置为该模式。

两种模式使用同一验证链路和 due time，只改变刷新后的浏览器所有权。

### 4.2 默认 cadence

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `browser_initial_delay_seconds` | 120 | sidecar 启动后的基础首轮延迟 |
| `browser_interval_seconds` | 1200 | active 成功刷新周期 |
| `browser_retired_interval_seconds` | 43200 | retired 成功刷新周期 |
| `browser_reconcile_interval_seconds` | 15 | 数据库 desired state reconcile 周期 |
| `browser_retry_base_seconds` | 60 | 普通失败指数退避起点 |
| `browser_retry_max_seconds` | 1800 | 普通失败退避上限 |
| `browser_human_retry_seconds` | 21600 | 需要人工处理时的重试间隔 |
| `browser_max_concurrent_launches` | 1 | 全局 Chrome launch 并发 |
| `browser_max_concurrent_refreshes` | 1 | 全局刷新并发 |

Token ID 会产生稳定 stagger，避免所有账号在同一秒启动。成功后的 active/retired due time 落在固定周期相位；普通失败按 60、120、240、480、960、1800 秒递增并封顶。

### 4.3 动态变更

sidecar 每次 reconcile 查询 `token_lifecycle.keepalive_enabled=1`：

- 新启用账号自动创建 runner；
- 关闭 keepalive 自动停止 runner 并释放资源；
- `persistent → warm` 自动关闭 resident Chrome；
- profile 不再 ready 或账号代理改变时释放旧浏览器；
- 数据库暂时不可用时保留最后一次已知 runner 集合，不把全部账号误停。

业务禁用账号和 retired 账号仍可被 sidecar 管理。

## 5. 依赖、配置与环境变量

### 5.1 软件依赖

- Python 项目虚拟环境：`/opt/Projects/flow2api/.venv`
- `nodriver==0.48.1`
- `browser-cookie3==0.20.1`
- Google Chrome Stable，默认 `/usr/bin/google-chrome-stable`
- Xvfb display `:10`
- 可选 XRDP display，默认 `:11`
- 可用的用户 D-Bus/keyring 会话，以便 `browser_cookie3` 解密 Chrome cookie
- SQLite CLI 用于维护窗口备份与只读验收查询

安装项目依赖：

```bash
/opt/Projects/flow2api/.venv/bin/pip install -r /opt/Projects/flow2api/requirements.txt
```

### 5.2 `[keepalive]` 配置

```toml
[keepalive]
browser_enabled = true
browser_token_ids = "23"
browser_interval_seconds = 1200
browser_initial_delay_seconds = 120
browser_retired_interval_seconds = 43200
browser_reconcile_interval_seconds = 15
browser_max_concurrent_refreshes = 1
browser_max_concurrent_launches = 1
browser_retry_base_seconds = 60
browser_retry_max_seconds = 1800
browser_human_retry_seconds = 21600
browser_profile_base = "/opt/flow2api-profiles"
browser_proxy = "http://127.0.0.1:7890"
browser_display = ":10"
browser_settle_seconds = 8.0
onboarding_display = ":11"
onboarding_session_ttl_seconds = 1800
```

`browser_token_ids` 只用于旧部署首次迁移/bootstrap。迁移后每账号 desired state 以 `token_lifecycle` 为准；不要通过长期修改 TOML 列表管理账号。

`browser_proxy` 以及账号级 `captcha_proxy_url` 在 sidecar 启动 Chrome 前都会经过 credential-free proxy validation。禁止配置 `scheme://username:password@host:port` 形式的嵌入式 userinfo；setup helper 使用同一校验，违规值会在构造 `--proxy-server` 参数前失败，且错误不会回显 username/password。需要认证时应在本机提供不含凭据的代理入口，由该入口处理上游认证。

### 5.3 环境变量

| 变量 | 作用 |
|---|---|
| `BROWSER_EXECUTABLE_PATH` | Chrome executable；未设置时使用 `/usr/bin/google-chrome-stable` |
| `DISPLAY` | setup helper 使用调用者可见 display；systemd sidecar 固定为 `:10` |
| `DBUS_SESSION_BUS_ADDRESS` | cookie 解密所需用户 keyring bus |
| `XDG_RUNTIME_DIR` | 对应服务用户 runtime 目录 |
| `FLOW2API_ALERT_WEBHOOK_URL` | Discord 告警 webhook，优先于 TOML |
| `FLOW2API_CORS_ALLOWED_ORIGINS` | 精确 CORS Origin 列表，逗号分隔；存在时覆盖 TOML |

### 5.4 可选 systemd webhook 环境文件

tracked unit 使用 `EnvironmentFile=-/etc/flow2api-keepalive.env`。前缀 `-` 表示该文件不存在时仍可启动；需要 sidecar Discord 告警时，才在服务器本地创建它：

```bash
sudo install -m 0600 -o root -g root /dev/null /etc/flow2api-keepalive.env
sudoedit /etc/flow2api-keepalive.env
```

文件格式如下，文档和仓库中只保留空值示意，不写入任何真实 webhook：

```dotenv
FLOW2API_ALERT_WEBHOOK_URL=
```

由管理员在服务器上的 `sudoedit` 会话中填写真实值，保存后确认 owner 为 `root:root`、mode 为 `0600`，再重启 sidecar。不要用包含真实 URL 的命令行、shell history、工单或版本控制文件生成它。

## 6. CORS 与 Chrome 插件

同源管理页面无需 CORS。默认 `cors_allowed_origins = []` 表示不允许任何跨域浏览器 Origin。

跨域 Web 控制台或 Chrome extension 必须配置浏览器实际发送的**完整 Origin**：

- Web Origin 必须是 `http://host[:port]` 或 `https://host[:port]`。
- 插件 Origin 必须是 `chrome-extension://` 加实际 extension ID。
- 禁止 `*`。
- 禁止路径、尾随路径、query 与 fragment；配置解析会移除单个结尾 `/` 后做精确匹配。
- 重复项会去重。

先从浏览器 DevTools 的请求 `Origin` header 读取真实值，再写入 `[server].cors_allowed_origins`，或设置：

```bash
export FLOW2API_CORS_ALLOWED_ORIGINS="$ACTUAL_WEB_ORIGIN,$ACTUAL_CHROME_EXTENSION_ORIGIN"
```

`FLOW2API_CORS_ALLOWED_ORIGINS` 会覆盖 TOML，不是追加。修改后重启主服务，因为 CORS middleware 在应用创建时装配。

插件继续使用 `/api/plugin/update-token` 的 connection token：

```http
Authorization: Bearer <plugin-connection-token>
```

CORS 只允许浏览器发送跨域请求，不替代 Bearer 认证。普通 `/api/tokens` 不返回 ST/AT；凭据导出必须使用管理员认证的 `POST /api/tokens/{token_id}/export`，响应设置 `Cache-Control: no-store`。

## 7. 管理 API

以下请求都需要管理员登录返回的 session token，而不是外部生成 API key：

```bash
BASE_URL="http://127.0.0.1:8000"
ADMIN_TOKEN="$(read -rsp 'Admin session token: ' VALUE; printf '%s' "$VALUE")"
AUTH_HEADER="Authorization: Bearer $ADMIN_TOKEN"
```

不要把 token 写入 shell history、共享脚本或工单。

### 7.1 profile validation

> `GET /api/onboarding/config`（获取 UI 所需的有效 XRDP display）随旧状态机一起
> [DEPRECATED]，固定返回 410；已从本节移除。管理页面的 XRDP display 配置改由
> `scripts/tokens.py onboard --display :N` 显式传入（见 §7.5）。

只读验证已有 profile（不受 onboarding 状态机禁用影响，正常工作）：

```bash
curl --fail-with-body -sS -X POST \
  -H "$AUTH_HEADER" \
  "$BASE_URL/api/tokens/$TOKEN_ID/validate-profile"
```

validation 从 retained profile 读取真实 ST，调用账号检查，要求观察邮箱同时匹配 Token 邮箱和 `verified_email`，返回 email、tier、credits、expiry、active project count 与 `profile_ready`。该端点不更新 Token、lifecycle 或项目池，也不会回退使用数据库中的 ST。

### 7.2 lifecycle desired state

`runtime_mode` 只接受 `"persistent"`：传 `"warm"` 会在请求体校验阶段被拒绝，返回
`422`。这是刻意的——一次 `warm` one-shot 曾在生产事故中拆掉 resident Chrome 并
重新导航，把一个有效 Google 会话的 session cookie 轮换进未授权状态（见文档顶部
说明）；该值不再有任何管理入口可以设置（`set_desired_state` 在 DB 写入层做了
同样的拒绝）。

开启保活（常驻 persistent）：

```bash
curl --fail-with-body -sS -X PUT \
  -H "$AUTH_HEADER" \
  -H 'Content-Type: application/json' \
  -d '{"keepalive_enabled":true,"runtime_mode":"persistent"}' \
  "$BASE_URL/api/tokens/$TOKEN_ID/lifecycle"
```

关闭 keepalive：

```bash
curl --fail-with-body -sS -X PUT \
  -H "$AUTH_HEADER" \
  -H 'Content-Type: application/json' \
  -d '{"keepalive_enabled":false}' \
  "$BASE_URL/api/tokens/$TOKEN_ID/lifecycle"
```

该 API 不改变 `tokens.is_active` 或 `ban_reason`。业务启停仍使用独立管理操作。

### 7.3 [DEPRECATED] onboarding job API（已禁用，固定返回 410）

> 本节全部 8 个路由（`GET /api/onboarding/config`、`POST/GET
> /api/onboarding/jobs`、`GET /api/onboarding/jobs/{id}`、
> `POST /api/onboarding/jobs/{id}/{start,finalize,cancel}`、
> `POST /api/onboarding/recover`）已永久禁用，固定返回
> `410 Gone`（`{"code":"onboarding_deprecated","message":"..."}`），不再执行下方任何流程。
> 保留本节仅供历史/排障参考。新入库/重登录见 §7.5。
>
> 注：主服务启动时仍会内部调用 `OnboardingService.recover_incomplete()`
> 清理遗留 `onboarding_jobs` 行（未改动，属于服务层而非 HTTP 层，见 Task 7
> 范围说明）——这与下方 `POST /api/onboarding/recover` 的**HTTP 入口**已禁用
> 并不矛盾，二者是两回事。

创建新账号 job 时不传 `target_token_id`；重新登录已有账号时传目标 ID。请求只接受 allowlisted choices，不接受 path、display、proxy、URL 或浏览器参数。

```bash
CREATE_RESPONSE="$(curl --fail-with-body -sS -X POST \
  -H "$AUTH_HEADER" \
  -H 'Content-Type: application/json' \
  -d "{\"target_token_id\":$TOKEN_ID,\"conflict_policy\":\"reject\",\"requested_business_enabled\":false,\"requested_keepalive_enabled\":true,\"requested_runtime_mode\":\"warm\"}" \
  "$BASE_URL/api/onboarding/jobs")"
JOB_ID="$(printf '%s' "$CREATE_RESPONSE" | /opt/Projects/flow2api/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["job"]["job_id"])')"
```

对新账号创建任务时，使用不含 `target_token_id` 的 JSON：

```json
{
  "conflict_policy": "reject",
  "requested_business_enabled": false,
  "requested_keepalive_enabled": true,
  "requested_runtime_mode": "warm"
}
```

启动受管 Chrome：

```bash
curl --fail-with-body -sS -X POST \
  -H "$AUTH_HEADER" \
  "$BASE_URL/api/onboarding/jobs/$JOB_ID/start"
```

操作员通过服务器 XRDP 连接 API 返回配置对应的 display，在 Chrome 中完成 Google 登录并打开 Flow。不要更改 `--user-data-dir` 或另起使用同一临时 profile 的 Chrome。

如果 Chrome 已关闭且 finalize 返回 `login_required`，先确认 job 仍为 pre-migration 的 `failed/verify_account`（或可恢复的 `failed/stop_browser`），然后再次执行同一个 `POST .../start` 命令。服务会在 profile lease 下验证没有 live owner、仅清理已证明 stale 的 Singleton artifacts，并重新打开原 profile；不会新建 profile，也不会允许 `migrate_profile`、`final_validation` 或 `account_commit` 失败走该恢复路径。原 `expires_at` 即使已经过去也无需新建 job：成功认领恢复时会原子刷新一个完整 session TTL。

登录完成后调用 finalize；服务会停止其拥有的 Chrome，等待 cookie flush，执行身份检查、项目池修复、profile 迁移、目标二次验证与账号状态提交：

```bash
curl --fail-with-body -sS -X POST \
  -H "$AUTH_HEADER" \
  "$BASE_URL/api/onboarding/jobs/$JOB_ID/finalize"
```

查询一个 job：

```bash
curl --fail-with-body -sS \
  -H "$AUTH_HEADER" \
  "$BASE_URL/api/onboarding/jobs/$JOB_ID"
```

按目标与状态筛选：

```bash
curl --fail-with-body -sS -G \
  -H "$AUTH_HEADER" \
  --data-urlencode "target_token_id=$TOKEN_ID" \
  --data-urlencode 'state=running' \
  "$BASE_URL/api/onboarding/jobs"
```

取消尚未提交的 job：

```bash
curl --fail-with-body -sS -X POST \
  -H "$AUTH_HEADER" \
  "$BASE_URL/api/onboarding/jobs/$JOB_ID/cancel"
```

主服务启动会自动执行 incomplete-job recovery；管理员也可显式触发：

```bash
curl --fail-with-body -sS -X POST \
  -H "$AUTH_HEADER" \
  "$BASE_URL/api/onboarding/recover"
```

### 7.4 [DEPRECATED] `archive_and_replace`

> 是 §7.3 已禁用的 `POST /api/onboarding/jobs` 的一个 `conflict_policy` 取值，
> 随 §7.3 一起禁用（固定 410）。保留本节仅供历史/排障参考——如果旧
> `.archive/<token_id>/<job-id>` 目录仍然存在，下方语义仍是理解它的依据。
> 新隧道（§7.5）不支持归档/替换：旧号重登录永远在其自身 canonical profile
> 原地进行，不 archive、不覆盖（若目标 profile 冲突，`scripts/tokens.py onboard`
> 会报错退出，不做任何自动决策）。

目标 `<profile_base>/<token_id>` 已存在时，默认 `reject` 会返回 `destination_conflict`，不覆盖旧 profile。只有在以下条件同时满足时才选择 `archive_and_replace`：

1. 已确认目标 Token ID 与登录邮箱一致；
2. 已停止 sidecar runner 或通过 lifecycle 关闭该 Token 的 keepalive；
3. 已确认旧 profile 当前没有 Chrome/process owner；
4. 已为数据库和 profile 做维护窗口备份；
5. 运维人员接受旧 profile 被移动到 `.archive/<token_id>/<job-id>`。

显式创建替换 job：

```json
{
  "target_token_id": 23,
  "conflict_policy": "archive_and_replace",
  "requested_business_enabled": false,
  "requested_keepalive_enabled": true,
  "requested_runtime_mode": "persistent"
}
```

迁移使用同一文件系统的原子 rename。旧 profile 归档不会自动删除；验收结束后仍应保留到回滚窗口关闭。若归档目标已存在，服务返回 `archive_conflict`，不要手工覆盖归档。

### 7.5 简化入库隧道（推荐，替代 onboarding 状态机）

`OnboardingService` 状态机（§3.4、§7.3、§7.4，2810 行）已废弃并禁用其 HTTP
面（固定 410）。新入库、旧号重登录、启停业务池、开关保活统一改用
`scripts/tokens.py` —— 一个只输出 JSON 的 Agent 工具，不是给人读的 CLI。

#### 7.5.1 新号入库

Agent 代表用户执行：

```bash
/opt/Projects/flow2api/.venv/bin/python scripts/tokens.py onboard --email xxx@gmail.com --display :11
```

分阶段输出 JSON（每阶段一行到 stdout）：

```jsonc
// 阶段1：前台 XRDP Chrome 即将打开，进程阻塞等待（默认超时 1800 秒）
{"phase": "awaiting_login", "target": "xxx@gmail.com", "display": ":11",
 "timeout_seconds": 1800,
 "message": "Log in to Google + Flow on the XRDP display, then close Chrome."}

// 阶段2a：成功
{"phase": "published", "token_id": 25, "membership_status": "active",
 "pool_transition": "activated", "business_active": true, "ban_reason": null,
 "keepalive_enabled": true, "runtime_mode": "persistent", "profile_state": "ready"}

// 阶段2b：失败（验证/发布任一步失败；见 7.5.5 安全属性）
{"error": {"code": "identity_mismatch", "message": "..."}, "phase": "failed"}
```

用户在 XRDP（对应 display，如 `:11`）里完成 Google + Flow 登录，看到 Flow
主界面后**关闭 Chrome**——隧道在前台阻塞等 Chrome 进程退出，退出后自动读取
cookie、校验身份、发布账号，不需要用户做任何其他操作。

#### 7.5.2 旧号重启用 / 重登录

```bash
/opt/Projects/flow2api/.venv/bin/python scripts/tokens.py onboard --token-id 21 --display :11
```

先只读验证该 Token 自己的 canonical profile（`<profile_base>/21`）：如果
session 仍然存活，不启动浏览器、不需要用户操作，直接完成校验并发布（秒级）；
如果 session 已失效，才用同一个 canonical profile（不新建、不 archive、不
覆盖）打开前台 Chrome，走与 7.5.1 相同的 `awaiting_login → published/failed`
流程，引导用户重新登录。

#### 7.5.3 管理命令

```bash
# 全局健康：keepalive_enabled=1 的账号 + 被排除（keepalive_enabled=0）的账号
scripts/tokens.py status
scripts/tokens.py status --token-id 21

# 进/出业务池；不改变 keepalive desired state
scripts/tokens.py enable  --token-id 21
scripts/tokens.py disable --token-id 21

# 开关保活；永远 runtime_mode=persistent（无 --mode，机队不再运行 warm）
scripts/tokens.py keepalive --token-id 21 on
scripts/tokens.py keepalive --token-id 21 off
```

`status` 输出 `{"tokens": [...], "excluded_keepalive_disabled": [...]}` 两个
数组：`tokens` 是 `keepalive_enabled=1` 账号的健康快照（含
`business_active`、`runtime_mode`、`profile_state`、`membership_status`、
`last_success_at`、`next_due_at`、`health`/`health_reason` 等，不含 ST/AT）；
`excluded_keepalive_disabled` 是 `keepalive_enabled=0` 的账号（`token_id`、
`email`、`is_active`、`ban_reason`），防止保活被误关的账号从视野中消失
（例如误执行 `keepalive off`、入库中途卡住）。

每个写命令都支持 `--dry-run`（只预览，不写库、不启动浏览器）。

#### 7.5.4 输出约定与退出码

- 只输出 JSON：成功/阶段性结果打到 stdout（一行一个 JSON 对象），错误打到
  stderr（`{"error": {"code", "message", "detail"}}`），永远不打印 ST/AT。
- 稳定退出码，Agent 可以直接按 `returncode` 分支，不需要解析 stderr：

  | 退出码 | 含义 |
  |---|---|
  | `0` | 成功（含 `--dry-run` 预览） |
  | `2` | 命令行参数错误（argparse） |
  | `3` | 目标不存在（token id 未找到 / db 文件缺失） |
  | `4` | 冲突（保留码） |
  | `5` | 校验失败（默认兜底：登录超时、cookie 缺失、身份不匹配、崩溃等大多数 `OnboardError`，需要人工介入，重跑同样输入不会自愈） |
  | `6` | `publish_verified_account` 发布失败 |
  | `7` | 忙（全局 onboard lease 或 profile lease 被占用） |
  | `70` | 内部错误（含被 SIGINT 中断） |

#### 7.5.5 安全属性（不变）

- **同一时刻只允许一个 onboard**：`acquire_onboard_global_lease` 是跨
  `--email`/`--token-id` 的全局 lease（按 `profile_base` 目录持锁），第二个
  并发 onboard 会拿到 `onboard_busy`（退出码 7），不会有两个 XRDP 会话互相
  串扰。
- **只发 persistent，无 warm**：`keepalive` 子命令没有 `--mode` 参数，
  onboard 发布时永远 `runtime_mode="persistent"`。
- **显式 `--profile-directory=Default`**：`build_browser_command` 固定带上此
  参数启动 Chrome，不依赖 Chrome 默认行为选择 profile。
- **验证不过 = 什么都不改**：
  - 新号：先用 `.onboarding/<random>` 临时 profile 登录验证，只有身份匹配、
    项目池就绪都成功后才 `INSERT tokens` 并 rename 到 canonical 路径；任一步
    失败会补偿删除已插入的 token 行和临时/已 rename 的 profile 目录。
  - 旧号：重登录前先暂停 keepalive 以释放 profile lease，若验证或发布失败，
    在每个失败路径上恢复暂停前的 keepalive 状态；重登录永远在账号自己的
    canonical profile 里进行，不 archive、不覆盖。

## 8. 运维命令

所有命令从部署路径执行，使用项目 `.venv`。除 setup 与真实 `--once` gate 外，`--preflight` 和 patrol 不启动 Chrome/不读取凭据。

### 8.1 preflight

```bash
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_browser.py --preflight
```

当 `[keepalive].browser_enabled=false` 时，preflight 立即输出 `browser keepalive is disabled` 并以 0 退出，不导入浏览器依赖、不检查 Chrome/X display、不查询 lifecycle 数据库，也不访问 profiles。启用时，它检查 Python 依赖、Chrome executable、X display、profile base、lifecycle DB，以及所有 enabled profile 的 ready/binding、`Default/Cookies` 文件和 service lease。它不读取 ST 内容，也不调用 Google。preflight 和 setup 的终端输出不会显示配置的 profile base 或 browser executable 绝对路径；canonical path 校验失败会报告不含候选路径的通用 mapping/validation 错误。

### 8.2 one-shot gate

验证一个 Token：

```bash
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_browser.py \
  --once --token-id "$TOKEN_ID"
```

验证全部 keepalive-enabled Token：

```bash
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_browser.py --once
```

`--once` 忽略 persisted due time，运行与 daemon 相同的真实 headed refresh 与原子写库路径；任一目标失败时命令返回非零。

兼容 gate wrapper：

```bash
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_gate_test.py \
  --token-id "$TOKEN_ID"
```

wrapper 只委托生产 `--once --token-id` 路径，不包含独立浏览器、凭据或探测逻辑。

### 8.3 daemon 前台运行

```bash
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_browser.py --daemon
```

不提供 mode 参数时也默认为 daemon。生产环境使用 systemd，不要同时手工启动第二个 daemon。

### 8.4 profile setup

setup 用于**已有且身份已知的 Token**进行兼容性人工登录或修复 per-token profile。它读取项目配置与数据库，使用调用者的可见 `DISPLAY` 或显式 display，持有 service lease，在前台运行 Chrome，等 Chrome 完全退出后读取 cookie 并验证真实账号身份。它不会写入 `token_lifecycle.verified_email`、`profile_state` 或其他账号状态；未 provision 的账号应使用 `OnboardingService` 完整入库，而不是只运行 setup。

使用调用者环境中的 display：

```bash
DISPLAY="$XRDP_DISPLAY" \
  /opt/Projects/flow2api/scripts/setup_keepalive_profile.sh "$TOKEN_ID"
```

也可以把 display 作为第二个位置参数显式传入：

```bash
/opt/Projects/flow2api/scripts/setup_keepalive_profile.sh \
  "$TOKEN_ID" "$XRDP_DISPLAY"
```

setup 不会删除 SingletonLock、不杀进程、不后台化 Chrome。检测到 service lease、任何 Singleton artifact、登录邮箱不匹配或代理 URL 含 embedded userinfo 时会失败。代理 username/password 不会进入 Chrome 参数或错误文本。对已经有 `verified_email` 绑定的账号，完成后再调用 profile validation endpoint；如果 lifecycle 尚未绑定身份，必须先通过生产 one-shot 验证路径或完整 onboarding 建立绑定，不能把 setup 的终端输出当作 lifecycle 已发布。

### 8.5 read-only patrol

```bash
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_patrol.py
```

对非默认数据库做只读检查：

```bash
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_patrol.py \
  --db "$FLOW2API_DB_PATH"
```

patrol 以 SQLite read-only mode 读取 `keepalive_enabled=1` 的 lifecycle telemetry，包括业务已禁用和 retired 账号；它不调用 Google、不修改数据库、不维护第二套告警状态。

对 `last_keepalive_status` 为 success/ok/alive 的记录，patrol 仍会检查 freshness，避免 sidecar 停止后历史成功永久显示健康：

- active 使用当前配置的 `browser_interval_seconds`；retired 使用当前配置的 `browser_retired_interval_seconds`，而不是在 patrol 中硬编码固定周期。
- 两类账号的 grace 都是所选 interval 的一半，并限制在最少 300 秒、最多 3600 秒。
- 当前时间超过 `last_keepalive_success_at + interval + grace` 时判定 `UNHEALTHY`；若存在 `next_due_at`，超过 `next_due_at + grace` 也判定 `UNHEALTHY`。
- 成功状态缺少成功时间、时间戳无法解析或 telemetry 转换失败属于 `PROBE_ERROR`。

退出码：`0` 表示所有 enabled 记录健康；`1` 表示至少一个明确 `UNHEALTHY` 且没有 probe error；`2` 表示存在 `PROBE_ERROR`，其优先级高于 unhealthy。空的 keepalive-enabled 集合返回 `1`。

## 9. systemd 与日志

安装或更新 unit：

```bash
sudo install -m 0644 \
  /opt/Projects/flow2api/flow2api-keepalive.service \
  /etc/systemd/system/flow2api-keepalive.service
sudo systemctl daemon-reload
sudo systemctl enable flow2api-keepalive.service
sudo systemctl restart flow2api-keepalive.service
```

查看状态和日志：

```bash
sudo systemctl status flow2api-keepalive.service --no-pager
sudo journalctl -u flow2api-keepalive.service -n 200 --no-pager
sudo journalctl -u flow2api-keepalive.service -f
```

同时查看主服务和 Xvfb：

```bash
sudo systemctl status flow2api.service xvfb@10.service --no-pager
sudo journalctl -u flow2api.service -n 200 --no-pager
sudo journalctl -u xvfb@10.service -n 100 --no-pager
```

验收日志必须出现 `headless=False`。unit 使用可选 `EnvironmentFile=-/etc/flow2api-keepalive.env`、`ExecStartPre --preflight`、显式 `--daemon`、`Restart=on-failure`、`UMask=0077` 与 `SIGTERM`。每个 runner 在收到停止请求后取消活动任务并以 20 秒边界排空浏览器/profile 资源；unit 的 `TimeoutStopSec=45s` 为整个 supervisor 留出更大的退出窗口。不要把 XRDP 加入 unit 的 `Requires=`。

## 10. 权限与机密管理

推荐检查：

```bash
sudo chown -R yufo:yufo /opt/flow2api-profiles
sudo chmod 0700 /opt/flow2api-profiles
sudo chmod 0600 /opt/Projects/flow2api/config/setting.toml
sudo chmod 0600 /opt/Projects/flow2api/data/flow.db
```

还需确认：

- 每账号 profile、`.onboarding`、`.archive` 和 `.flow2api-locks` 只对服务用户开放；服务创建目录使用 `0700`、lock 使用 `0600`。
- 不把 ST、AT、cookie、admin token、plugin connection token、webhook URL、profile 路径或完整 process cmdline 发到日志/工单。
- onboarding API response 不含 PID、start ticks、命令或路径；不要绕过 API 直接编辑 `onboarding_jobs`。
- XRDP 只绑定受控网络并使用强认证；非 provisioning 时限制访问或停止 XRDP。
- sidecar webhook 只放在 root-owned `0600` 的 `/etc/flow2api-keepalive.env`；`FLOW2API_CORS_ALLOWED_ORIGINS` 和其他服务机密也通过受保护的部署环境管理，任何真实值都不写入仓库或文档。
- 数据库备份、profile 归档和旧 unit 同样按敏感数据处理。

## 11. 维护窗口部署与验收

### 11.1 备份与基线

```bash
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/opt/flow2api-backups/keepalive-$TIMESTAMP"
sudo install -d -m 0700 -o yufo -g yufo "$BACKUP_DIR"
sqlite3 /opt/Projects/flow2api/data/flow.db ".backup '$BACKUP_DIR/flow.db'"
install -m 0600 /opt/Projects/flow2api/config/setting.toml "$BACKUP_DIR/setting.toml"
if [ -f /etc/systemd/system/flow2api-keepalive.service ]; then
  install -m 0600 /etc/systemd/system/flow2api-keepalive.service "$BACKUP_DIR/flow2api-keepalive.service"
fi
if [ -f /etc/flow2api-keepalive.env ]; then
  sudo install -m 0600 -o root -g root /etc/flow2api-keepalive.env "$BACKUP_DIR/flow2api-keepalive.env"
fi
```

记录不含凭据的基线：

```bash
sqlite3 -header -column /opt/Projects/flow2api/data/flow.db \
  "SELECT id,email,is_active,ban_reason,credits,user_paygate_tier,last_used_at,use_count FROM tokens ORDER BY id;"
```

对兼容账号保存 profile 元数据和备份；不要复制运行中的 profile。先关闭其 owner，再使用保留权限/扩展属性的备份工具。

### 11.2 停止与确认 profile 释放

```bash
sudo systemctl stop flow2api-keepalive.service
sudo systemctl stop flow2api.service
pgrep -af -- '--user-data-dir=/opt/flow2api-profiles/' || true
```

如果仍有进程，先确认 PID、start time 与完整 `--user-data-dir` 所有权；不要用模糊 kill。关闭正确的 GUI/XRDP Chrome 后再次确认。

### 11.3 安装依赖并运行迁移

```bash
/opt/Projects/flow2api/.venv/bin/pip install -r /opt/Projects/flow2api/requirements.txt
sudo systemctl start flow2api.service
sudo journalctl -u flow2api.service -n 200 --no-pager
```

验收 schema 和一对一 lifecycle：

```bash
sqlite3 -header -column /opt/Projects/flow2api/data/flow.db \
  "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('token_lifecycle','onboarding_jobs') ORDER BY name;"
sqlite3 -header -column /opt/Projects/flow2api/data/flow.db \
  "SELECT (SELECT COUNT(*) FROM tokens) AS tokens, (SELECT COUNT(*) FROM token_lifecycle) AS lifecycle_rows;"
```

确认 migration 没有改变现有凭据、业务状态、`last_used_at` 或 `use_count`。首次 legacy bootstrap 后，ID 23 应保持 `profile_state='ready'`、`runtime_mode='persistent'`、`keepalive_enabled=1`；其他未入库账号默认 disabled/warm/unprovisioned。当前 migration 不会从 `tokens.email` 自动填充 `token_lifecycle.verified_email`，因此需要单独检查该字段。

### 11.4 sidecar gate

确保 Xvfb `:10` 运行，并检查 legacy 生命周期。当前 migration 不会从 `tokens.email` 自动填充 `verified_email`，因此 legacy ID 23 的固定验收顺序是：**手工 one-shot → preflight → 启动 systemd sidecar**。不要先启动 unit；它的 `ExecStartPre` 会拒绝尚未建立绑定的 enabled profile。

```bash
sudo systemctl start xvfb@10.service
sqlite3 -header -column /opt/Projects/flow2api/data/flow.db \
  "SELECT t.id,t.email,l.verified_email,l.profile_state,l.runtime_mode,l.keepalive_enabled FROM tokens AS t JOIN token_lifecycle AS l ON l.token_id=t.id WHERE t.id=23;"
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_browser.py \
  --once --token-id 23
/opt/Projects/flow2api/.venv/bin/python \
  /opt/Projects/flow2api/scripts/keepalive_browser.py --preflight
```

one-shot 仍会把浏览器观察邮箱与 `tokens.email` 比对，并在完整原子快照成功后建立或刷新 `verified_email`。preflight 随后只做无凭据的运行时/profile 检查；两步都成功后才进入下一节安装或启动 unit。

验收 ID 23：

- one-shot 返回 0；
- 日志明确显示 `headless=False`；
- `verified_email` 与 Token 邮箱归一化后一致；
- credits 真实返回；
- ST/AT 更新只属于 ID 23；
- `last_used_at` 与 `use_count` 未变化；
- 人工、429 或其他 ban owner 未被清除；
- `next_due_at` 和 keepalive telemetry 已更新。

### 11.5 安装 unit 与观察

```bash
sudo install -m 0644 \
  /opt/Projects/flow2api/flow2api-keepalive.service \
  /etc/systemd/system/flow2api-keepalive.service
sudo systemctl daemon-reload
sudo systemctl enable --now flow2api-keepalive.service
sudo journalctl -u flow2api-keepalive.service -f
```

至少观察一次 ID 23 scheduled success，并确认周期仍为 1200 秒级相位、resident browser 使用 `:10`、没有第二个 process/profile owner。

### 11.6 API 与非 ID 23 pilot

1. 登录管理后台，检查 `/api/tokens` 不含 ST/AT。
2. XRDP display 不再通过 `/api/onboarding/config`（已 410）获取，直接用
   `scripts/tokens.py onboard --display :N` 显式传入（见 §7.5）；未传时回退到
   进程的 `$DISPLAY` 环境变量。
3. 调用 ID 23 profile validation，确认只读结果正确。
4. 对一个非 ID 23 账号执行 `scripts/tokens.py onboard --email xxx@gmail.com --display :N`
   完成 XRDP 入库（重登录已有账号用 `--token-id`），发布结果永远
   `runtime_mode="persistent"`——CLI 没有 `--mode` 参数，管理 API 也拒绝把账号
   设置为 `warm`。
5. 新账号只有精确 paid 时才请求 business enable；free/unknown 保持业务禁用。
6. 完成后确认无需重启 sidecar 即出现新 runner。
7. 观察新入库账号的 persistent Chrome 与 ID 23 各自独立刷新、互不影响（新隧道
   只发布 persistent，不会再出现"刷新后退出"的 warm Chrome）。
8. 检查项目池已补齐且有效 current project 未被无条件旋转。
9. 检查 onboard 输出（`scripts/tokens.py onboard` 的 JSON 与 systemd 日志）不含
   凭据、路径或 PID。
10. 再按账号逐个入库，避免并行 Google 登录触发风控。

## 12. ID 23 兼容基线

ID 23 是已验证的 headed/browser-cookie SQLite 兼容样本。部署期间：

- 保留 `/opt/flow2api-profiles/23` 与原本 Google 主登录态；
- legacy backfill 后保持 `persistent`、`ready`、`keepalive_enabled=1`，但需按上一节检查并建立 `verified_email`；
- 使用 active 1200 秒成功 cadence；默认初次未调度 due time 是 120 秒基础延迟加 stable stagger，ID 23 在默认 1200 秒 interval 下的 stagger 为 759 秒，因此首次自动 due 为启动后 879 秒；
- legacy 首次验证固定为 `--once --token-id 23` → `--preflight` → 启动 `flow2api-keepalive.service`，one-shot 先建立身份绑定；
- 必须从日志确认 `headless=False`；
- 在非 ID 23 pilot 完成前不要对该 profile 使用 `archive_and_replace`；
- 不要让 captcha profile、setup、XRDP 或第二个 daemon 占用 profile 23；
- 验收比较 `last_used_at` / `use_count`，确认 keepalive 没有污染业务统计。

ID 23 兼容性来自数据库 bootstrap 和相同生产刷新路径，不应通过硬编码专用账号逻辑扩展到其他 Token。

## 13. 回滚

发生身份错配、凭据写错号、profile owner 异常、迁移失败或业务池异常时：

1. 停止 `flow2api-keepalive.service`，再停止主服务。
2. 确认没有 Chrome 占用待恢复 profile。
3. 恢复上一版本代码和 `/etc/systemd/system/flow2api-keepalive.service`；如该版本使用可选 webhook 环境文件，同时恢复 root-owned `0600` 的 `/etc/flow2api-keepalive.env`，然后执行 `systemctl daemon-reload`。
4. 若 Token/lifecycle 状态发生错误，恢复维护窗口 SQLite backup；若只是不认识新增表，旧代码通常可忽略 additive tables。
5. 按需恢复 `setting.toml`。
6. 若 onboarding 使用 `archive_and_replace`，把当前目标 profile 移到隔离目录，再将 `.archive/<token_id>/<job-id>` 原子 rename 回 `<profile_base>/<token_id>`。不要覆盖已存在目录。
7. 先启动主服务并验证数据库，再恢复旧 keepalive 运行方式。
8. 对 ID 23 做一次旧路径健康验证，确认业务池和账号身份回到基线。

示例 profile 回滚前必须设置真实值并人工确认路径：

```bash
PROFILE_BASE="/opt/flow2api-profiles"
TOKEN_ID="${TOKEN_ID:?set TOKEN_ID}"
JOB_ID="${JOB_ID:?set JOB_ID}"
CURRENT="$PROFILE_BASE/$TOKEN_ID"
ARCHIVE="$PROFILE_BASE/.archive/$TOKEN_ID/$JOB_ID"
QUARANTINE="$PROFILE_BASE/.rollback-current-$TOKEN_ID-$JOB_ID"
test -d "$ARCHIVE"
test ! -e "$QUARANTINE"
mv "$CURRENT" "$QUARANTINE"
mv "$ARCHIVE" "$CURRENT"
```

回滚后先运行 profile validation 和 one-shot gate，再删除隔离目录。隔离和归档都包含敏感 cookie，不能直接上传或无保护保留。

## 14. 故障排查

| 现象 | 检查 | 处理 |
|---|---|---|
| preflight 输出 disabled 且返回 0 | `[keepalive].browser_enabled=false` | 这是预期行为；启用配置后再运行完整 preflight |
| preflight 报 `nodriver` / `browser_cookie3` 缺失 | `.venv` 与 requirements | 重新安装锁定依赖，确认 systemd 使用同一 `.venv` |
| `browser executable missing` | `BROWSER_EXECUTABLE_PATH`、文件权限 | 配置真实 Chrome Stable 路径并重启 unit |
| `X display unavailable: :10` | `xvfb@10.service`、`/tmp/.X11-unix/X10` | 修复 Xvfb，不改为 headless |
| profile missing/unprovisioned | lifecycle `profile_state`、目录、Cookies | 通过 XRDP onboarding 或 setup 建立并验证 profile |
| profile/service lease busy | 是否有 daemon、setup、XRDP Chrome | 关闭实际 owner；不要删 lock 或模糊 kill |
| SingletonLock unsafe | hostname、格式、PID/cmdline | 人工确认所有权；安全分类不明确时保留现场 |
| `identity_mismatch` / `target_identity_mismatch` | Token email、`verified_email`、当前 Google 账号 | 用正确目标 Token 重新登录；禁止把观察凭据写入错误账号 |
| `profile_identity_mismatch` | retained profile 与 lifecycle binding | 检查是否拿错 profile；通过受管重新登录修复 |
| `cookie_missing` | `Default/Cookies`、keyring、cookie 长度 | 确认 Chrome 完全退出、D-Bus/keyring env 正确，再重登录 |
| `session_rejected` | Flow 页面是否真实登录 | XRDP 打开 Flow 并重新授权 |
| `grant_expired` / `GRANT_EXPIRED` | credits 401、浏览器主登录态 | 对同一 Token 走 XRDP 重新登录；纯 HTTP ST 轮换不能修复 grant |
| `ST_REVOKED` | `st_to_at` 认证拒绝 | 重新登录并验证新的 per-token profile |
| proxy 报 `must not include userinfo` | `browser_proxy` 或账号 `captcha_proxy_url` 含 `user:password@` | 改用不含凭据的本地代理入口；不要把认证信息放进 Chrome proxy URL |
| `network` | 住宅代理、DNS、labs.google 连通性 | 修代理后等待指数退避或运行 one-shot gate |
| `membership_expired` 未恢复 | tier、candidate_count、ban_reason | 需要两次 paid 成功观察，且 ban_reason 必须仍为 `membership_expired` |
| 续费后仍是人工/429禁用 | ban owner | 保活按设计不会清除；由对应业务策略或管理员处理 |
| onboarding `active_job_exists` | running job 列表 | 完成/取消现有任务；系统只允许一个活动 XRDP job |
| `destination_conflict` | 目标 profile 已存在 | 优先验证旧 profile；确需替换才选择 `archive_and_replace` |
| `archive_conflict` | 目标 job archive 已存在 | 保留现场并人工审计，禁止覆盖归档 |
| `process_ownership_mismatch` | PID reuse、start ticks、cmdline | 不停止该 PID；先识别真实进程 owner |
| job 停在 `commit_complete` | 主服务日志、job state | 调用 finalize 或 recover，服务只补写 completion |
| CORS preflight 400 | 浏览器真实 Origin、env 覆盖 | 配置精确 Origin；确认没有路径和 `*`，重启主服务 |
| 插件 401 | connection token 与 Bearer header | CORS 正确后再核对插件 token，二者是独立检查 |
| patrol 报 success overdue | 最近成功已超过该账号当前配置的 interval + 计算所得 grace，或 `next_due_at` 已超过 grace | 核对 active/retired interval 配置；grace 为对应 interval 的一半并限制在 300–3600 秒。再检查 sidecar 是否停止、调度是否卡住及系统时间；历史成功不代表当前健康 |
| patrol 返回 1 | 明确 unhealthy、空 enabled 集合或成功 telemetry 已过 grace | 处理对应账号或恢复 sidecar；退出码 1 可用于告警 |
| patrol 返回 2 | DB/时间戳/telemetry 无法解析，或当前失败属于 probe error | 检查主服务迁移与 sidecar 日志；probe error 优先于 unhealthy，不把未知状态当健康 |
| systemd 停止接近超时 | 活动刷新或浏览器关闭阻塞 | runner 以 20 秒边界取消/排空，unit 最多等待 45 秒；随后检查残留进程的精确 profile 所有权 |

排障顺序建议：`systemctl status` → `journalctl` → `--preflight` → read-only patrol → profile validation → 有维护窗口时运行 `--once --token-id`。不要跳过身份验证直接修改 ST、`verified_email`、profile state 或 onboarding phase。
