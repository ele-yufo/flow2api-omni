# Discord 运维告警 — 设计文档

- 日期：2026-06-18
- 状态：已认可，待实现
- 背景关联：ST 自我续命功能（`reference_st_self_renewal_model`）落地后，需要在"账号需人工维护"等问题发生时主动通知运维者。

## 1. 背景与目标

15 个 Pro 账号池跑起来后，"某个账号失效需重登""池子快空了"这类需要人工介入的事件，目前只写日志、不会主动通知。目标：**问题发生时把告警投递到 Discord webhook**，让运维者无需盯日志。

现有 `token_manager._send_st_alert` 发的是通用 JSON（`{event, token_id, email}`），**Discord webhook 不认这个格式**（Discord 要 `{"content": ...}` 或 `{"embeds": [...]}`），直接对接会静默失败。本功能重构告警投递为 Discord 兼容，并扩展触发范围。

## 2. 架构（职责分离）

新增独立模块 `src/services/alert_notifier.py`，只负责"怎么发"；`token_manager` 只负责"何时/为何发"。告警渠道未来可替换而不动业务逻辑。

```
token_manager(决定事件 + 去重)  ──>  AlertNotifier(Discord embed 格式化 + POST + 容错)  ──>  Discord
```

### `alert_notifier.py`
- `build_discord_payload(title, description, fields, severity) -> dict`：**纯函数**，产出 Discord webhook body（一个 embed），可独立单测。
  - `fields`: `list[tuple[name, value, inline]]` 或 `list[dict]`。
  - `severity` ∈ {`critical`, `warning`}，映射 embed `color`：critical=红(15158332)、warning=橙(15105570)。
  - 含 `username`（"Flow2API 哨兵"）、`timestamp`（ISO8601，由调用方传入或函数内 `datetime.now(timezone.utc)`）。
- `class AlertNotifier`：
  - `__init__(self, webhook_url: str)`
  - `async send_alert(self, title, description, fields=None, severity="warning") -> bool`：尽力投递。
    - `webhook_url` 为空 → 只 `debug_logger` 记日志、返回 False（保留现有降级行为）。
    - POST 用 `curl_cffi.AsyncSession`，**直连不带任何代理**（Discord 非 Google，不走住宅代理），`timeout=10`。
    - 任何异常都 swallow + 记日志、返回 False —— **告警失败绝不能影响主流程**。

## 3. 三类告警（均带去重，不刷屏）

| 事件 | 触发点 | 去重 | severity |
|---|---|---|---|
| **账号失效需重登** | `_handle_refresh_failure` 中"本次新确认 ST_REVOKED"时 | 已有 `was_revoked_before` 快照保证只在跨越时触发 | critical |
| **账号池告急** | `disable_token` 执行后，活跃账号数 ≤ `alert_pool_low_threshold` | 内存标志 `_pool_low_alerted`：跨越阈值发一次；`enable_token`/`add_token` 使活跃数回到阈值以上时复位 | critical |
| **单账号额度耗尽** | `_do_refresh_at` 刷新成功后，credits 由 `> min_credits_to_select` 跌到 `<= min_credits_to_select` | 只在该"足→不足"跨越发一次（比较刷新前后 credits） | warning |

每条消息字段：账号 email、token_id、原因、**建议操作**、时间。建议操作示例：
- 失效：`重新登录 labs.google/fx/tools/flow，在后台「添加账号」粘贴该号 cookies.txt`
- 池告急：`当前可用账号仅剩 N 个，请尽快补充新 Pro 账号`
- 额度耗尽：`该账号额度已用尽，请充值或更换`

## 4. 配置

均在 `[admin]` 段（`config/setting.toml` + `setting_example.toml` + `config.py` property）：

