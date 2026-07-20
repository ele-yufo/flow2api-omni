# Flow2API 浏览器保活与多账号入库 Handoff

> 日期：2026-07-20  
> 仓库：`/opt/Projects/flow2api`  
> 分支：`main`  
> 当前状态：大量本地改动未 commit、未 push；生产服务正在直接运行该工作区中的未提交代码。  
> 安全说明：本文不包含 ST、AT、Cookie、管理员密码、webhook URL、浏览器 PID、调试端口或命令行参数。

## 1. 原始需求

用户已有一个长期正常工作的浏览器保活账号：

- Token ID：`23`
- 邮箱：`ruby291464@gmail.com`
- 模式：headed Chrome + 独立持久化 profile
- 原始流程已经可以稳定完成：打开 Flow、读取 Chrome SQLite 中的 Labs session cookie、ST→AT、credits 验证、更新数据库。

用户的真实需求不是重写 Ruby 流程，而是：

1. 把 Ruby 已验证成功的服务器浏览器保活方式复制到更多账号。
2. 目标规模约 5–10 个 Google/Flow 账号。
3. 新账号通过服务器 XRDP 完成 Google 登录，之后不依赖用户本机浏览器。
4. 登录完成后自动识别真实邮箱、匹配或创建 Token、修复项目池、迁移 profile、加入保活。
5. 业务池启用和认证保活必须分离。
6. 会员过期账号退出业务池但低频保活；续费后满足条件再恢复。
7. 不允许保活逻辑清除人工禁用、429、`ST_REVOKED`、`GRANT_EXPIRED` 等其他 owner 的状态。
8. Ruby / ID 23 必须作为兼容基线保留，不能被新功能破坏。

用户确认的策略：

- 邮箱规范化仅使用 `strip().casefold()`，不移除 Gmail 点号，不移除 `+alias`。
- Paid→Free 连续两次才退休；retired→Paid 连续两次才恢复。
- ID 23 使用 `persistent`。
- 新账号默认 `warm`。
- active cadence 1200 秒。
- retired cadence 43200 秒。
- reconcile 15 秒。
- 浏览器 launch 并发 1，refresh 并发 1。
- 新账号只有在 exact identity、credits、tier、项目池和目标 profile 全部验证后才能正式发布。

批准过的实施计划保存在：

```text
/home/yufo/.claude/plans/shimmying-drifting-lynx.md
```

## 2. 最终生产账号状态

### 2.1 Ruby / Token 23

最后一次检查：

- `id=23`
- 邮箱：`ruby291464@gmail.com`
- `is_active=1`
- ban reason 为空
- `keepalive_enabled=1`
- `runtime_mode=persistent`
- `profile_state=ready`
- verified email 正确
- `flow2api-keepalive.service` 中有该 profile 的 headed Chrome 进程驻留
- 运行日志已确认 `headless=False`

Ruby 在改造和事故期间仍有真实 `generate_video` 流量。数据库请求日志显示维护期间存在连续的 200、500 和 102 请求。

### 2.2 新账号 / Token 21

最终已完成入库：

- Token ID：`21`
- 邮箱：`susilawatyelvis566@gmail.com`
- `is_active=1`
- ban reason 为空
- Tier：`PAYGATE_TIER_ONE`
- Credits：`1000`
- active projects：4
- `profile_state=ready`
- verified email 正确
- onboarding job：`completed/completed`
- onboarding conflict：`archived_and_replaced`
- onboarding job ID：`48d39985e90b45bdb87de826dd370a8f`
- 当前数据库 AT 最后一次验证：Google tokeninfo 200、credits 200
- 当前 `at_expires`：`2026-07-21 05:50:21+00:00`

**重要：Token 21 的自动 keepalive 当前被有意关闭。**

```text
keepalive_enabled=0
runtime_mode=warm
```

