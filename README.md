# FlowProxy

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-supported-green.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-748%20passed-brightgreen.svg)](#开发与测试)

**Google Flow（flow.google.com）媒体生成的反向代理 —— OpenAI 兼容 + Gemini 官方格式的本地 API**

</div>

**FlowProxy** 把 Google Flow 的图片 / 视频生成封装为本地 HTTP API（生产端口 **18282**）：图片族 `gemini-3.2-flash-image`（Nano Banana 2.1，0 额度），视频族 `gemini_omni_*`（Gemini Omni 1.1 Flash，T2V / R2V / 首尾帧 / 视频延长）。项目名遵循 X-proxy 反向代理项目的命名惯例（2026-10 由旧名更名而来，历史见文末[致谢](#致谢)）。

它是一个**私有单操作者项目的生产后端**，不是公共服务：私有仓库 `ele-yufo/flowproxy`，生产部署只有一套（2080TI 主机，systemd 常驻，`/opt/Projects/flowproxy`），为个人媒体生成管线供水。维护者就是自己（或接手的 agent），无公共 issue 入口；读者默认是未来的你，重点写给「要日常运维这套服务的人」。

## 速查表

日常最常用的 10 条（CLI 均在 `/opt/Projects/flowproxy` 下执行）：

| 场景 | 命令 / 地址 |
|---|---|
| 服务状态 | `systemctl status flowproxy.service flowproxy-keepalive.service` |
| 重启主服务 / 保活 | `sudo systemctl restart flowproxy.service`（keepalive 同理） |
| 跟踪日志 | `journalctl -u flowproxy.service -f`（同步落盘仓库根 `logs.txt`） |
| 账号池健康 | `.venv/bin/python scripts/tokens.py status`（JSON，永不打凭据） |
| 池子诊断 | `.venv/bin/python scripts/keepalive_patrol.py` |
| 模型目录 | `curl -s -H "Authorization: Bearer $FLOWPROXY_KEY" http://localhost:18282/v1/models` |
| 管理后台 | `http://localhost:18282/manage`（默认 admin/admin，部署后立即改密） |
| 模型测试页 | `http://localhost:18282/test` |
| 服务地址（全场景） | `192.168.124.151:18282`（在家直连；在外经 Tailscale 子网路由） |
| 保活运维手册 | [`docs/operations/browser-keepalive.md`](docs/operations/browser-keepalive.md) |

## 架构

代码分层（依赖方向 `api → services → core → shared`，`shared/` 不反向依赖业务层，由测试守卫）：

```text
src/
├── shared/     通用配置、SQLite 引擎、存储、鉴权与基础工具
├── core/       数据模型、schema、迁移与 repositories
├── services/   生成、Flow 客户端、Token、验证码、保活业务逻辑
├── api/        FastAPI 路由、管理 API 与协议转换
└── main.py     应用组合根
```

运行时拓扑（生产机的真实形态）：

```text
HTTP / 管理后台 ──▶ flowproxy.service（FastAPI + TokenManager，:18282）
                        │ SQLite/WAL（data/flow.db：tokens / token_lifecycle / projects）
                        ▼ 动态 reconcile（每 15s 读 desired state）
Xvfb :10 ◀── flowproxy-keepalive.service（有头 Chrome sidecar，逐账号刷会话）
XRDP :11 ◀── scripts/tokens.py onboard（仅入库/重登录时人工 Google 登录）
```

数据库是账号状态的唯一权威：业务池启停（`tokens.is_active` / `ban_reason`）与保活 desired state（`token_lifecycle`）解耦。完整分层、事务与生命周期设计见 [`docs/architecture.md`](docs/architecture.md)。

## 日常运维

### systemd 单元一览

| Unit / 文件（宿主 `/etc/systemd/system/`） | 作用 |
|---|---|
| `flowproxy.service` | 主服务：`.venv/bin/python main.py`，`Restart=always`，`Upholds=flowproxy-keepalive.service` |
| `flowproxy.service.d/alert-webhook.conf` | 注入 `FLOWPROXY_ALERT_WEBHOOK_URL`（Discord 告警 webhook，不进 git） |
| `flowproxy.service.d/browser-keyring.conf` | 注入 `DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR`（有头 Chrome 解 keyring 用） |
| `flowproxy.service.d/captcha-cleanup.conf` | 主服务停止时 `ExecStopPost` 运行 `scripts/cleanup_captcha_chrome.py --terminate` 清理打码 Chrome（仓库源文件 `config/systemd/flowproxy-captcha-cleanup.conf`） |
| [`flowproxy-keepalive.service`](flowproxy-keepalive.service)（仓库文件即部署源，与宿主一致） | 保活 sidecar：`ExecStartPre` 先 `--preflight` 再 `--daemon`；`Requires=xvfb@10.service`；可选读取 root 0600 的 `/etc/flowproxy-keepalive.env`（只放 webhook 密钥）；`Restart=always`（干净退出也拉起——曾因 status=0 不触发 on-failure 静默死过数小时） |
| `flowproxy-healthcheck.timer` + `.service` | 每小时（每小时 :07 UTC，`Persistent=true`）跑 `scripts/keepalive_healthcheck.py` 巡检 |
| `flowproxy-post-reboot-check.service` | 重启后一次性自检（组网/服务/IPv6/GPU，机器专属，脚本在 `.wm_dev/`） |
| `xvfb@.service`（实例 `xvfb@10.service`） | 虚拟显示器模板；`xvfb@10` 即保活 sidecar 的 `:10` |

```bash
# 日常操作
sudo systemctl restart flowproxy.service
sudo systemctl restart flowproxy-keepalive.service
journalctl -u flowproxy.service -f          # 主服务日志；logs.txt 同步落盘在仓库根
```

### 自动巡检与健康告警

- `flowproxy-healthcheck.timer` 每小时跑一次巡检：账号失联、保活僵死等异常**即时推送 Discord**；默认全绿不响（静默即健康），需要全绿心跳时设 `FLOWPROXY_HEALTHCHECK_HEARTBEAT_HOURS`（如 `"0,12"`，UTC 时刻）。
- Discord webhook 优先读环境变量 `FLOWPROXY_ALERT_WEBHOOK_URL`（本机放 alert-webhook drop-in，root 0600，不进 git），回落 `[admin].alert_webhook_url`。
- 告警事件：账号失效需重登、活跃池低于 `alert_pool_low_threshold`（默认 2）、单账号额度耗尽。
- 池子健康的手动诊断口径：读 `last_keepalive_status` + `at_expires`（UTC），不要读 `last_failure_code`（历史残留）；工具 `scripts/keepalive_patrol.py`。

### 账号操作 CLI（`scripts/tokens.py`，JSON 输出，Agent 友好）

```bash
VENV=/opt/Projects/flowproxy/.venv/bin/python

$VENV scripts/tokens.py status                          # 全部保活账号健康总览
$VENV scripts/tokens.py onboard --email new@gmail.com --display :11   # 新账号入库（XRDP 前台登录）
$VENV scripts/tokens.py onboard --token-id 21 --display :11           # 已有账号重新登录
$VENV scripts/tokens.py reauth --token-id 21            # 静默重授权（cookie 重放，免登录）
$VENV scripts/tokens.py enable  --token-id 21           # 加入业务池
$VENV scripts/tokens.py disable --token-id 21           # 移出业务池（不影响保活）
$VENV scripts/tokens.py keepalive --token-id 21 on      # 打开保活（persistent 模式）；off 关闭
```

入库/重登录必须在 XRDP 对应的 `--display :11` 上做（sidecar 占用 `:10`，开错显示器会不可见）；CLI 会在登录后完成身份核验、项目池补齐、profile 原子迁移与发布。旧的 Web 端入库状态机已删除，其路由固定返回 `410 Gone`。

## 浏览器保活与账号生命周期

为什么需要浏览器：Google OAuth 授权寿命约 1 小时，仅靠接口轮换 ST 救不了授权过期（库里 token 没到期、实际调用 401 的 `GRANT_EXPIRED` 状态）。生产保活是「有头 Chrome 刷新 + 严格身份校验 + 原子写回」：

1. sidecar 在 Xvfb `:10` 上用每账号独立 profile（`/opt/flowproxy-profiles/<token_id>`）访问 Flow 页与 auth session；
2. 校验浏览器会话邮箱与 Token 绑定邮箱一致；
3. 从 profile 的 Chrome cookie 库读取轮换后的 ST，并用会话 AT 调真实 credits 接口读取精确 tier；
4. `BEGIN IMMEDIATE` 事务原子写回 ST/AT/有效期/credits/tier 与生命周期遥测。

关键设计（详见运维手册 [`docs/operations/browser-keepalive.md`](docs/operations/browser-keepalive.md)）：

- **`token_lifecycle` 独立管理保活**：`keepalive_enabled` 与 `runtime_mode`（`persistent` = 刷新后保留浏览器和 profile lease；`warm` = 到期启动、刷完即关）与业务池启停解耦；sidecar 每 15 秒从数据库 reconcile，改库即生效，不用重启 unit。
- **周期**：活跃会员 1200 秒；退休会员 43200 秒低频维护登录态。
- **会员过期**：连续两次 credits 观察为 free → 退休并 `ban_reason=membership_expired` 自动摘池；续费后连续两次 paid 才条件恢复，且不会误清人工禁用/429/连续错误等其他禁用原因。
- **静默重授权（2026-09 上游迁移的救命路径）**：上游迁移曾致全池 `GRANT_EXPIRED`，此时**不要让用户重登 Google**——用账号 profile 里的 Google cookie 通过 HTTP 重放 next-auth 登录即可自愈，已集成进 keepalive、业务刷新路径与 `tokens.py reauth`。
- **配额耗尽双信号摘除**：上游报账号级配额耗尽时打时间标记（不动 credits/is_active），冷却窗口内且 credits 未回涨则不路由；月度充值回涨、窗口内成功一次或标记到期都能自愈回池。

### Chrome 扩展入口（当前在用）

上游配套的 Token-Updater Chrome 扩展（上游原名见[致谢](#致谢)）通过 `POST /api/plugin/update-token` 显式提交账号凭据，使用独立 connection token 的 `Authorization: Bearer <token>` 认证；跨域调用需把扩展的精确 `chrome-extension://<扩展ID>` Origin 加入 `[server].cors_allowed_origins`（本机生产配置已加）。它不替代每账号的浏览器保活 profile。

### 远程访问

服务地址恒为 `192.168.124.151:18282`（在家直连；在外经 Tailscale 子网路由，直连依赖 IPv6）。组网细节与产物中转口径见 [`docs/operations/remote-access.md`](docs/operations/remote-access.md)。

## 部署

### systemd（生产方式，本机实际运行形态）

服务跑在仓库内的 `.venv` 上，端口由 `config/setting.toml` 的 `[server].port` 决定（当前 **18282**）。首次部署：

```bash
cd /opt/Projects/flowproxy
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 准备 config/setting.toml（见下），装好 Xvfb（xvfb@10.service）与 XRDP
sudo cp flowproxy-keepalive.service /etc/systemd/system/
sudo systemctl enable --now xvfb@10.service flowproxy.service
sudo systemctl enable flowproxy-keepalive.service flowproxy-healthcheck.timer
sudo mkdir -p /etc/systemd/system/flowproxy.service.d
sudo cp config/systemd/flowproxy-captcha-cleanup.conf /etc/systemd/system/flowproxy.service.d/
sudo systemctl daemon-reload && sudo systemctl restart flowproxy.service
```

### Docker（备选）

```bash
docker compose up -d --build    # 使用仓库根 Dockerfile 就地构建，不拉任何外部镜像
```

- `docker-compose.yml` 挂载 `./data`、`./tmp`、`./config/setting.toml`；容器内应用端口跟随配置里的 `[server].port`（仓库当前为 18282），`ports` 映射右侧的容器端口必须与之一致，左侧宿主端口自定。
- 需要容器内有头打码（browser/personal 模式）时用 `docker-compose.headed.yml`（容器内 Xvfb + Fluxbox，不开放远程桌面端口）；`docker-compose.local.yml` 是等价的本地构建变体。
- **浏览器保活不在 Docker 里**：sidecar 依赖宿主 Xvfb/XRDP 与 systemd，Docker 适合无保活的轻量部署。

## 配置

配置文件 `config/setting.toml`（git 跟踪，即本机生产配置）；完整默认键位与注释见 [`config/setting_example.toml`](config/setting_example.toml)（其 `[keepalive]` 块被 `tests/test_keepalive_packaging.py` 锁定）。

```toml
[global]
api_key = "$FLOWPROXY_KEY"            # 调用 /v1/* 与 Gemini 端点的 Bearer key
admin_username = "admin"       # 管理后台登录（默认 admin/admin，部署后立即改密）
admin_password = "admin"

[server]
host = "0.0.0.0"
port = 18282
# 跨域 Origin 精确 allowlist（不支持 *），Chrome 扩展入口需要；也可用
# 环境变量 FLOWPROXY_CORS_ALLOWED_ORIGINS（逗号分隔）覆盖
cors_allowed_origins = ["chrome-extension://<扩展ID>"]

[call_logic]
call_mode = "default"          # default=随机轮询 + 层级优先；polling=顺序轮询
prefer_higher_tier = true      # false 恢复纯负载均衡
min_credits_to_select = 20     # 余额 ≤ 此值的账号退出候选池
quota_exhausted_cooldown_seconds = 43200   # 配额耗尽标记的摘除窗口（默认 12h）

[captcha]
captcha_method = "personal"    # 生产路径：持久化登录态打码；yescaptcha/capmonster/ezcaptcha/capsolver/remote_browser 为休眠备选
persistent_profile_enabled = true
persistent_profile_path = "/opt/flowproxy-profiles/ultra"
personal_max_resident_tabs = 5       # 常驻打码标签页上限（每 tab 约 200-300MB）
personal_min_resident_tabs = 3
personal_idle_tab_ttl_seconds = 600

[token]
st_keepalive_enabled = true          # 旧版 HTTP ST 巡检；浏览器保活启用后由 token_lifecycle 接管
st_browser_refresh_enabled = false   # 旧版共享浏览器刷新（多账号写错号），仅兼容保留

[keepalive]
browser_enabled = true
browser_interval_seconds = 1200      # 活跃会员刷新周期；grant 寿命约 1h，20min 留 3x 余量
browser_token_ids = "23"             # 仅旧部署首次迁移用；此后以数据库动态管理
browser_profile_base = "/opt/flowproxy-profiles"   # 每账号子目录 = {token_id}
browser_proxy = "http://127.0.0.1:7890"           # 必须与登录时同一出口（住宅 IP）
browser_display = ":10"
browser_settle_seconds = 8.0
# 初始延迟、退休间隔、并发上限、三层超时兜底等其余键走内置缺省，完整清单见 setting_example.toml
```

相关环境变量：`FLOWPROXY_ALERT_WEBHOOK_URL`（Discord 告警 webhook，优先于 `[admin].alert_webhook_url`）、`FLOWPROXY_CORS_ALLOWED_ORIGINS`、`FLOWPROXY_HEALTHCHECK_HEARTBEAT_HOURS`、`BROWSER_EXECUTABLE_PATH`（Chrome 路径，默认 `/usr/bin/google-chrome-stable`）。密钥类只放 `/etc/flowproxy-keepalive.env` 或 systemd drop-in，不进 git。

### personal 打码 profile 的一次性登录

```bash
sudo systemctl stop flowproxy        # 释放 profile
google-chrome --user-data-dir=/opt/flowproxy-profiles/ultra --profile-directory=Default \
  --proxy-server=http://127.0.0.1:7890
# 在打开的 Chrome 里登录目标 Google 账号 → 访问 https://flow.google.com/ 确认能进 → 关闭
sudo systemctl start flowproxy
```

换账号同流程（停服 → 登出旧号登新号 → 启服）。GUI Chrome 未退出就启服会触发 `SingletonLock` 硬错误。注意这套 captcha profile 与每账号的 keepalive profile 是**两套独立资源**，不要混用。健康度以一次真实生成成功为最终验收（`logs.txt` 中 `Token 获取成功 (长度: NNNN)` 的长度骤降 + `PUBLIC_ERROR_UNUSUAL_ACTIVITY` 是失联信号）。

## 模型目录

`/v1/models` 共 **80** 个模型 = 15 图片 + 65 视频（2026-10 目录收敛：只保留下列两族 SOTA，veo / imagen / 360P / 720P 原生档等旧模型已永久移除）。

### 图片：Nano Banana 2.1（0 额度）

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `gemini-3.2-flash-image-landscape` | 图/文生图 | 横屏 |
| `gemini-3.2-flash-image-portrait` | 图/文生图 | 竖屏 |
| `gemini-3.2-flash-image-square` | 图/文生图 | 方图 |
| `gemini-3.2-flash-image-four-three` | 图/文生图 | 横屏 4:3 |
| `gemini-3.2-flash-image-three-four` | 图/文生图 | 竖屏 3:4 |
| `gemini-3.2-flash-image-{aspect}-2k` | 图/文生图(2K)，上述 5 种画幅同后缀 | 同上 |
| `gemini-3.2-flash-image-{aspect}-4k` | 图/文生图(4K，5504×3072)，上述 5 种画幅同后缀 | 同上 |

上游枚举 `BELUGA`；产品名 Nano Banana 2.1。2K 档会优先分散路由给 Pro 账号。

### 视频：Gemini Omni 1.1 Flash（上游 family `abra`）

4 种 video_type × 横竖屏 × 4 个时长档（每个时长是独立模型）× `_1080p`/`_4k` 两档 = 64 个，外加 `gemini_omni_edit`。横竖屏共享上游 `model_key`，仅请求体 `aspectRatio` 区分。

| 任务类型 | 模型名称形态 | 输入图 |
|---|---|---|
| 文生视频 T2V | `gemini_omni_t2v_{4,6,8,10}s_{1080p,4k}`（竖屏加 `_portrait`） | 无 |
| 多图参考 R2V | `gemini_omni_r2v_{4,6,8,10}s_{1080p,4k}`（竖屏加 `_portrait`） | 最多 **7 张** |
| 首帧图生视频 I2V | `gemini_omni_i2v_{4,6,8,10}s_{1080p,4k}`（竖屏加 `_portrait`） | 恰好 1 张 |
| 首尾帧 | `gemini_omni_fl_{4,6,8,10}s_{1080p,4k}`（竖屏加 `_portrait`） | 恰好 2 张（首帧+尾帧，按顺序） |
| 视频延长/编辑 | `gemini_omni_edit` | 1 条源视频引用 |

**高清档说明**：两档均为「720P 原生生成 + 上采样」两步链，产物分辨率即后缀标称——上游没有原生 1080P/4K 生成。

| 后缀 | 输出 | 费用 | 账号要求 |
|------|------|------|------|
| `_1080p` | 1080P | 原生费 + 0 | Pro 及以上 |
| `_4k` | 4K | 原生费 + 50 | **仅 Ultra** |

原生 720P 生成费：4s=7 / 6s=10 / 8s=12 / 10s=15 额度（Pro/Ultra 同价）。

### `gemini_omni_edit`（视频延长/编辑）

把一条已生成视频作为输入，附延长/编辑指令，输出固定 10s 720P 新视频（Pro 账号 20 额度/次），可链式延长。源视频引用放在消息里 `{"type": "video_url", "video_url": {"url": "<引用>"}}`，引用支持上游 media id（含 `_upsampled` 后缀）或本服务上次响应里的 `/tmp/` 视频 URL（自动提取 media id 反查）。宽高比/时长自动继承源视频；媒体按账号隔离，跨账号引用上游一律失败。Gemini 格式用 `{"fileData": {"mimeType": "video/mp4", "fileUri": "<引用>"}}`。

> 实测耗时（持久化登录态 + 住宅 IP 代理）：T2V 4s ≈ 45s、T2V 10s ≈ 50s、R2V 4s ≈ 60s、T2V 4s + 1080P 上采样 ≈ 80s。

## API 使用

所有端点都要求流式（`stream: true` / `:streamGenerateContent`）。以下示例统一用本机生产端口 **18282**，API key 为 `[global].api_key`。

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容 |
| `GET /v1/models`（及 `/v1/models/aliases`） | 模型目录 |
| `POST /models/{model}:generateContent`、`/v1beta/models/{model}:generateContent`（及 `:streamGenerateContent`） | Gemini 官方格式；认证支持 `Authorization: Bearer`、`x-goog-api-key`、`?key=` |

### 调用方

各机器上的 Agent 通过共享 Skill **`flowproxy`** 调用本服务（t2i / i2i / t2v / r2v / i2v / edit 自动路由到正确模型），日常生成优先走 Skill 而不是手写 curl；Skill 源在 `~/.agents/skills/flowproxy/`，跨机由 skills 体系分发。直接 HTTP 调用按下面示例。

### 文生图（OpenAI 格式）

```bash
curl -X POST "http://localhost:18282/v1/chat/completions" \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.2-flash-image-landscape",
    "messages": [{"role": "user", "content": "一只可爱的猫咪在花园里玩耍"}],
    "stream": true
  }'
```

### 图生图

```bash
curl -X POST "http://localhost:18282/v1/chat/completions" \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.2-flash-image-landscape",
    "messages": [{"role": "user", "content": [
      {"type": "text", "text": "将这张图片变成水彩画风格"},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<base64_encoded_image>"}}
    ]}],
    "stream": true
  }'
```

### Gemini 官方 generateContent

```bash
curl -X POST "http://localhost:18282/models/gemini-3.2-flash-image-square:generateContent" \
  -H "x-goog-api-key: $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "一颗放在木桌上的红苹果，棚拍光线，极简背景"}]}],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"}
    }
  }'
```

流式把路径换成 `:streamGenerateContent?alt=sse`。请求体支持 `systemInstruction`、`contents[].parts[].text/inlineData/fileData`。

### 文生视频

```bash
curl -X POST "http://localhost:18282/v1/chat/completions" \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini_omni_t2v_10s_1080p",
    "messages": [{"role": "user", "content": "一只小猫在草地上追逐蝴蝶，柔和阳光"}],
    "stream": true
  }'
```

### 首尾帧视频（2 张图按顺序：首帧、尾帧）

```bash
curl -X POST "http://localhost:18282/v1/chat/completions" \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini_omni_fl_8s_1080p",
    "messages": [{"role": "user", "content": [
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,<首帧>"}},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,<尾帧>"}},
      {"type": "text", "text": "镜头从首帧平滑推进到尾帧，海浪持续拍打沙滩"}
    ]}],
    "stream": true
  }'
```

### 多图视频（R2V，最多 7 张参考图）

```bash
curl -X POST "http://localhost:18282/v1/chat/completions" \
  -H "Authorization: Bearer $FLOWPROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini_omni_r2v_portrait_8s_4k",
    "messages": [{"role": "user", "content": [
      {"type": "text", "text": "以参考图的人物和场景为基础，生成一段镜头平滑推进的竖屏视频"},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<参考图1>"}},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<参考图2>"}}
    ]}],
    "stream": true
  }'
```

服务端自动组装新版视频请求体并映射最新上游模型键；参考图最多传 7 张。

### 视频延长 / 编辑

```bash
curl -X POST http://localhost:18282/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini_omni_edit",
    "messages": [{"role": "user", "content": [
      {"type": "text", "text": "Extend this video seamlessly: the wave keeps rolling slowly"},
      {"type": "video_url", "video_url": {"url": "http://localhost:18282/tmp/<上次生成的视频文件>"}}
    ]}],
    "stream": true
  }'
```

### Web 界面

- 管理后台：`http://localhost:18282/manage`（文件为 `static/manage.html`；未登录时访问 `/` 即登录页 `/login`，默认 admin/admin，**首次登录后立即改密**）——Token 管理、系统配置、请求日志
- 测试页：`http://localhost:18282/test`——按分类浏览模型、上传图片、流式预览生成结果

## 开发与测试

- Python 3.11+（Dockerfile 用 `python:3.11-slim`，本机 `.venv` 为 3.13；依赖均为现代锁定版本）。
- 测试全离线、锁定项目 `.venv`，唯一正确入口：

```bash
bash scripts/test.sh                              # 全量（748 passed, 1 skipped, 53 subtests）
bash scripts/test.sh tests/test_keepalive_documentation.py   # 单文件
REGEN_GOLDEN=1 bash scripts/test.sh tests/characterization/test_poll_video_result.py  # 行为有意变更后重放 golden
```

- 测试以 **golden 特征化（characterization）** 为主，锁定重构前后行为等价；`REGEN_GOLDEN=1` 重新捕获基线，务必 review diff。
- 两个架构守卫：`tests/characterization/test_shared_extractability.py`（`shared/` 不依赖业务模块）与 `test_no_undefined_names.py`（pyflakes 扫描防漏带 import）。
- 修改对外接口、schema、环境变量、命令或目录结构时同步本 README 与 `docs/`。

## 致谢

本项目前身为私有仓库 `flow2api-omni`，2026-10 更名为 FlowProxy（沿用 X-proxy 反向代理命名惯例）。它派生自 **TheSmallHanCat** 的开源项目 **flow2api**（MIT 许可证，上游已停止维护）：最初的 Flow 逆向调用、验证码处理框架与 Web 管理界面均来自上游，配套的 Token-Updater Chrome 扩展（上游原名 Flow2API-Token-Updater）同样出自上游作者。本项目按 MIT 条款继续使用其代码，上游版权声明完整保留在 [LICENSE](LICENSE) 文件中。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
