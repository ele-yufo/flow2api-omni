# Flow2API

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**一个功能完整的 OpenAI 兼容 API 服务，为 Flow 提供统一的接口**

</div>

## 核心特性

- **文生图** / **图生图**
- **文生视频** / **图生视频** / **多图视频**
- **首尾帧视频**
- **视频放大** (1080P / 4K)
- **视频延长 15s** — 生成 8s + 延长 8s + 拼接（跳过 1s 重叠），对上游透明
- **Gemini Omni Flash (abra)** — 新一代视频模型，T2V/R2V × 4 个时长档位（4/6/8/10s）× 横竖屏 × 原版/1080P 上采样，共 32 个变体
- **持久化登录态打码** — `personal` 模式可绑定固定 Chrome profile，复用用户登录态 cookie 提交 reCAPTCHA，把 `PUBLIC_ERROR_UNUSUAL_ACTIVITY` 拒绝率从匿名态 30%+ 降到个位数
- **浏览器验证式账号保活** — 每个 Token 绑定独立持久化 Chrome profile；有头浏览器刷新 Flow 会话后，服务校验邮箱、读取 SQLite 中轮换后的 ST、验证 AT 与 credits，再以原子快照写回数据库
- **数据库驱动的账号生命周期** — `token_lifecycle` 独立保存保活开关、`persistent` / `warm` 运行模式、会员状态、调度与失败遥测；业务池启停与认证保活互不替代
- **余额感知调度** — 负载均衡自动跳过剩余额度 ≤ `min_credits_to_select`（默认 1）的账号，多账号池耗尽账号自动退出轮询
- **Discord 运维告警** — 账号失效需重登 / 账号池告急 / 单账号额度耗尽时主动推送到 Discord webhook（带去重），无需盯日志
- **余额显示** - 实时查询和显示 VideoFX Credits
- **负载均衡** - 多 Token 轮询和并发控制
- **代理支持** - 支持 HTTP/SOCKS5 代理
- **Web 管理界面** - 直观的 Token 和配置管理
- **图片生成连续对话**
- **Gemini 官方请求体兼容** - 支持 `generateContent` / `streamGenerateContent`、`systemInstruction`、`contents.parts.text/inlineData/fileData`
- **Gemini 官方格式已实测出图** - 已使用真实 Token 验证 `/models/{model}:generateContent` 可正常返回官方 `candidates[].content.parts[].inlineData`

## 快速开始

### 前置要求

- Docker 和 Docker Compose（推荐）
- 或 Python 3.8+