原因是：入库后的第一次 warm one-shot 曾把浏览器中的有效 Flow session 更新成一个 tokeninfo 400 / credits 401 的 session。为了先让用户立即使用该账号，最终重新完成 exact profile OAuth、写入有效凭据并启用业务，但没有再次执行浏览器 keepalive，避免再次破坏有效凭据。

当前 Token 21 没有 Chrome 进程驻留。业务可用，但持久化保活尚未完成最终无损验证。

### 2.3 当前 Chrome 进程

最后一次精确 profile 检查：

- Token 21 Chrome：0
- Token 23 Chrome：8 个同 profile Chrome 进程（persistent resident）
- onboarding 临时 profile Chrome：0

曾多次出现 onboarding Chrome 子进程没有随记录的主进程一起退出，最后已按 canonical profile + Chrome executable + start-ticks 逐个精确停止。没有使用 `pkill -f`。

## 3. 已实现的代码改造

### 3.1 数据库生命周期

新增：

- `token_lifecycle`
- `onboarding_jobs`
- 对应 model、repository、Database facade
- `BEGIN IMMEDIATE` 跨进程事务
- migration/backfill

主要文件：

- `src/core/database.py`
- `src/shared/db/engine.py`
- `src/core/models.py`
- `src/core/repositories/token_lifecycle_repository.py`
- `src/core/repositories/onboarding_job_repository.py`
- `src/core/repositories/token_repository.py`
- `src/core/token_states.py`
- `src/core/account_lifecycle.py`
- `src/core/account_identity.py`

实现了：

- keepalive desired state 与 `tokens.is_active` 分离
- exact tier classifier
- 两次观察迟滞
- `membership_expired` owner 保护
- manual / 429 / consecutive errors / ST revoked / grant expired 状态保护
- verified snapshot 原子写入
- ST collision 和 exact email 校验

### 3.2 账号身份、Token 和项目池

主要文件：

- `src/services/token_manager.py`
- `src/services/tokens/account_identity.py`
- `src/services/tokens/lifecycle.py`
- `src/services/tokens/project_pool.py`

实现了：

- ST 长度下限
- ST→AT
- exact email
- credits / tier 强验证
- rotated ST 选择
- verified snapshot 应用
- 项目池幂等修复
- 新 Token 与现有 Token 的 onboarding 合并路径

### 3.3 Keepalive package

新增目录：

- `src/services/keepalive/`

包括：

- `models.py`
- `profile.py`
- `refresher.py`
- `scheduler.py`
- `alerts.py`
- `supervisor.py`

实现了：

- headed nodriver Chrome
- SQLite Cookie 读取
- `persistent` / `warm`
- 数据库动态 reconcile
- 全局 launch / refresh semaphore
- profile `flock` lease
- PID + `/proc` start ticks + canonical user-data-dir ownership检查
- warm attempt 后关闭
- persistent runner 驻留
- failure telemetry 和 restart-safe alert episode

运行脚本和 unit：

- `scripts/keepalive_browser.py`
- `scripts/keepalive_gate_test.py`
- `scripts/keepalive_patrol.py`
- `scripts/setup_keepalive_profile.py`
- `scripts/setup_keepalive_profile.sh`
- `flow2api-keepalive.service`

### 3.4 XRDP onboarding

主要文件：

- `src/services/onboarding.py`
- `src/api/admin.py`

实现了：

- create/start/get/list/finalize/cancel/recover
- 临时 profile marker
- profile migration blocker / cleanup blocker
- archive-and-replace
- crash-forward recovery
- PID/start-ticks tuple CAS
- singleton owner adoption
- stale lock proof
- per-job cross-process operation lease
- profile lease
- start/finalize/cancel/recovery serialization
- conditional terminal state transitions
- retained-profile recovery
- 凭据安全响应

### 3.5 管理 API 和 UI

修改/新增：

- `src/api/admin.py`
- `src/main.py`
- `src/shared/config/cors.py`
- `static/manage.html`
- `static/manage-account-lifecycle.js`
- `static/manage-account-onboarding.js`

实现了：

