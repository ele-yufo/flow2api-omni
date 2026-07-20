# Flow2API 保活/入库 技术债清理 Handoff

> 日期：2026-07-21
> 仓库：`/opt/Projects/flow2api`，分支 `main`，已 push 到 `be7902c`
> 前置阅读：`docs/browser-account-lifecycle-handoff-2026-07-20.md`（上一次事故记录）、`docs/superpowers/specs/2026-07-20-multi-account-keepalive-onboarding.md`（隧道设计）
> 本文目的：功能已跑通，但代码是两拨实现的混合体。这里给出**可直接开工的清理方案**，不是泛泛建议。

---

## 0. 先读这段：当前是什么状态

**功能是好的，别推翻重来。** 生产上 4 个账号自动保活稳定运行，重启自愈已实测。清理的目标是**删除死代码、消除重复、修正边界**，不是重写。

**动手前必须知道的红线：**
- `src/services/keepalive/`（保活引擎）是生产命脉，Token 23 靠它活着。**除非有实测证据，否则不要动。**
- 任何改动后必须验证：`systemctl is-active flow2api-keepalive.service` + 日志里 `headless=False` + `token_lifecycle.last_keepalive_success_at` 在推进。
- `config/setting.toml` 是生产配置（untracked），改之前备份到 `.wm_dev/backups/`。
- 不要提交 `.antigravitycli/`、`KEEPALIVE_TASK.md`。

---

## 1. 债务全貌（附实测数据）

### 1.1 代码规模

| 文件 | 行数 | 性质 |
|---|---:|---|
| `src/services/onboarding.py` | 2810 | **死代码**（HTTP 全 410，但仍被实例化）|
| `tests/test_onboarding.py` | 4566 | **死代码**（测一个已禁用的功能）|
| `src/api/admin.py` | 1849 | 混合：10 处 410 stub + 活跃路由 |
| `src/core/repositories/onboarding_job_repository.py` | 422 | **死代码** |
| `src/core/repositories/token_lifecycle_repository.py` | 838 | 活跃，但混了 publisher（新）+ 平台化遗产（旧）|
| `src/services/keepalive/supervisor.py` | 800 | 活跃（生产命脉，谨慎）|
| `src/services/tokens/onboard.py` | 570 | 新写，活跃 |
| `scripts/tokens.py` | 421 | 新写，活跃 |
| `scripts/setup_keepalive_profile.py` | 338 | 活跃，但与 onboard.py 职责重叠 |

**死代码合计约 7800 行**，占本功能相关代码的一半以上。

### 1.2 三类债务

**A. 死代码仍在运行时装配**
`src/main.py:60` 仍在 `OnboardingService(...)` 实例化，`src/api/admin.py:21,54,251,471` 仍持有它的类型和单例。所有 HTTP 入口已返回 410（`admin.py` 中 10 处 `onboarding_deprecated`），但对象照建、依赖照注入。

**B. 重复/混淆的模块**
- `src/core/account_identity.py`（33 行：`VerifiedAccountSnapshot`、`normalize_account_email`）
- `src/services/tokens/account_identity.py`（107 行：`AccountIdentityError`、`inspect_account_identity`）
  同名不同物，import 时极易搞错。我在写隧道时就踩过。