- 由于Flow增加了额外的验证码，你可以自行选择使用浏览器打码或第三发打码：
注册[YesCaptcha](https://yescaptcha.com/i/13Xd8K)并获取api key，将其填入系统配置页面```YesCaptcha API密钥```区域
- 默认 `docker-compose.yml` 建议搭配第三方打码（yescaptcha/capmonster/ezcaptcha/capsolver）。
如需 Docker 内有头打码（browser/personal），请使用下方 `docker-compose.headed.yml`。

- Chrome 扩展（**可选**）：[Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater) 可用于显式提交账号凭据，但不替代每账号浏览器保活 profile。跨域调用 `/api/plugin/update-token` 时，必须把扩展的精确 `chrome-extension://<扩展ID>` Origin 加入 CORS allowlist，并继续使用插件 connection token 的 Bearer 认证。

### 方式一：Docker 部署（推荐）

#### 标准模式（不使用代理）

```bash
# 克隆项目
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

> 说明：Compose 已默认挂载 `./tmp:/app/tmp`。如果把缓存超时设为 `0`，语义是"不自动过期删除"；若希望容器重建后仍保留缓存文件，也需要保留这个 `tmp` 挂载。

#### WARP 模式（使用代理）

```bash
# 使用 WARP 代理启动
docker-compose -f docker-compose.warp.yml up -d

# 查看日志
docker-compose -f docker-compose.warp.yml logs -f
```

#### Docker 有头打码模式（browser / personal）

> 适用于你有虚拟化桌面需求、希望在容器里启用有头浏览器打码的场景。
> 该模式默认启动 `Xvfb + Fluxbox` 实现容器内部可视化，并设置 `ALLOW_DOCKER_HEADED_CAPTCHA=true`。
> 仅开放应用端口，不提供任何远程桌面连接端口。

```bash
# 启动有头模式（首次建议带 --build）
docker compose -f docker-compose.headed.yml up -d --build

# 查看日志
docker compose -f docker-compose.headed.yml logs -f
```

- API 端口：`8000`
- 进入管理后台后，将验证码方式设为 `browser` 或 `personal`

### 方式二：本地部署

```bash
# 克隆项目
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

### 首次访问

服务启动后,访问管理后台: **http://localhost:8000**,首次登录后请立即修改密码!

- **用户名**: `admin`
- **密码**: `admin`

### 模型测试页面

访问 **http://localhost:8000/test** 可打开内置的模型测试页面，支持：

- 按分类浏览所有可用模型（图片生成、文/图生视频、多图视频、视频放大等）
- 输入提示词一键测试，流式显示生成进度
- 图生图 / 图生视频场景支持上传图片
- 生成完成后直接预览图片或视频

## 配置进阶

### 持久化登录态打码（推荐 `personal` 模式启用）

匿名态向 Google reCAPTCHA Enterprise 提交时拒绝率较高（频繁触发 `PUBLIC_ERROR_UNUSUAL_ACTIVITY`）。开启后，nodriver 浏览器复用固定的 `user-data-dir`，里面保留用户一次性手动登录的 Google 账号 cookie，reCAPTCHA 按"已登录账号"评分，token 长度从 ~2200 跳到 ~2300+，拒绝率显著下降。

#### 配置项（`config/setting.toml`）

```toml
[captcha]
captcha_method = "personal"
persistent_profile_enabled = true
persistent_profile_path = "/opt/flow2api-profiles/ultra"
```

#### 一次性登录步骤

```bash
# 1) 停服释放 profile
sudo systemctl stop flow2api

# 2) 用 GUI Chrome 打开同一个 profile 路径登录
google-chrome --user-data-dir=/opt/flow2api-profiles/ultra
# 在打开的 Chrome 里：登录 Google 账号 → 访问 https://labs.google/fx/tools/flow 确认能进 → 关闭

# 3) 启服，nodriver 自动复用此 profile
sudo systemctl start flow2api
```

启动日志看到下面这行即为生效：

```
[BrowserCaptcha] ✅ 持久化 profile 已检测到登录痕迹 (.../Default/Cookies)
```

#### 更换账号

同上流程：停服 → GUI Chrome 登出旧账号登入新账号 → 启服。

> ⚠️ GUI Chrome 没退出就启服会触发 `SingletonLock` 硬错误，启动日志里有清晰报错。

#### 工作机制（健康度判定）

`personal` 打码完成后，reCAPTCHA token 长度是登录态生效与否的物理信号：

| 状态 | Token 长度 |
|---|---|
| 匿名 / 未登录 | ≤ 2240 |
| 持久化登录态生效 | ≥ 2295（实测分布 2297-2425） |

在 logs.txt 里搜 `Token 获取成功 (长度: NNNN)` 即可判断。如果开启了持久化但长度仍 ≤ 2240，说明 cookie 没生效或 profile 缺关键 token（SID/HSID/SAPISID/__Secure-1PSID 等），需要重新 GUI 登录。

### 浏览器保活与多账号生命周期

#### 为什么不能把纯 HTTP 轮换视为永久保活

`st_to_at` 在 Google 授权仍有效时可以轮换 ST，但它不能保证背后的 Google OAuth 授权无限存活。授权过期时可能出现“ST 仍可兑换响应，但 `get_credits` 返回 401”的 `GRANT_EXPIRED` 状态；ST 本身被撤销则是 `ST_REVOKED`。因此生产保活采用**浏览器支持、身份验证后再写库**的流程，而不是把一次 ST 注入视为永久凭据。

每个保活账号使用独立目录 `<browser_profile_base>/<token_id>`。有头 Chrome 访问账号的 Flow 项目与 `/fx/api/auth/session`，服务随后：

1. 校验浏览器会话邮箱与 Token 邮箱、`verified_email` 绑定一致；
2. 从该 profile 的 Chrome SQLite cookie 库读取长度合格的轮换 ST；
3. 用浏览器会话 AT 调用真实 credits 接口并读取精确 tier；
4. 在 `BEGIN IMMEDIATE` 事务中原子写入 ST、AT、有效期、credits、tier 和生命周期遥测。

身份不匹配、ST 与其他账号冲突、cookie 缺失、credits 验证失败时不会写入部分凭据。保活也不会修改业务请求统计 `last_used_at` / `use_count`。

#### 业务池与保活 desired state 分离

- `tokens.is_active` 与 `tokens.ban_reason` 决定账号是否进入图片/视频业务负载均衡。
- `token_lifecycle.keepalive_enabled` 决定浏览器 sidecar 是否继续维护该账号，即使账号已退出业务池。
- `token_lifecycle.runtime_mode` 支持两种浏览器生命周期：
  - `persistent`：刷新后保留浏览器和 profile lease，适用于已验证的兼容基线账号。
  - `warm`：到期时启动，完成一次刷新后关闭浏览器并释放 lease，适用于普通多账号运行。
- 默认成功周期为活跃会员 **1200 秒**；已退休会员仍以 **43200 秒**低频维护登录态。sidecar 默认每 15 秒从数据库动态 reconcile，启停或切换模式不需要重启 systemd。

业务“禁用”不会自动关闭保活；保活成功也不会清除 `manual_disabled`、`429_rate_limit` 或 `consecutive_errors` 等由其他策略拥有的禁用原因。管理 API `PUT /api/tokens/{token_id}/lifecycle` 只修改保活 desired state，不等价于 `/enable` 或 `/disable`。

#### 会员过期与条件恢复

只有成功、身份一致的 credits 检查才计入会员观察：

- `PAYGATE_TIER_ONE` / `PAYGATE_TIER_TWO` 精确值为 paid；
- `PAYGATE_TIER_NOT_PAID` 精确值为 free；
- 缺失、大小写不同、带额外空白或未知 tier 均为 unknown，不会被当作 free。

活跃账号连续两次观察为 free 后，生命周期进入 retired；仅当该账号当时没有其他禁用原因时，业务状态才改为 `is_active=0`、`ban_reason=membership_expired`。退休账号续费后，连续两次观察为 paid 才尝试恢复；恢复事务会重新读取账号，只在状态仍为 `is_active=0` 且 `ban_reason='membership_expired'` 时恢复。人工禁用、429 或连续错误封禁不会被会员恢复误清除。

#### 服务器 XRDP 入库与重新登录

管理后台的账号入库流程在服务器端创建持久化 `onboarding_jobs`：服务在配置好的 XRDP 显示器上启动受管 Chrome，操作员仅负责 Google 登录并确认 Flow 页面可用。Finalize 会关闭并核验受管进程、读取真实账号身份、匹配或创建 Token、补齐项目池、原子迁移 profile，再次验证目标 profile，最后分别应用“加入业务池”和“启用保活”的选择。

重新登录已有账号时指定目标 Token；服务会拒绝邮箱不匹配。目标 profile 已存在时默认拒绝覆盖，只有显式选择 `archive_and_replace` 才会把旧 profile 保留到归档目录后替换，便于回滚。Free 或 unknown 新账号不会因请求业务启用而自动进入业务池。

完整的部署、XRDP 操作、API、命令、维护窗口、回滚和故障排查见 [`docs/operations/browser-keepalive.md`](docs/operations/browser-keepalive.md)。

#### captcha profile 与 keepalive profile 不是同一资源

- **captcha profile**：`personal` 验证码模式使用的共享持久化登录态与常驻标签页，目标是提高 reCAPTCHA 通过率。
- **keepalive profile**：`<browser_profile_base>/<token_id>` 下每账号独占的 Google/Flow 登录态，目标是刷新并严格验证该 Token 的 OAuth 凭据。

不要让 captcha 服务、XRDP Chrome、setup helper 和 keepalive sidecar 同时占用同一个 keepalive profile。sidecar 通过跨进程 `flock`、Chrome `SingletonLock` 的 PID/cmdline 校验和精确 profile 所有权检查拒绝并发占用。

#### 添加 Token、插件与 CORS

管理后台仍支持从 cookies 文本、Cookie 请求头、DevTools JSON 或裸 ST 中提取 `__Secure-next-auth.session-token`。这些入口会验证真实账号身份，但只添加凭据并不等于已经完成独立 keepalive profile 的服务器入库。

同源管理页面不需要 CORS。跨域 Web 控制台或 Chrome 插件必须在 `[server].cors_allowed_origins` 或环境变量 `FLOW2API_CORS_ALLOWED_ORIGINS` 中配置**精确 Origin**；不支持 `*`，也不能包含路径、查询参数或 fragment。允许的 scheme 为 `http`、`https` 和 `chrome-extension`。例如：

```toml
[server]
cors_allowed_origins = [
  "https://console.example.com",
  "chrome-extension://abcdefghijklmnop",
]
```

环境变量会覆盖 TOML，多个 Origin 用逗号分隔。插件 `/api/plugin/update-token` 继续使用独立 connection token 的 `Authorization: Bearer <token>` 契约；CORS allowlist 只允许浏览器发送请求，不替代认证。普通 `GET /api/tokens` 不返回 ST/AT；敏感凭据只能通过需要管理员认证、带 `Cache-Control: no-store` 的显式导出端点获取。

相关主机配置位于 `config/setting.toml`：

```toml
[keepalive]
browser_enabled = true
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

`BROWSER_EXECUTABLE_PATH` 选择 Chrome 可执行文件；Discord webhook 仍优先读取 `FLOW2API_ALERT_WEBHOOK_URL`。systemd unit 可选读取 root-owned、权限 `0600` 的 `/etc/flow2api-keepalive.env`，该文件只在服务器本地保存 `FLOW2API_ALERT_WEBHOOK_URL`，不得把真实 webhook 写入仓库或文档。旧 `[token].st_keepalive_enabled` 只在浏览器 supervisor 关闭时运行，启用 browser keepalive 后 sidecar 与 `token_lifecycle` 是账号保活的权威来源。

当 `browser_enabled=false` 时，`--preflight` 会输出 disabled 并以 0 退出，不检查浏览器、display、数据库或 profiles。保活与 setup 传给 Chrome 的代理 URL 禁止内嵌 `user:password@` userinfo，违规配置会在构造浏览器参数前被拒绝且不会回显凭据。preflight 与 setup 的运维输出不显示配置的 profile/browser 绝对路径，canonical path 校验失败也会转换为不含路径的通用错误。read-only patrol 不把历史成功永久视为健康：active 和 retired 分别使用配置的 `browser_interval_seconds` 与 `browser_retired_interval_seconds`；grace 为对应 interval 的一半，并限制在 300–3600 秒，同时检查 `last_keepalive_success_at` 与已逾期的 `next_due_at`。Legacy ID 23 首次迁移按 `--once --token-id 23` → `--preflight` → 启动 systemd sidecar 的顺序建立身份绑定并验收。

## 支持的模型

### 图片生成

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `gemini-3.0-pro-image-landscape` | 图/文生图 | 横屏 |
| `gemini-3.0-pro-image-portrait` | 图/文生图 | 竖屏 |
| `gemini-3.0-pro-image-square` | 图/文生图 | 方图 |
| `gemini-3.0-pro-image-four-three` | 图/文生图 | 横屏 4:3 |
| `gemini-3.0-pro-image-three-four` | 图/文生图 | 竖屏 3:4 |
| `gemini-3.0-pro-image-landscape-2k` | 图/文生图(2K) | 横屏 |
| `gemini-3.0-pro-image-portrait-2k` | 图/文生图(2K) | 竖屏 |
| `gemini-3.0-pro-image-square-2k` | 图/文生图(2K) | 方图 |
| `gemini-3.0-pro-image-four-three-2k` | 图/文生图(2K) | 横屏 4:3 |
| `gemini-3.0-pro-image-three-four-2k` | 图/文生图(2K) | 竖屏 3:4 |
| `gemini-3.0-pro-image-landscape-4k` | 图/文生图(4K) | 横屏 |
| `gemini-3.0-pro-image-portrait-4k` | 图/文生图(4K) | 竖屏 |
| `gemini-3.0-pro-image-square-4k` | 图/文生图(4K) | 方图 |
| `gemini-3.0-pro-image-four-three-4k` | 图/文生图(4K) | 横屏 4:3 |
| `gemini-3.0-pro-image-three-four-4k` | 图/文生图(4K) | 竖屏 3:4 |
| `imagen-4.0-generate-preview-landscape` | 图/文生图 | 横屏 |
| `imagen-4.0-generate-preview-portrait` | 图/文生图 | 竖屏 |
| `gemini-3.1-flash-image-landscape` | 图/文生图 | 横屏 |
| `gemini-3.1-flash-image-portrait` | 图/文生图 | 竖屏 |
| `gemini-3.1-flash-image-square` | 图/文生图 | 方图 |
| `gemini-3.1-flash-image-four-three` | 图/文生图 | 横屏 4:3 |
| `gemini-3.1-flash-image-three-four` | 图/文生图 | 竖屏 3:4 |
| `gemini-3.1-flash-image-landscape-2k` | 图/文生图(2K) | 横屏 |
| `gemini-3.1-flash-image-portrait-2k` | 图/文生图(2K) | 竖屏 |
| `gemini-3.1-flash-image-square-2k` | 图/文生图(2K) | 方图 |
| `gemini-3.1-flash-image-four-three-2k` | 图/文生图(2K) | 横屏 4:3 |
| `gemini-3.1-flash-image-three-four-2k` | 图/文生图(2K) | 竖屏 3:4 |
| `gemini-3.1-flash-image-landscape-4k` | 图/文生图(4K) | 横屏 |
| `gemini-3.1-flash-image-portrait-4k` | 图/文生图(4K) | 竖屏 |
| `gemini-3.1-flash-image-square-4k` | 图/文生图(4K) | 方图 |
| `gemini-3.1-flash-image-four-three-4k` | 图/文生图(4K) | 横屏 4:3 |
| `gemini-3.1-flash-image-three-four-4k` | 图/文生图(4K) | 竖屏 3:4 |

### 视频生成

#### 文生视频 (T2V - Text to Video)

不支持上传图片

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `veo_3_1_t2v_fast_portrait` | 文生视频 | 竖屏 |
| `veo_3_1_t2v_fast_landscape` | 文生视频 | 横屏 |
| `veo_2_1_fast_d_15_t2v_portrait` | 文生视频 | 竖屏 |
| `veo_2_1_fast_d_15_t2v_landscape` | 文生视频 | 横屏 |
| `veo_2_0_t2v_portrait` | 文生视频 | 竖屏 |
| `veo_2_0_t2v_landscape` | 文生视频 | 横屏 |
| `veo_3_1_t2v_fast_portrait_ultra` | 文生视频 | 竖屏 |
| `veo_3_1_t2v_fast_ultra` | 文生视频 | 横屏 |
| `veo_3_1_t2v_fast_portrait_ultra_relaxed` | 文生视频 | 竖屏 |
| `veo_3_1_t2v_fast_ultra_relaxed` | 文生视频 | 横屏 |
| `veo_3_1_t2v_portrait` | 文生视频 | 竖屏 |
| `veo_3_1_t2v_landscape` | 文生视频 | 横屏 |
| `veo_3_1_t2v_lite_portrait` | 文生视频 Lite | 竖屏 |
| `veo_3_1_t2v_lite_landscape` | 文生视频 Lite | 横屏 |

#### 首尾帧模型 (I2V - Image to Video)

支持 1-2 张图片：1 张作为首帧，2 张作为首尾帧

> **自动适配**：系统会根据图片数量自动选择对应的 model_key
> - **单帧模式**（1张图）：使用首帧生成视频
> - **双帧模式**（2张图）：使用首帧+尾帧生成过渡视频
> - `veo_3_1_i2v_lite_*` 仅支持 **1 张** 首帧图片
> - `veo_3_1_interpolation_lite_*` 仅支持 **2 张** 首尾帧图片

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `veo_3_1_i2v_s_portrait` | 图生视频 满血版 | 竖屏 |
| `veo_3_1_i2v_s_landscape` | 图生视频 满血版 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_fl` | 图生视频 | 竖屏 |
| `veo_3_1_i2v_s_fast_fl` | 图生视频 | 横屏 |
| `veo_2_1_fast_d_15_i2v_portrait` | 图生视频 | 竖屏 |
| `veo_2_1_fast_d_15_i2v_landscape` | 图生视频 | 横屏 |
| `veo_2_0_i2v_portrait` | 图生视频 | 竖屏 |
| `veo_2_0_i2v_landscape` | 图生视频 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl` | 图生视频 | 竖屏 |
| `veo_3_1_i2v_s_fast_ultra_fl` | 图生视频 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_ultra_relaxed` | 图生视频 | 竖屏 |
| `veo_3_1_i2v_s_fast_ultra_relaxed` | 图生视频 | 横屏 |
| `veo_3_1_i2v_lite_portrait` | 图生视频 Lite（仅首帧） | 竖屏 |
| `veo_3_1_i2v_lite_landscape` | 图生视频 Lite（仅首帧） | 横屏 |
| `veo_3_1_interpolation_lite_portrait` | 图生视频 Lite（首尾帧过渡） | 竖屏 |
| `veo_3_1_interpolation_lite_landscape` | 图生视频 Lite（首尾帧过渡） | 横屏 |

#### 多图生成 (R2V - Reference Images to Video)

支持多张参考图（最多 3 张）

> 服务端自动组装新版视频请求体，调用方仍然使用 OpenAI 兼容输入即可。

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `veo_3_1_r2v_fast_portrait` | 多图视频 | 竖屏 |
| `veo_3_1_r2v_fast` | 多图视频 | 横屏 |
| `veo_3_1_r2v_fast_portrait_ultra` | 多图视频 | 竖屏 |
| `veo_3_1_r2v_fast_ultra` | 多图视频 | 横屏 |
| `veo_3_1_r2v_fast_portrait_ultra_relaxed` | 多图视频 | 竖屏 |
| `veo_3_1_r2v_fast_ultra_relaxed` | 多图视频 | 横屏 |

### 视频放大 (Upsample)

| 模型名称 | 说明 | 输出 |
|---------|------|------|
| `veo_3_1_t2v_fast_portrait_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_fast_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_fast_portrait_ultra_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_fast_ultra_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_fast_portrait_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_t2v_fast_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_t2v_fast_portrait_ultra_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_t2v_fast_ultra_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_4k` | 图生视频放大 | 4K |
| `veo_3_1_i2v_s_fast_ultra_fl_4k` | 图生视频放大 | 4K |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p` | 图生视频放大 | 1080P |
| `veo_3_1_i2v_s_fast_ultra_fl_1080p` | 图生视频放大 | 1080P |
| `veo_3_1_r2v_fast_portrait_ultra_4k` | 多图视频放大 | 4K |
| `veo_3_1_r2v_fast_ultra_4k` | 多图视频放大 | 4K |
| `veo_3_1_r2v_fast_portrait_ultra_1080p` | 多图视频放大 | 1080P |
| `veo_3_1_r2v_fast_ultra_1080p` | 多图视频放大 | 1080P |

### 视频延长 15s (Video Extend)

内部流程：生成 8s 视频 → 延长 8s → 拼接（跳过 1s 重叠）→ 返回约 15s 视频。对上游调用方透明，使用方式与普通视频模型完全一致。

使用 `_15s` 后缀的模型名即可触发。也支持省略横竖屏后缀的简写（如 `veo_3_1_t2v_fast_15s`），服务端根据请求自动匹配横竖屏。

#### 文生视频 15s (T2V 15s)

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `veo_3_1_t2v_fast_portrait_15s` | 文生视频延长 | 竖屏 |
| `veo_3_1_t2v_fast_landscape_15s` | 文生视频延长 | 横屏 |
| `veo_3_1_t2v_fast_portrait_ultra_15s` | 文生视频延长 | 竖屏 |
| `veo_3_1_t2v_fast_ultra_15s` | 文生视频延长 | 横屏 |
| `veo_3_1_t2v_fast_portrait_ultra_relaxed_15s` | 文生视频延长 | 竖屏 |
| `veo_3_1_t2v_fast_ultra_relaxed_15s` | 文生视频延长 | 横屏 |
| `veo_3_1_t2v_portrait_15s` | 文生视频延长 | 竖屏 |
| `veo_3_1_t2v_landscape_15s` | 文生视频延长 | 横屏 |
| `veo_3_1_t2v_lite_portrait_15s` | 文生视频延长 Lite | 竖屏 |
| `veo_3_1_t2v_lite_landscape_15s` | 文生视频延长 Lite | 横屏 |

#### 图生视频 15s (I2V 15s)

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `veo_3_1_i2v_s_portrait_15s` | 图生视频延长 满血版 | 竖屏 |
| `veo_3_1_i2v_s_landscape_15s` | 图生视频延长 满血版 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_fl_15s` | 图生视频延长 | 竖屏 |
| `veo_3_1_i2v_s_fast_fl_15s` | 图生视频延长 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_15s` | 图生视频延长 | 竖屏 |
| `veo_3_1_i2v_s_fast_ultra_fl_15s` | 图生视频延长 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_ultra_relaxed_15s` | 图生视频延长 | 竖屏 |
| `veo_3_1_i2v_s_fast_ultra_relaxed_15s` | 图生视频延长 | 横屏 |
| `veo_3_1_i2v_lite_portrait_15s` | 图生视频延长 Lite（仅首帧） | 竖屏 |
| `veo_3_1_i2v_lite_landscape_15s` | 图生视频延长 Lite（仅首帧） | 横屏 |
| `veo_3_1_interpolation_lite_portrait_15s` | 图生视频延长 Lite（首尾帧） | 竖屏 |
| `veo_3_1_interpolation_lite_landscape_15s` | 图生视频延长 Lite（首尾帧） | 横屏 |

#### 多图视频 15s (R2V 15s)

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `veo_3_1_r2v_fast_portrait_15s` | 多图视频延长 | 竖屏 |
| `veo_3_1_r2v_fast_15s` | 多图视频延长 | 横屏 |
| `veo_3_1_r2v_fast_portrait_ultra_15s` | 多图视频延长 | 竖屏 |
| `veo_3_1_r2v_fast_ultra_15s` | 多图视频延长 | 横屏 |
| `veo_3_1_r2v_fast_portrait_ultra_relaxed_15s` | 多图视频延长 | 竖屏 |
| `veo_3_1_r2v_fast_ultra_relaxed_15s` | 多图视频延长 | 横屏 |

### 视频延长 15s + 放大 (Extend + Upsample)

在 15s 延长基础上叠加 1080P 或 4K 放大。流程：生成 8s → 放大 → 延长 8s → 拼接（跳过 1s 重叠）→ 返回 15s 高清视频。

#### T2V 延长 + 放大

| 模型名称 | 输出 | 尺寸 |
|---------|------|------|
| `veo_3_1_t2v_fast_portrait_15s_1080p` | 15s + 1080P | 竖屏 |
| `veo_3_1_t2v_fast_landscape_15s_1080p` | 15s + 1080P | 横屏 |
| `veo_3_1_t2v_fast_portrait_15s_4k` | 15s + 4K | 竖屏 |
| `veo_3_1_t2v_fast_landscape_15s_4k` | 15s + 4K | 横屏 |
| `veo_3_1_t2v_fast_portrait_ultra_15s_1080p` | 15s + 1080P | 竖屏 |
| `veo_3_1_t2v_fast_ultra_15s_1080p` | 15s + 1080P | 横屏 |
| `veo_3_1_t2v_fast_portrait_ultra_15s_4k` | 15s + 4K | 竖屏 |
| `veo_3_1_t2v_fast_ultra_15s_4k` | 15s + 4K | 横屏 |

#### I2V 延长 + 放大

| 模型名称 | 输出 | 尺寸 |
|---------|------|------|
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_15s_1080p` | 15s + 1080P | 竖屏 |
| `veo_3_1_i2v_s_fast_ultra_fl_15s_1080p` | 15s + 1080P | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_15s_4k` | 15s + 4K | 竖屏 |
| `veo_3_1_i2v_s_fast_ultra_fl_15s_4k` | 15s + 4K | 横屏 |

#### R2V 延长 + 放大

| 模型名称 | 输出 | 尺寸 |
|---------|------|------|
| `veo_3_1_r2v_fast_portrait_ultra_15s_1080p` | 15s + 1080P | 竖屏 |
| `veo_3_1_r2v_fast_ultra_15s_1080p` | 15s + 1080P | 横屏 |
| `veo_3_1_r2v_fast_portrait_ultra_15s_4k` | 15s + 4K | 竖屏 |
| `veo_3_1_r2v_fast_ultra_15s_4k` | 15s + 4K | 横屏 |

### Gemini Omni Flash (T2V / R2V)

Google Flow 的新一代视频模型，上游代号 `abra`。每个时长档位是独立模型（4/6/8/10s 各一），与 Veo 系列固定时长不同。横竖屏共享同一上游 `model_key`，仅请求体 `aspectRatio` 区分。1080P 上采样链路复用 Veo 3.1 的 upsampler。4K 上采样暂未集成。

调用方式与现有模型完全一致 —— OpenAI `chat.completions` 输入或 Gemini 官方格式。

#### 文生视频 (T2V)

| 模型名称 | 时长 | 尺寸 |
|---------|------|------|
| `gemini_omni_t2v_4s` / `gemini_omni_t2v_portrait_4s` | 4s | 横/竖屏 |
| `gemini_omni_t2v_6s` / `gemini_omni_t2v_portrait_6s` | 6s | 横/竖屏 |
| `gemini_omni_t2v_8s` / `gemini_omni_t2v_portrait_8s` | 8s | 横/竖屏 |
| `gemini_omni_t2v_10s` / `gemini_omni_t2v_portrait_10s` | 10s | 横/竖屏 |

#### 多图视频 (R2V，最多 3 张参考图)

| 模型名称 | 时长 | 尺寸 |
|---------|------|------|
| `gemini_omni_r2v_4s` / `gemini_omni_r2v_portrait_4s` | 4s | 横/竖屏 |
| `gemini_omni_r2v_6s` / `gemini_omni_r2v_portrait_6s` | 6s | 横/竖屏 |
| `gemini_omni_r2v_8s` / `gemini_omni_r2v_portrait_8s` | 8s | 横/竖屏 |
| `gemini_omni_r2v_10s` / `gemini_omni_r2v_portrait_10s` | 10s | 横/竖屏 |

#### 1080P 上采样版（在上面任一基础名后加 `_1080p`）

| 模型名称 | 输出 | 尺寸 |
|---------|------|------|
| `gemini_omni_t2v_{4,6,8,10}s_1080p` | 原版时长 + 1080P | 横屏 |
| `gemini_omni_t2v_portrait_{4,6,8,10}s_1080p` | 原版时长 + 1080P | 竖屏 |
| `gemini_omni_r2v_{4,6,8,10}s_1080p` | 原版时长 + 1080P | 横屏 |
| `gemini_omni_r2v_portrait_{4,6,8,10}s_1080p` | 原版时长 + 1080P | 竖屏 |

> 实测耗时（持久化登录态 + 住宅 IP 代理）：T2V 4s ≈ 45s、T2V 10s ≈ 50s、R2V 4s ≈ 60s、T2V 4s + 1080P 上采样 ≈ 80s。

## API 使用示例（需要使用流式）

> 除了下方 `OpenAI-compatible` 示例，服务也支持 Gemini 官方格式：
> - `POST /v1beta/models/{model}:generateContent`
> - `POST /models/{model}:generateContent`
> - `POST /v1beta/models/{model}:streamGenerateContent`
> - `POST /models/{model}:streamGenerateContent`
>
> Gemini 官方格式支持以下认证方式：
> - `Authorization: Bearer <api_key>`
> - `x-goog-api-key: <api_key>`
> - `?key=<api_key>`
>
> Gemini 官方图片请求体已兼容：
> - `systemInstruction`
> - `contents[].parts[].text`
> - `contents[].parts[].inlineData`
> - `contents[].parts[].fileData.fileUri`
> - `generationConfig.responseModalities`
> - `generationConfig.imageConfig.aspectRatio`
> - `generationConfig.imageConfig.imageSize`

### Gemini 官方 generateContent（文生图）

> 已使用真实 Token 实测通过。
> 如需流式返回，可将路径替换为 `:streamGenerateContent?alt=sse`。

```bash
curl -X POST "http://localhost:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "systemInstruction": {
      "parts": [
        {
          "text": "Return an image only."
        }
      ]
    },
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "一颗放在木桌上的红苹果，棚拍光线，极简背景"
          }
        ]
      }
    ],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {
        "aspectRatio": "1:1",
        "imageSize": "1K"
      }
    }
  }'
```

### 文生图

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只可爱的猫咪在花园里玩耍"
      }
    ],
    "stream": true
  }'
