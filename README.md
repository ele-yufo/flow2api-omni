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
- **AT/ST自动刷新** - AT 过期自动刷新，ST 过期时自动通过浏览器更新（personal 模式）
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

- 自动更新st浏览器拓展：[Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater)

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

## 支持的模型

### 图片生成

| 模型名称 | 说明 | 尺寸 |
|---------|------|------|
| `gemini-2.5-flash-image-landscape` | 图/文生图 | 横屏 |
| `gemini-2.5-flash-image-portrait` | 图/文生图 | 竖屏 |
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
