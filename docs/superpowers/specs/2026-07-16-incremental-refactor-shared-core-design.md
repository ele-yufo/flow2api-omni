# 渐进式重构 + 可提取 shared core 设计

- 日期：2026-07-16
- 状态：设计已批准，待写实施计划
- 作者：yufo + Claude Code
- 关联二期：去水印 SaaS（Web + API，独立新服务）

## 1. 背景与动机

flow2api 是一个把 Google Flow/Veo 视频生成逆向封装成 OpenAI/Gemini 兼容 API 的
FastAPI 服务，开发早期未遵循软件工程最佳实践，已出现屎山化风险。

### 1.1 屎山诊断（体检结论）

`src/` 下约 19,300 行 Python，**4 个文件占约 70%**：

| 文件 | 行数 | 病灶 |
|---|---|---|
| `src/services/browser_captcha_personal.py` | 3654 | 最脏。依赖自举(pip install)、给 nodriver 打**猴子补丁**、tab 池、生命周期、打码算法五种关注点揉一起 |
| `src/services/generation_handler.py` | 3138 | 上帝对象。其中约 **1200 行是硬编码的 `MODEL_CONFIG` 巨表**(46-1270 行)；`_poll_video_result` 单方法约 500 行(2420-2926) |
| `src/services/flow_client.py` | 3095 | 传输层 / 打码耦合 / 业务动作三层揉一个类；curl_cffi 与 urllib 两套 HTTP 实现并存 |
| `src/core/database.py` | 1830 | aiosqlite + 手写裸 SQL，无 ORM，13 张表(其中 8 张单行配置表)，手写迁移系统 |
| `src/api/admin.py` | 1820 | 约 60 个 JSON 端点平铺，且重复实现了 flow_client 里的远程浏览器 HTTP 逻辑 |

**头号耦合根**：`config = Config()` 全局单例(`src/core/config.py:726`)，被 19 处直接
`from ..core.config import config` 消费，且**可变**——`database.reload_config_to_memory`
会把 DB 值回灌进内存 config，配置真相在 toml / DB / 内存三处漂移。这是"多租户/多实例
几乎不可能"的根因。

**装配方式**：手动全局单例 + push 式 setter 注入(`src/main.py:190-207`)，非 DI 框架。

**去水印链路(全项目最干净)**：`src/services/watermark_client.py`(54 行瘦客户端)通过
HTTP + 本地文件路径调用 `dewatermark/server.py`(192 行独立 GPU 进程，ProPainter 常驻
显存)，两者零代码依赖。仅在 `generation_handler._poll_video_result`(约 2833 行)对
Pro(TIER_ONE) 720p 视频调用一次，失败回退原 URL。

**测试现状**：`tests/` 9 个文件约 1443 行，用 `unittest.mock`，覆盖面偏窄(纯函数为主)，
三个巨型核心几乎零覆盖。**当前测试跑不起来**：Python 3.12 环境 `ModuleNotFoundError: tomli`，
9 个测试全部在收集阶段失败。回归安全绳目前是断的。

### 1.2 两期目标

- **一期(本 spec)**：按最佳实践渐进式重构，保证代码质量与可扩展性，并为二期铺路。
- **二期(未来)**：把去水印做成 SaaS，同时提供 **Web 端**与 **API** 两种入口，作为**独立
  新服务**部署。能力范围只做 Veo/Flow 水印(不做通用视频去水印)。后续可能把 flow2api
  整体 SaaS 化。

### 1.3 已确认的关键决策

1. **重构策略**：渐进式，先抽公共地基（非全量重写，非仅聚焦去水印链路）。
2. **SaaS 边界**：去水印 SaaS 是独立新服务（非 flow2api 内模块，非 monorepo 分应用）。
3. **落地形态**：方案 A —— 仓内分层 + **可提取的 shared core**。二期新服务把 core 当本地
   pip 包 / git submodule 依赖复用。
4. **能力范围**：去水印只做 Veo/Flow 固定位置 sparkle 水印。