- `/api/tokens` 不返回 ST/AT/token alias
- 独立 authenticated POST credential export
- lifecycle desired-state API
- onboarding API
- profile validate API
- onboarding modal 和状态展示
- exact CORS allowlist

### 3.6 配置、依赖、文档

修改/新增：

- `requirements.txt`
- `config/setting_example.toml`
- `README.md`
- `docs/architecture.md`
- `docs/operations/browser-keepalive.md`

依赖包含：

- `browser-cookie3==0.20.1`
- `nodriver==0.48.1`

## 4. 2026-07-20 最后新增但尚未完整验证的代码

最终发现 Chrome profile 根目录同时有：

```text
Default
Profile 1
```

用户实际登录产生的有效 Flow session 在 `Profile 1/Cookies`，而原实现固定读取：

```text
Default/Cookies
```

这正是反复拿到旧 session、tokeninfo 400、credits 401 的直接原因之一。

已在工作区修改：

- `src/services/keepalive/profile.py`
  - 新增 `_chrome_cookie_files()`
  - 读取 `Local State -> profile.last_used`
  - 优先读取 active Chrome profile，再 fallback `Default`
- `tests/test_keepalive_profile.py`
  - 新增 `test_cookie_reader_prefers_last_used_chrome_profile`

验证结果：

```text
4 cookie_reader focused tests passed
```

**但这最后一组修改没有重新跑全量测试、没有重新 code review，也没有重启 main/sidecar 加载。**

运行中的：

- `flow2api.service` 加载的是这次 profile-selection 修复之前的 Python 模块。
- `flow2api-keepalive.service` 同样加载的是修复之前的 Python 模块。

Token 21 的 profile 已手工归一化为 `Default`，因此当前运行时不依赖该新修复；但下一位 Agent 应完善并部署它。

## 5. Profile 21 的手工归一化

为了让旧运行代码也能读取正确 Cookie，执行了手工 profile normalization：

1. 验证 `Profile 1` 中的 session：
   - tokeninfo 200
   - `aisandbox` scope 存在
   - credits 200
   - Tier One
   - 1000 credits
2. 停止 exact onboarding Chrome profile。
3. 把旧 `Default` 原子重命名为：

```text
.Default-before-profile-normalization
```

4. 把 `Profile 1` 原子重命名为 `Default`。
5. 修改 `Local State`：
   - `profile.last_used = Default`
   - 更新 `last_active_profiles`
   - 更新 `profiles_order`
   - 把 `info_cache[Profile 1]` 移到 `info_cache[Default]`
6. 原 `Local State` 备份为：

```text
.Local State.before-profile-normalization
```

这些文件随后随 onboarding profile 一起迁移到 Token 21 的最终 profile。

## 6. 生产部署和直接变更

### 6.1 服务操作

曾执行：

- 停止 `flow2api-keepalive.service`
- 停止 `flow2api.service`
- 启动主服务
- 启动 sidecar
- 再次重启 sidecar
- 多次运行 ID 23 `--once`
- 手工修改 ID 23 `next_due_at` 触发调度验证

已知恢复时间：

- 主服务于 `2026-07-20 03:57:25 CST` 恢复 active
- sidecar 于 `03:59:34 CST` 启动
- sidecar 于 `04:03:18 CST` 再次重启

最后一次状态：

```text
flow2api.service: active
flow2api-keepalive.service: active
xvfb@10.service: active
```

### 6.2 数据库直接修改

直接做过：

- 把 stale onboarding job 从 `running/awaiting_login` 整理为 `failed/verify_account`
- 清除 stale browser tuple
- 把 onboarding conflict policy 从 `reject` 改为 `archive_and_replace`
- 临时修改 Token 23 `next_due_at` 触发调度
- Token 21 onboarding 完成后手工启用/禁用过 business 和 keepalive

最终 Token 21：business enabled，keepalive disabled。

### 6.3 OS 变更

为了操作 XRDP 中的精确 Chrome 窗口，安装了：

```text
xdotool
libxdo3
```

