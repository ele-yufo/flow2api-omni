# 去水印 SaaS Demo（dewatermark_saas）

二期 v1 Demo：贴 **Flow 分享链接**（或上传视频）→ 去掉 Veo/Flow 720-tier ✦ 水印 → 下载。
独立 FastAPI 小服务，复用一期抽出的 `src.shared.config`，调用本机常驻 ProPainter（`:18290`）。
设计见 `docs/superpowers/specs/2026-07-18-dewatermark-saas-demo-design.md`。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/jobs` | `multipart/form-data`：`link`（Flow 分享链接）**或** `file`（上传视频），二选一 → `{job_id}` |
| GET | `/api/jobs/{id}` | 轮询：`{status, progress, error_message?, download_url?}`；status: queued→resolving→downloading→dewatermarking→done/error |
| GET | `/api/jobs/{id}/download` | 完成后下载去水印 mp4 |
| GET | `/` | 单页前端（frontend-design 产出） |
| GET | `/healthz` | 探活（免鉴权） |

分享链接解析：抽 uuid → 公开端点 `https://labs.google/fx/api/og-video/shared/{uuid}` 直接取 mp4（无需登录）。
ProPainter 只支持 `1280×720` / `720×1280`；其它分辨率返回「仅支持 720p Veo 视频」。

## 运行

**必须从 repo 根启动**（`src` 是 PEP420 namespace 包，需 repo 根在 sys.path）：

```bash
# 常驻（生产）：systemd
sudo cp dewatermark_saas/flow2api-dewatermark-saas.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now flow2api-dewatermark-saas

# 手动（本地调试）
cd /opt/Projects/flow2api
DEWM_HTTP_PROXY=http://127.0.0.1:7890 \
.venv/bin/python -m uvicorn dewatermark_saas.app:app --host 127.0.0.1 --port 18300
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEWM_PORT` | `18300` | 监听端口（仅 127.0.0.1） |
| `DEWM_WORK_DIR` | `/opt/Projects/flow2api/tmp/dewm_saas` | job 工作目录，**必须在 `WM_ALLOWED_DIR` 下** |
| `DEWM_HTTP_PROXY` | 空 | 取 og-video 用的代理；本机部署设 `http://127.0.0.1:7890`（住宅 mihomo） |
| `DEWM_BASIC_USER` / `DEWM_BASIC_PASS` | 空 | Basic Auth 凭证；**都为空则鉴权关闭**（切勿公开暴露） |
| `DEWM_MAX_INPUT_BYTES` | `209715200` | 上传上限（200MB） |
| `DEWM_JOB_TTL_SECONDS` | `3600` | 终态 job 存活时长，过期回收内存条目+工作目录 |

生产凭证与代理放在 root-only `/etc/flow2api-dewm-saas.env`（`EnvironmentFile`，不进 git）。

## 部署拓扑（国内网关范式）

```
https://dewm.ele-yufo.com
→ ECS-BJ OpenResty 443（泛证书 *.ele-yufo.com）
→ JD-Cloud frps 111.228.51.177:38013
→ 2080TI frpc（/etc/frp/frpc.toml: dewatermark-saas 18300→38013, useEncryption）
→ 127.0.0.1:18300
```

ECS-BJ vhost：`/opt/1panel/apps/openresty/openresty/conf/conf.d/dewm.ele-yufo.com.conf`
（含 `client_max_body_size 210m` + `proxy_request_buffering off` 支持上传）。

## 已知限制（Demo 范围，见 spec 后续接缝）

- GPU（ProPainter `:18290`）与 flow2api 主服务共享，串行锁，主服务出片时本服务请求会排队。
- 内存 job store，进程重启丢 job。
- Basic Auth 单租户；无账号/计量/支付（v1 YAGNI）。
- openresty→frps 一跳走公网（frp `useEncryption` 已开）。
