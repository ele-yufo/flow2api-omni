# 去水印 SaaS Demo 设计（二期 v1）

> 二期目标是把 Pro 视频去水印做成**独立 SaaS**。本 spec 只覆盖 **v1 Demo**：能端到端跑通
> `贴 Flow 分享链接 / 上传 → 去水印 → 下载`，无支付、无账号。跑通后账号/计量/支付是后续阶段。

**状态**：设计已定稿（经与用户 brainstorming 逐项拍板）。本文件与实施计划均为内部交付物，不占用用户审阅。

---

## 目标（一句话）

新建一个独立的 FastAPI 小服务 `dewatermark_saas/`，复用 flow2api 一期抽出的 `src/shared/`（config/storage/telemetry），
调用本机常驻 ProPainter 服务（`127.0.0.1:18290`）去除 Veo/Flow 720-tier sparkle 水印，
经国内网关范式发布到 `https://dewm.ele-yufo.com`（Basic Auth 网关）。

## 已验证的关键事实（写码前实测，非假设）

1. **分享链接可解析**：`https://labs.google/fx/tools/flow/shared/video/<uuid>` 页面含公开 og:video 端点
   `https://labs.google/fx/api/og-video/shared/<uuid>`，HTTP 200 直接返回 `video/mp4`（实测 1.38MB），
   **无需登录、无需代理、无需视频主人授权**。这是输入方式的地基，已验证。
2. **水印固定**：实测样例视频 1280×720、24fps、Veo ✦ 星标固定在右下角、全程不动。
3. **ProPainter 服务自动化**：`dewatermark/server.py` 自己 `ffprobe` 探分辨率、自动选 mask，
   caller **无需**指定水印区域。仅支持 `1280×720` / `720×1280`（720-tier landscape/portrait），
   其它分辨率返回 `{ok:false, reason}`。性能（2080Ti，模型常驻）：4s 视频 ~5.5s、10s ~13s；
   **GPU 内部串行锁，一次处理一条**。
4. **部署拓扑**：本机 = 2080TI（`yufo-ministation-01-2080ti`），flow2api `:18282`、ProPainter `:18290`(localhost)、
   mihomo `:7890`、`frpc` systemd active（`/etc/frp/frpc.toml`，serverAddr `111.228.51.177:37000`，现仅映射 SSH 22→37001）。
   `WM_ALLOWED_DIR=/opt/Projects/flow2api/tmp`。

## 架构分层

```
dewatermark_saas/                 # 新独立服务（与 src/ 同仓，逻辑独立）
├── app.py                        # FastAPI 组合根：装配路由 + 静态前端 + 启动 worker
├── jobs.py                       # 内存 job store（dataclass + asyncio 队列 + 状态机）
├── pipeline.py                   # 纯处理管线：解析链接 → 下载 → 调 ProPainter → 结果
├── share_link.py                 # 分享链接 uuid 解析 + og-video URL 构造（纯函数）
├── config.py                     # 服务自身配置（端口/工作目录/并发/Basic Auth 凭证来源）
├── static/                       # designer subagent 产出的单页前端（index.html + assets）
└── tests/                        # 管线/链接解析/端点的离线测试
```