命令来源：Ubuntu apt。

### 6.4 备份

部署前备份：

```text
/opt/Projects/flow2api/.wm_dev/backups/browser-lifecycle-20260719T195249Z
```

包含：

- SQLite DB
- `setting.toml`
- `flow2api.service`
- `flow2api-keepalive.service`
- SHA256 checksums

数据库 integrity check 通过，权限已调整为 0600/0700。

**缺陷：该备份没有包含重启前正在内存中运行的未提交 Python 源码快照。**

因此它不是完整的 source-level 精确回滚包。

## 7. 完整失败记录与责任说明

以下是实际发生的失败，不能忽略：

### 7.1 严重范围失控

本应先复制 Ruby 最小流程到第二账号，却先做了：

- 数据库生命周期平台
- onboarding state machine
- 管理 API/UI
- systemd 产品化
- 多轮并发 hardening

这延迟了用户真正需要的“第二账号可用”交付。

### 7.2 重复要求用户登录

用户被要求重复登录/授权多次。主要原因不是用户操作失败，而是实现始终固定读取 `Default/Cookies`，而有效登录发生在 `Profile 1/Cookies`。

没有尽早检查 `Local State.profile.last_used` 和 profile 目录，是最关键的诊断失误。

### 7.3 操作了错误的 XRDP Chrome 窗口

XRDP `:11` 上同时有多个 Chrome 窗口。

最初用 `xdotool search ... | tail -1` 选择窗口，导致操作了普通 Chrome，而不是 onboarding profile Chrome。用户第一次输入密码发生在错误窗口。

后来才通过：

- `xdotool getwindowpid`
- 进程父链
- canonical profile marker

定位精确 onboarding/Token 21 窗口。

### 7.4 上游 OAuth 错误被误判为纯账号 grant 问题

多次观察到：

- `/auth/session` 200
- exact email 正确
- tokeninfo 400
- 无 `aisandbox` scope
- credits 401

这些事实是真的，但最初读取的是旧 `Default` session。正确的 `Profile 1` session 实际为 tokeninfo 200 / credits 200。

因此“账号 grant 本身坏掉”不是完整结论；错误 profile 选择才是直接原因。

### 7.5 生产停机前没有重新确认实时流量

用户早先说过业务暂时不用，但维护前没有重新检查实时请求。

维护后发现 Token 23 在整个期间存在真实 `generate_video` 流量。这说明把早先的停机许可当作持续有效授权是错误的，确实影响了生产业务。

### 7.6 不必要的 sidecar 重启和调度触发

为了验收 persistent runner，执行了：

- 手工 one-shot
- 修改 `next_due_at`
- sidecar restart

其中部分是重复和不必要的。

### 7.7 错误的 persistent ownership 检查

`inspect_singleton_lock()` 对 nodriver 修改后的 Chrome argv 形态返回 stale，但 systemd cgroup 中实际有 profile 相关 Chrome 进程。

最初监控因此产生了 false negative。后来改用 systemd cgroup + profile marker 统计进程确认 Ruby resident Chrome 存在。

下一位 Agent 应检查 `verify_process_ownership()` 对 nodriver argv0 的兼容性。

### 7.8 onboarding finalize 未停止全部 Chrome 子进程

多次 finalize 后，记录的 browser identity 已清空，但 profile 相关 Chrome 子进程仍然存在。

手工通过：

- Chrome executable 校验
- exact profile marker
- start ticks 重验
- 先 SIGTERM，超时才 SIGKILL

进行了清理。

最后 onboarding orphan count 已为 0，但代码层面仍需修复“停止整个 owned Chrome process tree / cgroup”的问题。

### 7.9 Token 21 第一次 warm one-shot 破坏了有效 session

Token 21 onboarding 首次成功后，运行了一次：

```text
scripts/keepalive_browser.py --once --token-id 21
```

结果：

- `grant_expired`
- profile session 变成 tokeninfo 400 / credits 401

为避免继续破坏，已关闭 Token 21 keepalive。