- `scripts/setup_keepalive_profile.py` 与 `src/services/tokens/onboard.py` 职责重叠：后者从前者 import `SetupRuntime` / `build_browser_command`（`onboard.py:52-55`）——**一个 src/ 模块反向依赖 scripts/**，是错误的依赖方向。

**C. 前端残留**
`static/manage-account-onboarding.js`（261 处 onboarding 引用）仍被 `manage.html` 引用，UI 上的「重登」按钮会打开一个必然 410 失败的 modal。操作员点了只会看到报错，且没有任何指引说"改用 `scripts/tokens.py onboard`"。

---

## 2. 清理方案（按顺序执行，每步可独立验证）

### 第 1 步：删除 onboarding 死代码（收益最大，风险可控）

**删除：**
- `src/services/onboarding.py`（2810 行）
- `src/core/repositories/onboarding_job_repository.py`（422 行）
- `tests/test_onboarding.py`（4566 行）
- `tests/test_admin_onboarding_disabled.py` 中针对 410 的测试（整个功能没了，410 stub 也该删）
- `static/manage-account-onboarding.js` + `manage.html` 中对它的引用
- `src/api/admin.py` 中 10 处 410 stub 路由 + `OnboardingService` import/单例/依赖注入（`:21,54,251,434,471`）
- `src/main.py:19,60` 的 import 与实例化

**保留（不要连坐删掉）：**
- `POST /api/tokens/{id}/validate-profile` —— 只读 profile 校验，与状态机无关，仍有用
- `PUT /api/tokens/{id}/lifecycle` —— desired-state API，`scripts/tokens.py` 之外的备用入口
- `POST /api/tokens/{id}/export` —— 凭据导出

**数据库：**`onboarding_jobs` 表当前有 1 行（上个 agent 遗留的 failed job）。建议保留建表语句但停止写入，或写一个明确的 migration 删表——**你来定，别默默 drop**。

**验证：**`pytest` 全绿；`systemctl restart flow2api` 后 `/api/tokens` 正常；保活不受影响。

### 第 2 步：消除 account_identity 重复

把 `src/core/account_identity.py` 的内容并入 `src/services/tokens/account_identity.py`，或反过来——**选一个，只留一个**。改完全局搜 `account_identity` 确认没有残留的双路径 import。

注意 `src/core/models.py` 可能也导出 `VerifiedAccountSnapshot`，一并核对。

### 第 3 步：修正 scripts/ ↔ src/ 依赖方向

`src/services/tokens/onboard.py` 不该从 `scripts/` import。把 `SetupRuntime`、`build_browser_command`、`resolve_runtime`、`resolve_display`、`canonical_token_id` 下沉到 `src/services/tokens/browser_launch.py`（新建），然后：
- `onboard.py` 从 src/ 内部 import
- `scripts/setup_keepalive_profile.py` 改为薄 wrapper 调用 src/

注意 `onboard.py:26` 有个 `PROJECT_ROOT = parents[3]` 的 sys.path hack，下沉后应该能删掉——**删之前确认 `scripts/tokens.py` 仍能独立运行**（它是 CLI 入口，不走 uvicorn 的 import 路径）。

### 第 4 步：拆分 admin.py（1849 行）

删完 onboarding 部分后会瘦一圈，再评估是否需要拆。若拆，按域分：token CRUD / lifecycle / 配置 / 日志。**不要为拆而拆**——第 1 步之后可能已经够用。

### 第 5 步（可选）：token_lifecycle_repository.py 分层

838 行里混着：`publish_verified_account`（新，隧道用）、`apply_verified_snapshot`（旧，保活用）、迟滞逻辑、telemetry。可以按"读/写/纯策略"分文件，但**这个文件是保活写库的唯一入口，改动风险最高，放最后做，且要有完整测试保护**。

---

## 3. 已知遗留问题（清理时顺手修）

| 问题 | 位置 | 说明 |
|---|---|---|
| `awaiting_login` 无条件打印 | `scripts/tokens.py` `_cmd_onboard` | 旧号走免登录快路径时也打印"请去登录"，误导人。应只在真要开浏览器时打印 |
| 幂等测试名不副实 | `tests/test_publish_verified_account.py` | `test_publish_second_leg_failure_is_idempotent_on_retry` 实际只测重复成功调用，没测失败重试 |
| `banned_at` 不一致 | `token_lifecycle_repository.publish_verified_account` | 转入 `manual_disabled` 时不写 `banned_at`，与 `finalize_onboarding_state` 行为不一致 |
| `_poll_profile_lease` 阻塞 | `src/services/tokens/onboard.py` | 用 `time.sleep(1)` 最多 40s，阻塞事件循环。CLI 独立进程跑无害，但若将来被服务端复用会出问题 |
| `--display` 不读配置 | `scripts/tokens.py` | 只读 `$DISPLAY`，不读 `keepalive.onboarding_display`（`:11`）。**每次 onboard 必须手动传 `--display :11`**，否则可能开在 sidecar 的 `:10` 上不可见 |
| 孤儿目录 | `/opt/flow2api-profiles/.onboarding/48d39985...` | 上个 agent 遗留的失败 job 临时 profile，确认无用后可删 |
| `docs/architecture.md` | — | 已标注 onboarding 路由 410，但删完代码后需再更新 |

---

## 4. 账号与生产现状（清理时别破坏）

**保活中（4 个，业务池可用 3 个）：**

| Token | 邮箱 | Credits | 备注 |
|---|---|---:|---|
| 21 | susilawatyelvis566@gmail.com | 978 | |
| 23 | ruby291464@gmail.com | 10 | **额度耗尽，已被路由排除**（`min_credits_to_select=20`），保活继续 |
| 27 | ele.yufo@gmail.com | 573 | |
| 28 | phamthithibich476@gmail.com | 993 | |

**已停用（5 个）：**

| Token | 邮箱 | 原因 |
|---|---|---|
| 18 | gentanaka606@gmail.com | 授权过期，待重登录 |
| 19 | phamthithibich476@gmail.com | **与 28 重复**（同一账号旧记录）|
| 22 | ele.yufo@gmail.com | **与 27 重复**（同一账号旧记录）|
| 24 | langenaekencolette134@gmail.com | ST_REVOKED |
| 26 | ranjitmoh219@gmail.com | 授权过期，**用户密码丢失**，找回后用 `--token-id 26` 重登录 |

> 19/22 是历史重复记录。清理时可考虑合并或删除，但**先确认 27/28 稳定运行一段时间后再动**，它们的 profile 是回滚余地。

**服务：** `flow2api.service` / `flow2api-keepalive.service` / `xvfb@10.service` 全 active，均 `enabled` + `Restart=always`。

---

## 5. 清理时的验证清单

每完成一步：

```bash
# 1. 测试
/opt/Projects/flow2api/.venv/bin/pytest

# 2. committed tree 能否独立导入（关键！上次就栽在这）
cd /tmp && rm -rf tc && mkdir tc && cd tc
git -C /opt/Projects/flow2api archive HEAD | tar x
PYTHONPATH=. /opt/Projects/flow2api/.venv/bin/python -c "import src.api.admin, src.main, src.services.tokens.onboard, scripts.tokens; print('OK')"

# 3. 保活未受影响
systemctl is-active flow2api-keepalive.service
/opt/Projects/flow2api/.venv/bin/python /opt/Projects/flow2api/scripts/tokens.py status

# 4. 重启主服务后 API 正常（sidecar 不用重启）
sudo systemctl restart flow2api.service && sleep 8 && systemctl is-active flow2api.service
```

**验收标准：** 测试全绿 + committed tree 可导入 + 4 个号 `last_keepalive_success_at` 持续推进 + `tokens status` 输出正常。

---

## 6. 建议的工作方式

- **一步一 commit**，每步独立可回滚。删 7800 行死代码不该和重构混在一个 commit 里。
- **先删后改**：第 1 步删完，代码量少一半，后面的重构会容易得多。
- 删代码前先跑一次 `grep -rn "<符号名>" src/ scripts/ tests/` 确认无引用——这次就发现 `main.py` 还在实例化一个"已废弃"的服务。
- 保活引擎（`src/services/keepalive/`）除非有实测证据支撑，否则本轮不要动。它现在工作正常，而它一旦坏了，所有账号一小时内授权全灭。

---

## 7. 本次 session 已完成的事（供追溯）

16 个 commit（`6e1b56a..be7902c`），要点：
- 新建入库隧道（`scripts/tokens.py` + `src/services/tokens/onboard.py` + `publish_verified_account`），绕开 2810 行状态机
- 禁用 onboarding HTTP surface（410）
- **修复生产事故**：keepalive sidecar 因 `Restart=on-failure` + 干净退出而静默停摆 3.5 小时 → 改 `Restart=always`
- **修复 git 树不自洽**：上个 agent 的代码从未提交，committed tree 无法导入（部署会导致主服务和 sidecar 双双崩溃）
- **修复路由缺陷**：`min_credits_to_select` 1 → 20，额度耗尽账号不再被优先选中
- 入库 3 个账号（21 免登录 / 27 / 28），全部自动保活
- 重启自愈能力实测验证
