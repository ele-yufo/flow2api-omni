# P3a 叶子模块归位 shared/ Implementation Plan（已执行）

**Goal:** 把已干净、自洽的基础设施模块迁入 `src/shared/`,建成二期去水印 SaaS 可直接复用的地基。零行为变更(shim 保持所有旧 import 可用)。

## 迁移清单（relocate + re-export shim 模式）

| 原位置 | 新位置(shared) | 单例/身份 | 依赖 |
|---|---|---|---|
| `core/logger.py` | `shared/telemetry/` | `debug_logger` 单例唯一 | config |
| `core/auth.py` | `shared/auth/` | `security`/`optional_security` 唯一 | config, fastapi |
| `services/file_cache.py` | `shared/storage/` | 无单例 | config, logger |
| `services/watermark_client.py` | `shared/gpu/` | 无单例 | config, logger(参数注入 file_cache) |

（config 已在 P1 迁入 `shared/config/`。）

## 可提取判据（已验证）

- **shared/ 不 import 任何 core/services/api 业务模块** —— grep 确认为空。shared/ 内部只互相依赖 + 外部库。这是二期能把 shared/ 整包拎出去的编译期判据。
- 每个原文件变 4-13 行 re-export shim,旧 import(`from ..core.logger import debug_logger` 等)全部继续可用,单例仍全局唯一。

## 验证门（已过）

- 全量 112 绿。
- app 全装配 import OK(含 src.main)。
- 迁移中发现并修复:file_cache 内部有 `from ..core.config/logger`(head 截断漏看),已改 shared 内部路径;test_watermark 的 httpx patch 目标跟随模块迁到 shared/gpu。

## YAGNI 延后

- cookie_extractor 暂不迁(可归 shared 但非紧急)。
- database 的 repository 化(P3b)是更大重构,单独进行。
- 消费者从 shim import 改为直接 import shared/,二期接线时再逐步收敛。