随后在 exact Token 21 profile 中重新完成 Google OAuth，停止 Chrome、读取有效 session、通过管理 API 严格更新凭据，并重新启用业务。

最后没有再次执行 one-shot。

### 7.10 回滚资料不完整

虽然备份了 DB/config/unit，但没有保存重启前未提交源码的完整 snapshot。这个缺口必须写进事故记录。

### 7.11 webhook 未轮换

曾有一个 Discord webhook 在 operator-visible 输出中暴露。

- 本文不重复 URL。
- 当前没有完成 Discord 侧 rotation/revocation。
- 下一位 Agent 应要求用户在 Discord 侧旋转，不能从日志中复制旧 URL 继续使用。

## 8. 测试和 review 状态

在最后 profile-selection 修改之前：

```text
649 passed, 1 skipped, 54 subtests passed
```

相关 focused suites：

```text
107 onboarding tests passed
24 lifecycle characterization tests passed
```

经历多轮 code review，最终 onboarding concurrency review 当时没有 surviving finding。

但在上述全量测试之后，又修改了：

- `src/services/keepalive/profile.py`
- `tests/test_keepalive_profile.py`

最后只运行：

```text
pytest tests/test_keepalive_profile.py -k cookie_reader
4 passed
```

因此下一位 Agent 必须：

1. review `_chrome_cookie_files()`。
2. 补 malformed/missing `Local State`、unsafe profile name、active profile missing、fallback Default 测试。
3. 跑完整 `scripts/test.sh`。
4. 在维护窗口内重启 main/sidecar 后验证。

## 9. 当前 Git 工作区

当前在 `main`，没有 commit/push。

Tracked modifications：

```text
README.md
config/setting_example.toml
docs/architecture.md
requirements.txt
src/api/admin.py
src/core/database.py
src/core/models.py
src/core/repositories/token_repository.py
src/main.py
src/services/token_manager.py
src/shared/config/provider.py
src/shared/db/engine.py
static/manage.html
tests/characterization/test_config_clamp.py
tests/golden/db_token_crud.json
tests/test_alert_triggers.py
tests/test_st_rotation.py
```

主要 untracked 产品文件：

```text
docs/operations/
flow2api-keepalive.service
scripts/keepalive_browser.py
scripts/keepalive_gate_test.py
scripts/keepalive_patrol.py
scripts/setup_keepalive_profile.py
scripts/setup_keepalive_profile.sh
src/core/account_identity.py
src/core/account_lifecycle.py
src/core/repositories/onboarding_job_repository.py
src/core/repositories/token_lifecycle_repository.py
src/core/token_states.py
src/services/keepalive/
src/services/onboarding.py
src/services/tokens/account_identity.py
src/services/tokens/lifecycle.py
src/services/tokens/project_pool.py
src/shared/config/cors.py
static/manage-account-lifecycle.js
static/manage-account-onboarding.js
大量新增测试文件
```

原本就存在且不得误删/误提交的无关文件：

```text
.antigravitycli/
KEEPALIVE_TASK.md
```

`config/setting.toml` 没有出现在 git status 中，不要修改或提交。

Tracked diff stat（不含大量 untracked 文件）：

```text
17 files changed, 2141 insertions(+), 567 deletions(-)
```

## 10. 下一位 Agent 的优先级

### P0：保持 Token 21 立即可用

- 不要立即开启 Token 21 keepalive。
- 不要运行 Token 21 browser one-shot，除非先做 profile 和 session 的只读备份，并准备恢复。
- 当前 DB credentials 最后验证 tokeninfo 200 / credits 200。
- 当前 `at_expires` 为 `2026-07-21 05:50:21+00:00`。
- 先验证真实业务请求能否使用 Token 21。

### P0：修复 Token 21 无损保活

需要查明为什么在第一次 onboarding 成功后，warm browser launch 会把 session 变成无效 bearer。

建议最小诊断：