```

### 图生图

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "将这张图片变成水彩画风格"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_encoded_image>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### 文生视频

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_t2v_fast_landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只小猫在草地上追逐蝴蝶"
      }
    ],
    "stream": true
  }'
```

### 文生视频 15s

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_t2v_fast_landscape_15s",
    "messages": [
      {
        "role": "user",
        "content": "一只小猫在草地上追逐蝴蝶"
      }
    ],
    "stream": true
  }'
```

### 首尾帧生成视频

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_i2v_s_fast_fl_landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "从第一张图过渡到第二张图"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<首帧base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<尾帧base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Gemini Omni Flash

模型名换成 `gemini_omni_*` 即可，调用方式完全一致。R2V 与 1080P 上采样同步支持。

```bash
# T2V 10 秒，横屏
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini_omni_t2v_10s",
    "messages": [
      {
        "role": "user",
        "content": "一只小猫在草地上追逐蝴蝶，柔和阳光"
      }
    ],
    "stream": true
  }'

# T2V 4 秒 + 1080P 上采样
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini_omni_t2v_4s_1080p",
    "messages": [{"role":"user","content":"a glowing jellyfish in deep ocean"}],
    "stream": true
  }'
```

### 多图生成视频

> `R2V` 会由服务端自动组装新版视频请求体，调用方仍然使用 OpenAI 兼容输入即可。
> 服务端会将横屏 `R2V` 自动映射到最新的 `*_landscape` 上游模型键。
> 当前最多传 **3 张参考图**。

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_r2v_fast_portrait",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "以三张参考图的人物和场景为基础，生成一段镜头平滑推进的竖屏视频"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图1base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图2base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图3base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

---

## 开发与测试

代码架构与模块划分见 [`docs/architecture.md`](docs/architecture.md)。

运行测试套件（全离线、锁定项目 `.venv`）：

```bash
# 安装开发依赖（pytest / pyflakes 等）
pip install -r requirements-dev.txt

# 运行全部测试
bash scripts/test.sh

# 运行单个测试文件
bash scripts/test.sh tests/characterization/test_poll_video_result.py
```

测试以 **golden 特征化（characterization）** 为主，锁定重构前后的行为等价；另有两个架构守卫：
`test_shared_extractability.py`（保证 `shared/` 不依赖业务模块）与 `test_no_undefined_names.py`
（pyflakes 扫描，防止抽取模块时漏带 import）。

---

## 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。

---

## 致谢

- [PearNoDec](https://github.com/PearNoDec) 提供的YesCaptcha打码方案
- [raomaiping](https://github.com/raomaiping) 提供的无头打码方案
感谢所有贡献者和使用者的支持！

---

## 联系方式

- 提交 Issue：[GitHub Issues](https://github.com/TheSmallHanCat/flow2api/issues)

---

**如果这个项目对你有帮助，请给个 Star！**

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=TheSmallHanCat/flow2api&type=date&legend=top-left)](https://www.star-history.com/#TheSmallHanCat/flow2api&type=date&legend=top-left)