依赖方向：`dewatermark_saas` → `src.shared`（config / storage / telemetry）。**只读复用，不改 src/**。
这也二次验证一期「shared/ 可被第二个消费者独立复用」的抽取契约。

## 组件与接口

### share_link.py（纯函数，无 I/O）
- `extract_share_uuid(text: str) -> str | None`
  - 从完整分享 URL 或裸 uuid 中抽出 uuid（正则 `[0-9a-f]{8}-[0-9a-f]{4}-...`）。
  - 兼容：`https://labs.google/fx/tools/flow/shared/video/<uuid>`、带 query/fragment、裸 uuid。
- `og_video_url(uuid: str) -> str`
  - `-> f"https://labs.google/fx/api/og-video/shared/{uuid}"`

### pipeline.py（async，有 I/O，错误语义明确）
- `async def process(job: Job) -> None`
  - 步骤并更新 `job.status` / `job.progress`：
    1. `resolving`：若输入是链接 → `extract_share_uuid` → `og_video_url`；若上传文件 → 跳过。
    2. `downloading`：`httpx` GET og-video（或落地上传文件）到 `WORK_DIR/<job_id>/in.mp4`（在 `WM_ALLOWED_DIR` 下）。
       - 校验 content-type 前缀 `video/`、大小 >0 且 <= `MAX_INPUT_BYTES`（默认 200MB）。
    3. `dewatermarking`：POST `{config.watermark_service_url}/dewatermark {input, output}`，
       超时 `config.watermark_timeout_seconds`（复用 shared/config）。
       - `{ok:true}` → `done`，`download_path = out`。
       - `{ok:false, reason}` → `error`，`error_message="仅支持 720p Veo 视频（1280×720/720×1280）"`（分辨率不支持）。
       - HTTP 500 / 异常 → `error`，`error_message="去水印处理失败，请重试"`。
    4. `done`：产物 `WORK_DIR/<job_id>/dewm_in.mp4`，供下载端点流式返回。
  - **不吞异常成"成功"**：与 `shared/gpu/watermark_client.dewatermark_video` 的「失败回退原片」语义相反——
    SaaS 必须把失败暴露给用户，绝不把带水印原片当结果返回。

### jobs.py（内存 store）
- `@dataclass Job`：`id: str`（uuid4）、`status: Literal["queued","resolving","downloading","dewatermarking","done","error"]`、
  `progress: int`(0-100)、`source: {"kind":"link"|"upload", ...}`、`download_path: str|None`、`error_message: str|None`、
  `created_monotonic: float`、`timings: dict|None`。
- `JobStore`：`create(...)`、`get(id)`、`asyncio.Queue` 派发；后台单 worker 串行消费（与 ProPainter GPU 串行匹配，
  并发上限=1；多请求排队，`queued` 状态含排队位次可选）。
- 内存态：进程重启丢 job（Demo 可接受，YAGNI 不上库）。工作文件 job 完成/失败后延时清理（cron 或 worker 收尾）。

### app.py（FastAPI，`127.0.0.1:18300`）
- `POST /api/jobs`：`multipart/form-data`（`link: str` 可选 + `file: UploadFile` 可选，二选一）→ 建 job → `{job_id}`。
  - 校验：link 与 file 至少给一个；link 必须能抽出 uuid，否则 400。
- `GET /api/jobs/{job_id}`：`{status, progress, error_message?, download_url?}`。前端每 ~1.5s 轮询。
- `GET /api/jobs/{job_id}/download`：完成后 `FileResponse` 流式返回 `dewm_*.mp4`（`Content-Disposition: attachment`）。
- `GET /`：托管 designer 产出的单页前端（`static/index.html`）。
- `GET /healthz`：`{ok:true}`（供 nginx/frp 上游探活）。

### 前端（designer subagent 产出，走 frontend-design 技能）
- 单页：链接输入框 + 可选拖拽上传 → "去水印" → 进度条（轮询驱动的阶段态：排队/下载/去水印）→ 完成后内嵌播放 + 下载按钮。
- 要求：production-grade、有明确美学主张、避开 AI 通用感（见 frontend-design 技能）。纯 HTML/CSS/JS（无构建步骤），由 FastAPI 直接托管。
- 错误态 UI：分辨率不支持 / 处理失败 / 链接无效，各有清晰文案。

## 数据流

```
浏览器(dewm.ele-yufo.com, Basic Auth)
  └─POST /api/jobs (link|file) ─────────────► FastAPI(18300) ── create Job ──► JobStore.queue
                                                     │                              │
  ◄──────────────── {job_id} ────────────────────────┘                              ▼
  ─GET /api/jobs/{id} (轮询)──────────────► {status,progress}          后台 worker(串行)
                                                                          pipeline.process:
                                                                          resolve→download→
                                                                          POST 18290 ProPainter
                                                                          →done/error
  ─GET /api/jobs/{id}/download──────────► FileResponse(dewm_*.mp4)
```

## 错误处理

| 情况 | 表现 | 用户文案 |
|---|---|---|
| 链接抽不出 uuid | `POST /api/jobs` 400 | 链接无效，请贴 Flow 分享链接 |
| og-video 下载失败/非视频 | job `error` | 视频获取失败，请检查链接 |
| 分辨率不支持（ProPainter ok:false） | job `error` | 仅支持 720p Veo 视频 |
| ProPainter 500/超时 | job `error` | 去水印处理失败，请重试 |
| 上传文件过大 | `POST /api/jobs` 413 | 文件过大（上限 200MB） |

## 部署（走 wiki 现成「国内优先公网访问网关」范式）

```
https://dewm.ele-yufo.com
→ ECS-BJ OpenResty 443（泛域名证书 *.ele-yufo.com 已就绪，加 1 个 vhost）
→ http://111.228.51.177:38013（JD-Cloud frps 高位端口，38013 空闲）
→ 2080TI frpc（/etc/frp/frpc.toml 加 1 段 [[proxies]] 18300→38013）
→ 127.0.0.1:18300
```

- **frpc**：加 `name="dewatermark-saas", type="tcp", localPort=18300, remotePort=38013`；`sudo systemctl restart frpc`。
- **ECS-BJ vhost**：`/opt/1panel/apps/openresty/openresty/conf/conf.d/dewm.ele-yufo.com.conf`，照 SOP 模板
  （443 ssl + 泛证书 + `proxy_pass http://111.228.51.177:38013` + `proxy_read_timeout 3600s` + `proxy_buffering off`
  以支持大文件上传/下载）。`docker exec 1Panel-openresty-Y6PP nginx -t && ... -s reload`。
- **京东云安全组**：确认放行 38013（devices 卡显示 38001-38012 已用，38xxx 段开放，需实测确认）。
- **Basic Auth 网关（安全纪律，必做）**：按 wiki `domestic-public-access-gateway` 安全节——暴露到公网的服务不能裸奔。
  Demo 用 HTTP Basic Auth；实现放在 **FastAPI 层**（依赖注入校验 `Authorization: Basic`），凭证从环境变量读，不写死、不进 git。
  这既挡陌生人白嫖 GPU，又把「公开去水印挂真实备案域名」的平台风险压成「私有自用」。
  - `/healthz` 豁免鉴权（供上游探活）；其余全部要 Basic Auth。
  - 验收铁律：暴露后从外网**不带凭证测一次**，敏感端点必须 401。

## 测试

- `dewatermark_saas/tests/`（离线，锁 `.venv`）：
  - `test_share_link.py`：uuid 抽取（完整 URL / 带 query / 裸 uuid / 非法）→ og-video URL 构造。
  - `test_pipeline.py`：mock httpx + mock ProPainter HTTP，覆盖 ok / ok:false（分辨率不支持）/ 500 / 下载失败 四条错误语义。
  - `test_api.py`：`POST /api/jobs`（link/upload/两者缺失 400）、`GET /api/jobs/{id}` 状态流转、download 端点、Basic Auth（无凭证 401、有凭证 200）。
- **本地端到端**（真实上游，用已验证的样例链接）：贴 `acf396ba-...` 分享链接 → 跑通 → 下到去水印 mp4，肉眼确认右下角水印消失。
- **公网端到端**：`curl` `https://dewm.ele-yufo.com`（不带凭证 401、带凭证 200 + 前端挂载）。

## YAGNI（v1 明确不做，推迟到后续阶段）

账号注册、登录、会话、支付、Merchant-of-Record 接入、API key 签发、用量计量、限流、多租户、法务/隐私页、
持久化 job 库、多机 GPU 扩容、上传文件的病毒扫描。

## 后续阶段接缝（不实现，仅留位）

- `config.py` 的 Basic Auth → 未来替换为真实账号体系（登录态/JWT）。
- `JobStore` 内存 → 未来换 sqlite/持久队列。
- 单 worker 串行 → 未来多 GPU/多机时换分布式队列（复用 flow2api 的「提交→轮询」范式）。
- Basic Auth 网关 → 未来公开商用前，先解决平台合规（支付平台对「去 AI 水印」的政策风险）。