1. 复制 Token 21 profile 到受保护测试副本，不直接实验生产 profile。
2. 记录启动前的 cookie creation metadata 和 tokeninfo/credits 状态，不记录 token 内容。
3. 用与 Ruby 完全相同的 browser argv、display、proxy 和导航 URL 启动副本。
4. 导航后再次比较 session cookie metadata、tokeninfo 和 credits。
5. 确认是否是：
   - OAuth callback/session rotation
   - active profile 选择
   - Chrome `Local State`
   - 深链导航
   - proxy/display 环境
   - Chrome profile recovery popup
6. 只有副本连续成功后才对 Token 21 开启 keepalive。

### P0：不要破坏 Ruby

- ID 23 仍在生产承载业务。
- 不要停止或重启 sidecar，除非先确认实时请求和维护窗口。
- 每次部署后必须从运行日志确认 `headless=False`。

### P1：完成 active Chrome profile 支持

- 完善 `_chrome_cookie_files()`。
- 全量测试和 review。
- 设计是否在 onboarding 时直接拒绝非 Default profile，还是正式支持 `Profile N`。
- 更简单的长期方案可能是 onboarding 启动时显式加：

```text
--profile-directory=Default
```

这样从源头禁止 profile 漂移，比事后扫描更容易验证。但需要兼容已有 profile 并测试。

### P1：修复 Chrome process tree 停止

- 不能只假设记录的主 PID 退出就代表整个 profile 没有 Chrome。
- 需要在 profile lease 下枚举并验证同 canonical profile 的 Chrome process tree。
- 使用 start ticks / pidfd；禁止 `pkill -f`。
- terminal transition 前确保没有 profile-owned descendant。

### P1：整理提交

在 commit 前：

- 重新跑全量测试。
- review untracked 文件。
- 不要使用 `git add .`。
- 不要加入 `.antigravitycli/`、`KEEPALIVE_TASK.md`、生产配置、DB、profile、备份或临时截图。
- 按 feature 分批 stage。
- 用户此前没有授权 commit/push；需要新的明确授权。

### P1：轮换 webhook

必须在 Discord 侧创建新 webhook 并撤销旧 webhook。不要把 URL 写进仓库、命令行、本文或聊天。

## 11. 建议的安全检查

不含凭据的检查：

```bash
systemctl is-active flow2api.service flow2api-keepalive.service xvfb@10.service
sqlite3 /opt/Projects/flow2api/data/flow.db 'PRAGMA quick_check;'
/opt/Projects/flow2api/scripts/test.sh
```

Token 21 状态应满足：

```text
id=21
is_active=1
ban_reason=NULL
keepalive_enabled=0
runtime_mode=warm
profile_state=ready
verified_email=susilawatyelvis566@gmail.com
```

Token 23 应满足：

```text
id=23
is_active=1
keepalive_enabled=1
runtime_mode=persistent
profile_state=ready
verified_email=ruby291464@gmail.com
```

不要在普通日志或 handoff 中输出：

- ST
- AT
- Cookie
- admin token/password
- webhook
- browser PID/start ticks
- remote debugging port
- profile 内部凭据文件内容

## 12. 最诚实的完成度结论

已完成：

- 数据库驱动生命周期框架
- XRDP onboarding 框架
- 管理 API/UI
- Ruby 保活继续运行
- 新账号 Token 21 已入库并进入业务池
- Token 21 当前凭据有效
- 项目池和 profile migration 完成

未完成：

- Token 21 的持久化浏览器保活尚未安全验证
- active profile 支持修改尚未全量测试/部署
- Chrome descendant cleanup 尚未在代码中彻底解决
- webhook 未轮换
- 变更未 commit/push
- 缺少完整 source rollback snapshot

本任务的核心失败是：没有先按 Ruby 最小流程快速交付第二账号，而是过早平台化；同时因为读取错误 Chrome profile 和操作错误 XRDP 窗口，让用户重复登录并造成了生产干扰。下一位 Agent 应以最小变更、可回滚、先副本验证为原则继续。