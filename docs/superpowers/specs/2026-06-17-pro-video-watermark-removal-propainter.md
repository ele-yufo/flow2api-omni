# Pro 视频去水印 — ProPainter 常驻服务方案（取代早期 un-blend 方案）

- 日期：2026-06-17
- 状态：方案已验证（质量+性能），进入实现
- 取代：`2026-06-16-pro-video-watermark-removal-design.md`（un-blend 方案，已废弃）

## 1. 背景与方案演进

目标不变：Pro 账号生成的视频右下角有 Gemini sparkle（✦）水印，Ultra 没有；用多 Pro 账号轮转替代昂贵的 Ultra，需把 Pro 视频水印去到大屏看不出。

**方案演进**（经实测）：
- 早期 **un-blend（反向 alpha 解混合）**：图片无损，但**重压缩视频**上反混合残留结构性边缘，平滑/亮背景大屏可见；inpaint 补则在流动背景留补丁。**经典方法到顶，弃用。**
- 最终 **ProPainter（视频时序 inpainting）**：4× 像素级放大下水印彻底消失（无星/无补丁/无糊），时序一致。流动/纹理/平滑背景通吃。用户大屏验收通过。

## 2. 已验证的关键事实

- 水印固定在 720p 帧的 **(1136,576)，48px**（横竖屏皆距右下角 ~120px；本期只做 720p landscape/portrait 同档）。
- 精确 sparkle 蒙版 `v2_diamond`（取自社区 froggeric 内嵌资产）。
- tier 路由：`token.user_paygate_tier` 已知。`TIER_ONE`=Pro→去水印；`TIER_TWO`=Ultra/Free→透传。
- ProPainter 整帧 720p 会 OOM（与 ComfyUI 共用 GPU）→ **只裁水印周围 192×192** 喂模型，修完 **ffmpeg alpha 叠加**贴回（只动水印区）。
- **性能（2080Ti，常驻模型）**：4s 视频 ~5.5s / 10s 视频 ~13s（端到端）。冷启动多 ~1s 模型加载（常驻后免除）。

## 3. 架构

**两个进程，同机：**

### 3.1 去水印常驻服务 `dewatermark/server.py`
- 独立进程，用 **ComfyUI 的 venv**（torch+CUDA），systemd 常驻。
- 启动**一次性加载** ProPainter 3 模型（RAFT / flow_completion / ProPainter）入显存，常驻复用（monkey-patch 缓存构造器）。
- HTTP：`POST /dewatermark {input, output}`（本地文件路径）→ 192 裁剪 → ProPainter（缓存模型）→ ffmpeg alpha 叠加 → 写 output → 返回 `{ok, timings, total}`；`GET /health`。
- GPU **串行**（内部锁），一次一条。
- 参数化：`PROPAINTER_DIR`、`WM_MASK_DIR`、`WM_PORT`、裁剪/位置常量、`WM_VENV_PYTHON`。
- 依赖（不入 git）：ProPainter 仓库 + 权重（~200MB）+ imageio-ffmpeg（已装进 ComfyUI venv）。README 记录安装。
- 入 git：`server.py`、`masks/{mask192,mask192_alpha}.png`、`README.md`、`flow2api-dewatermark.service`。

### 3.2 flow2api 接入
- `src/core/config.py` + `config/setting.toml`：新增 `[watermark]`（`enabled`、`service_url`、`timeout_seconds`、`min_tier`）。
- `src/services/watermark_client.py`（新）：`async dewatermark_video(upstream_url, token, response_state) -> local_url`：
  1. 下载上游视频到本地（复用 `file_cache.download_and_cache`）。
  2. `POST {service_url}/dewatermark {input: 本地原片, output: 本地去水印}`（httpx，超时 = `timeout_seconds`）。
  3. 返回 `{base_url}/tmp/{去水印文件名}`。
- `src/services/generation_handler.py`：视频返回路径（`_poll_video_result` ~2800 处），当 `normalize_user_paygate_tier(token.user_paygate_tier) == PAYGATE_TIER_ONE` 且 `config.watermark_enabled`：走 `watermark_client` 替换返回 URL；否则现状透传。
- **失败兜底（铁律）**：服务超时/报错/不可达 → 记日志 + 回退原 URL（已下载的本地原片或上游 URL），**绝不让生成失败**。

## 4. 范围 / 非目标

- 做：720p Pro 视频（t2v/r2v/i2v，横竖屏）去 sparkle 可见水印。
- 不做：SynthID 隐形水印（视频不可行，已与用户确认放弃）；1080p（水印位置/尺寸需另标，本期不覆盖，未知分辨率走透传兜底）；图片（无水印）。

## 5. 测试

- **单元**：config 属性默认/边界；`watermark_client` 的 tier 门控 + 失败兜底（mock 服务 200/超时/500）；URL 构造。
- **服务**：`/health`；`/dewatermark` 对一条已知 Pro 片返回成功 + 输出存在。
- **端到端冒烟**：启动去水印服务 + 重启 flow2api（带新代码）→ 经 flow2api 生成接口出一条 Pro 视频 → 断言返回本地 URL、文件存在、水印区 4× 无星。Ultra 透传不受影响。

## 6. 风险

- GPU 与 ComfyUI 共用：常驻 + 串行锁缓解；重负载下延迟会抖（生产可低峰/排队）。
- 上游若改水印位置/分辨率 → 固定参数失效；兜底透传不致崩；后续可加检测门控。
- 重编码 CRF14 视觉无损。