### 1.4 硬约束

- **对外 API 契约不可破**：现有 OpenAI(`/v1/chat/completions`) 与 Gemini
  (`:generateContent` / `:streamGenerateContent`) 契约有多个生产系统在调用（知人 Beta 视频
  生成、ele-yufo 博客生图、公众号管线），必须向后兼容。
- **不改变现有运行时行为**：重构只改结构不改行为；现有行为即使有 bug 也先用特征测试锁住，
  行为变更走单独变更，不混进重构。
- **两个旁挂常驻进程不在本次重构范围**：`flow2api-keepalive.service`(nodriver 保活)、
  `dewatermark/flow2api-dewatermark.service`(ProPainter GPU)保持独立部署。

## 2. 目标架构

核心思路：把"跟 Flow 业务无关的通用能力"从"Flow 逆向业务"里剥出来。前者收敛成一个自洽的
`shared/`，后者只消费 `shared/` 的接口。

```
src/
  shared/                    # 可提取地基（二期独立服务直接复用）
    config/     Settings 不可变对象 + Provider（统一 toml+DB 单一真相来源）
    db/         engine + repositories/（每实体一个 repo，消灭裸 SQL 模板重复）
    auth/       API key / 管理员鉴权（预留"外部用户"user/plan/quota 挂载点）
    storage/    file_cache 泛化（下载/缓存/代理/签名 URL/过期清理）
    tasks/      异步任务编排范式（状态机 + 进度）
    gpu/        dewatermark 客户端泛化（二期去水印 SaaS 直接调）
    telemetry/  logger + 脱敏
  flow2api/                  # 应用层，只依赖 shared 的接口
    api/        routes（OpenAI/Gemini 双协议） + admin（拆分）
    catalog/    模型目录数据化（MODEL_CONFIG → 数据文件 + ModelCatalog 加载器）
    generation/ generation_handler 拆分（编排 / 轮询 / 拼接延长 / SSE / 日志）
    flow/       flow_client 拆分（transport / actions）
    captcha/    browser_captcha 拆分（自举 / 补丁 / tab 池 / 生命周期 / 算法）
    tokens/     token_manager / load_balancer / concurrency
```

### 2.1 "可提取"的两条物理判据

1. **`shared/` 内部不 import 任何 `flow2api/` 业务模块**——这是可提取的编译期判据。
2. **`shared/` 各模块可纯离线单测**（不依赖 Flow 逆向、不依赖真实网络/浏览器）——这是
   可提取的运行期判据。

### 2.2 YAGNI 边界

一期**不物理打包** `shared/` 成 pip 包，只把边界重构到"随时能拎出去"的程度。真正的物理
提取（建独立包 / submodule）等二期开工、需求确定后再做，避免凭空猜二期接口而过度设计。

## 3. 分阶段路线（每阶段一个可验证的小胜利，可独立回滚发版）

| 阶段 | 内容 | 风险 | 排此顺序的理由 |
|---|---|---|---|
| **P0 安全网** | 修测试环境(`tomli`→`tomllib`/依赖装齐/加 `conftest.py`)、9 个测试跑绿、给 4 个巨核补**特征测试**锁住现有行为、加本地 CI 钩子 | — | 渐进重构的安全绳，断了不能动刀 |
| **P1 config 去全局化** | 引入不可变 `Settings` + `Provider`，统一 toml+DB 两来源为单一真相；19 处直 import 逐个改注入，留兼容 shim 渐进迁移 | 中 | 耦合根，解锁多实例/多租户，回报最高 |
| **P2 模型目录数据化** | MODEL_CONFIG(约1200行) + resolver 映射表 → 数据文件 + `ModelCatalog` 加载器 | 低 | 纯搬运零逻辑，generation_handler 立减约 38% |
| **P3 抽 shared 地基** | database 引 repository 模式消除裸 SQL 重复；file_cache/auth/logger/watermark 归位 `shared/` | 中 | 地基就位，二期能复用 |
| **P4 拆 generation_handler** | `_poll_video_result`(约500行)拆轮询/拼接延长/去水印钩子/SSE 组包/DB 日志；`handle_generation` 拆图/视频分派 | 中高 | 靠 P0 特征测试保驾 |
| **P5 拆 flow_client** | transport(去 urllib 重复实现) / actions 分层；captcha 依赖注入 | 中高 | — |
| **P6 拆 browser_captcha** | 依赖自举 / nodriver 猴补丁 / tab 池 / 生命周期 / 打码算法 分离 | 高 | 最脏最脆弱、最难测、与二期无关，放最后 |