- `alert_webhook_url`：**优先读环境变量 `FLOW2API_ALERT_WEBHOOK_URL`**，其次读 toml `[admin] alert_webhook_url`，默认空。
  - 实现：`config.py` 顶部 `import os`；property 先 `os.environ.get("FLOW2API_ALERT_WEBHOOK_URL")`，回退 toml。
  - 把今天刚加的 `st_alert_webhook_url` **重命名为 `alert_webhook_url`**（语义已通用化）；同步更新 `setting.toml`/`setting_example.toml`/README/memory `reference_st_self_renewal_model`。
- `alert_pool_low_threshold`：默认 `2`。活跃账号数 ≤ 此值时触发"池告急"。

## 5. token_manager 集成

- `__init__`：新增 `self._pool_low_alerted = False`。
- 新增私有 helper `async def _alert(self, title, description, fields=None, severity="warning") -> None`：
  从 `config.alert_webhook_url` 构造 `AlertNotifier` 并 `send_alert`；整体 try/except 包裹，**绝不向上抛**。
- **失效告警**：`_handle_refresh_failure` 的 `newly_revoked` 分支，把现有 `await self._send_st_alert(token_id)` 替换为 `await self._alert(...)`（构造失效消息）。删除 `_send_st_alert`（其逻辑并入 `_alert` + 调用点）。
- **池告急**：`disable_token` 在 `update_token(is_active=False)` 后，统计 `len(await self.db.get_active_tokens())`；若 `<= config.alert_pool_low_threshold and not self._pool_low_alerted` → `_alert(...)` 并置 `_pool_low_alerted=True`。
- **池恢复复位**：`enable_token` 与 `add_token` 成功后，若活跃数 `> threshold` 则 `self._pool_low_alerted=False`。
- **额度耗尽**：`_do_refresh_at` 成功分支，在更新 credits 前读一次 token 拿 `prev_credits`（与现有"清 ST_REVOKED"读 token 合并为一次读），更新后若 `prev_credits > floor >= new_credits` → `_alert(...)`。

## 6. 错误处理与边界

- 告警投递全程 best-effort：webhook 不可达 / 4xx / 超时 → 记日志、不影响刷新/禁用/生成主流程。
- 空 `alert_webhook_url` → 全部降级为只记日志（与现状一致），不报错。
- 去重状态为进程内存：服务重启后标志复位，最坏情况是重启后某条告警可能再发一次，可接受。
- 池告急在 `disable_token` 触发；该方法也被 ST_REVOKED 路径调用，故账号失效时可能同时收到"失效"+"池告急"两条——这是两种不同信息，符合预期。

## 7. 测试

- **单元**：
  - `build_discord_payload`：embed 结构、severity→color、fields 映射、title/description 注入。
  - `AlertNotifier.send_alert`：空 URL→只记日志不 POST；有 URL→调用 session.post（mock）并传 Discord body；POST 抛异常→返回 False 不上抛。
  - 触发去重：额度"足→不足"跨越发一次、已不足再刷新不重发；池告急跨越发一次、再次 disable 不重发、回升复位后能再发；ST_REVOKED 仍发。
- **端到端（真实 Discord）**：用真实 webhook 发一条测试告警，确认频道里能看到（投递链路打通）。

## 8. 部署（密钥落地，不进 git）

webhook URL 通过环境变量提供给运行中的 systemd 服务（`flow2api.service`）：用 systemd drop-in（`/etc/systemd/system/flow2api.service.d/override.conf` 设 `Environment=FLOW2API_ALERT_WEBHOOK_URL=...`）或 `EnvironmentFile`。需要 sudo 写 `/etc`；若无权限则交给用户执行一条命令。受跟踪文件中只保留 `alert_webhook_url = ""`（无密钥）。

## 9. 明确不做（YAGNI）

- ❌ 告警历史持久化 / 告警中心 UI
- ❌ 多渠道（钉钉/飞书/邮件）—— 留好 `AlertNotifier` 边界即可，本期只 Discord
- ❌ 告警分级订阅 / 静默规则
- ❌ 对自动恢复的 429 限流告警（噪音）
