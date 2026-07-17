# Flow2API 架构与模块地图

> 本文档描述 `refactor/shared-core` 分支上**渐进式重构**后的代码分层与模块划分。
> 重构为**行为保持**（只改结构不改行为），对外 API 契约、部署方式、配置、支持的模型均不变——
> 因此面向用户的 `README.md` 无需改动。本文档面向**维护者**与**二期（去水印独立 SaaS）**规划。

## 分层总览

```
src/
├── shared/          # 通用核心 —— 不依赖任何业务模块，可整体提取（二期地基）
├── services/        # 业务逻辑（生成编排、Flow 客户端、打码、token）
├── core/            # 数据层（Database 组合根 + repositories + 配置 + 模型目录）
├── api/             # HTTP 层（FastAPI 路由 + 管理端 + 协议转换）
└── main.py          # 组合根：构造依赖、装配 FastAPI app、lifespan 启动
```

**依赖方向**：`api` → `services` → `core` → `shared`。`shared/` 是叶子层，**禁止**反向依赖
`core`/`services`/`api`。该约束由 CI 守卫（见下方「架构守卫测试」），不是口头约定。

## `shared/` —— 可提取通用核心（10 模块）

二期去水印独立 SaaS 可整体搬走这一层。已用运行时测试证明：冷启动仅导入 `shared/` 不牵连任何业务模块。

| 模块 | 职责 |
|---|---|
| `shared/config/provider.py` | 配置 provider（`config_path` 参数 = 二期多租户接缝）。`core/config.py` 是 9 行兼容 shim |
| `shared/telemetry/logger.py` | 调试日志 |
| `shared/auth/auth.py` | FastAPI 鉴权依赖 |
| `shared/storage/file_cache.py` | 媒体文件缓存 |
| `shared/storage/media_types.py` | MIME 检测 / 图片转码 |
| `shared/storage/cache_helpers.py` | 缓存纯助手（扩展名猜测 / 下载头 / 错误归一） |
| `shared/gpu/watermark_client.py` | **去水印客户端 `dewatermark_video`（二期核心能力）**：失败即回退原 URL |
| `shared/db/engine.py` | 通用 SQLite 连接层 `SqliteEngine`（Database 继承它） |
| `shared/proxy_parse.py` | 代理行解析 |
| `shared/async_utils.py` | 通用异步超时包装 |

## `services/` —— 业务逻辑

大编排器所在层。已把可干净分离的纯逻辑/请求契约抽成子包（20 个聚焦模块）：

- **`services/generation/`**（3）：`responses`（响应/SSE 格式化）、`state`（结果状态/tier 解析/错误归一）、`response_parsing`（上游响应归一化 + `_poll_video_result` 抽出的纯函数：media-id 归一 / 失败谓词 / video-info 提取 / 轮询进度）
- **`services/flow/`**（5）：`http_headers`（UA/header 构造）、`errors`（网络错误重试分类）、`request_builders`（Flow 请求纯 builder，10 个 `build_*` 函数：t2v/r2v/i2v/图片/upsample/extend 等生成 + 状态/拼接）、`response_parsers`、`transport`（urllib 回退 + 远程打码 HTTP 原语）
- **`services/captcha/`**（9）：`errors` / `proxy` / `evaluate_result` / `environment` / `bootstrap` / `nodriver_patches` / `fetch_helpers` / `cooldown` / `api_solver`
- **`services/tokens/`**（3）：`at_refresh`（AT 刷新阈值）、`project_naming`、`locks`

编排器主体仍在原文件（`generation_handler.py`、`flow_client.py`、`browser_captcha_personal.py`），
通过薄委托调用上述纯函数，**调用点不变、golden 字节等价**。

## `core/` —— 数据层

- `core/database.py`：收敛为**组合根**（schema/迁移 + config 回灌 + 薄委托）
- `core/repositories/`（6）：`TokenStats` / `RequestLog` / `Project` / `Task` / `Config` / `Token`——各仓储共享 `SqliteEngine` 连接层
- `core/model_catalog.py` + `core/model_catalog_data.json`：模型目录（130 手写 + omni builder 生成）
- `core/config.py`：9 行兼容 shim，re-export `shared/config` 的单例

## `api/` —— HTTP 层

`routes.py`（OpenAI/Gemini 兼容端点）、`admin.py`（管理端）、抽出的 `admin_helpers.py`（10 纯 helper）、`protocol_conversion.py`（OpenAI↔Gemini 协议转换）。

## 重构成效（实测，main 基线 vs 当前分支）

| 文件 | main | 当前 | Δ |
|---|---:|---:|---:|
| generation_handler.py | 3138 | 1767 | −43% |
| database.py | 1830 | 843 | −53% |
| flow_client.py | 3095 | 2425 | −21% |
| browser_captcha_personal.py | 3654 | 3106 | −14% |
| admin.py | 1820 | 1597 | −12% |
| routes.py | 882 | 777 | −11% |
| config.py | 685 | 9 | −98% |
| **合计** | **15104** | **10524** | **−30%** |

- 最脏的编排巨核 `_poll_video_result`：**504 → 242 行（−52%）**，拆出 `_handle_video_extend` / `_finalize_video_success` / `_emit_video_failure` / `_persist_video_completion` 等聚焦方法。
- **38 个聚焦模块**：shared(10) + services 子包(20) + repositories(6) + api 抽出(2)。

## 测试与守卫

- `bash scripts/test.sh`（锁 `.venv`，全离线）——当前 **237 passed**，60 个 characterization 测试文件，48 组 golden。
- **架构守卫**：
  - `tests/characterization/test_shared_extractability.py`：静态 AST + 子进程运行时双检，保证 `shared/` 不依赖业务模块（二期前提）。
  - `tests/characterization/test_no_undefined_names.py`：pyflakes 扫全 `src/`，挡「抽模块漏带 import/全局」这类 bug（live 测试曾连环抓出 3 个）。
- **golden 特征化 + 委托 shim** 是重构安全网的核心工艺，模板见 `tests/characterization/`。

## Live 端到端验证（2026-07-17）

用重构分支代码对真实上游跑通：**图片生成**（签名 CDN URL）、**视频生成**（走通 `_poll_video_result` 编排）、
**去水印**（ProPainter 真抹水印、返回本机 `dewm_*.mp4`）。过程中抓到并修复 3 个真实重构回归（undefined name）。

## 当前状态与后续

- ✅ 通用核心可提取性、数据层仓储化、生成管线纯逻辑抽取、最脏编排核心 `_poll_video_result` 拆分——**已完成 + live 验证**。
- ⏳ **待续（一期收尾）**：`handle_generation`（1767 行文件内 362 行方法）、`_handle_image_generation`（290）、`_handle_video_generation`（253）三大编排方法尚未拆分；`_poll_video_result` 的 upsample 段、`flow_client` 的 `_make_request`/`_get_recaptcha_token` 亦未拆。工艺已成熟（先建 mock 特征网再拆），可照 `test_poll_video_*.py` 模板续做。
- 分支 `refactor/shared-core` 尚**未合并 main**。

设计与阶段计划见 `docs/superpowers/specs/2026-07-16-incremental-refactor-shared-core-design.md` 及 `docs/superpowers/plans/`。