### 3.1 阶段间的可交付性

- 每个 P 都是**独立可发版**的：任何阶段可停下来发布，不留半成品。
- **二期去水印 SaaS 做完 P0→P3 就能开工**（地基干净、可复用）；P4-P6 是 flow2api 内部
  质量改进，不阻塞二期。
- 每个 PR 只动一个 P 的一部分，测试绿才合，保持可回滚粒度。

## 4. 二期独立 SaaS 的预留接缝（一期就埋，不空想）

去水印 SaaS = **Web 上传 + API**，是典型的"异步任务 + 用户配额"系统。一期 `shared/` 每块
精确对应它的需求：

| shared 模块 | 二期用途 | 一期一动作 |
|---|---|---|
| `shared/gpu` | 去水印核心引擎 | dewatermark 客户端泛化，二期零改动复用 |
| `shared/tasks` | Web 上传去水印(上传→排队→GPU→回调)的异步任务 | 把 generation 轮询逻辑抽象成通用任务范式 |
| `shared/storage` | 上传接收 / 结果签名 URL / 过期清理 | file_cache 泛化 |
| `shared/auth` | 外部用户鉴权(user/plan/quota) | **只预留挂载点接口，不实现**，二期填肉 |
| `shared/config` | 多租户 per-tenant 配置 | Provider 支持 per-tenant 覆盖 |

**唯一不复用**：flow2api 的打码(P6)——去水印不碰 Google，不需要 captcha。这也是 P6 放最后
不影响二期的原因。

## 5. 测试与验证策略（渐进重构的命门）

1. **P0 先补特征测试(characterization tests)**：重构前用当前代码的真实输入输出锁住行为
   （哪怕现有行为有 bug 也先锁，重构只改结构不改行为）。覆盖 4 个巨核的关键路径：
   generation 主流程、flow_client 业务动作、routes 双协议转换、database CRUD/迁移。
2. **每个 PR 单一阶段、可回滚**：一次只动一个 P 的一部分，测试绿才合。
3. **契约不破**：特征测试里包含 OpenAI/Gemini 协议层**黄金样本**，锁死对外契约。
4. **`shared/` 单测隔离**：地基模块不依赖业务，纯离线单测——这是"可提取"的第二判据。
5. **验证手段**：优先端到端驱动真实流程观察行为，而非只跑单测/类型检查。

## 6. 非目标（本次不做）

- 不做全量重写。
- 不做通用视频去水印（只 Veo/Flow 固定位置 sparkle）。
- 不物理打包 shared 成 pip 包（留给二期）。
- 不改造成 monorepo。
- 不改动两个旁挂常驻进程(keepalive / dewatermark GPU)的部署。
- 一期不实现二期的 SaaS 业务功能（用户体系、计费、Web 前端），只预留接缝。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 重构引入回归，破坏生产调用方 | P0 特征测试 + 协议黄金样本先行；每 PR 可回滚 |
| config 去全局化触及 19 处，改动面大 | 保留兼容 shim，逐处迁移，非一次性替换 |
| P4-P6 边际收益递减、耗时长 | 允许一期只做 P0-P3，P4-P6 转常态化改进 |
| 抽 shared 时过度设计 | YAGNI：只重构到边界清晰，不提前物理打包 |
| 巨核拆分时行为漂移 | 只改结构不改行为；行为变更走独立变更 |
