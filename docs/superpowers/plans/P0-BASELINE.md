# P0 安全网基线（2026-07-16）

分支：`refactor/shared-core`（worktree，隔离于 live 服务的主目录）

## 测试入口

```bash
bash scripts/test.sh            # 锁 .venv 解释器;严禁系统 python(缺 tomli)
bash scripts/test.sh --cov=src --cov-report=term-missing:skip-covered
```

golden 重新捕获（仅在确认行为变更是预期时）：`REGEN_GOLDEN=1 bash scripts/test.sh <path>`

## 结果

- **112 passed, 39 subtests passed**（原 103 + 新增 9 个特征测试断言组）
- 全离线、确定性、约 1.8s
- 覆盖率总计 **26%**（8813 stmts / 6479 miss）

## 特征测试覆盖（锁定的重构表面）

| 测试 | 锁定对象 | 锁定阶段 |
|---|---|---|
| `test_model_catalog.py` | 162 模型 MODEL_CONFIG + OpenAI/Gemini 目录 | P2 数据化 |
| `test_model_resolver_golden.py` | resolve_model_name 拼装 + passthrough + fallback | P2 resolver |
| `test_account_tiers.py` | 分级 rank/label/required/门控矩阵 | 分层逻辑 |
| `test_config_clamp.py` | flow_timeout/max_retries/min_credits clamp 兜底 | P1 Settings |
| `test_protocol_contract.py` | OpenAI/Gemini 模型清单响应信封 + 字段 schema | 对外契约(下游) |
| `test_db_token_crud.py` | token add→update 可观察状态(临时库) | P3 repository |

## 覆盖率基线（关键模块）

| 模块 | 覆盖 | 备注 |
|---|---|---|
| `generation_handler.py` | 10% | 巨核,主流程需 live token |
| `browser_captcha_personal.py` | 18% | 巨核,需有头浏览器 |
| `database.py` | 18% | 巨核,CRUD 部分覆盖(0.8) |
| `routes.py` | 23% | 生成路径需 live |
| `admin.py` | 26% | 60 端点,需 app 运行 |
| `flow_client.py` | 27% | 业务动作需 live |
| `config.py` | 51% | clamp 已锁(0.6) |
| `model_resolver.py` | 64% | 解析已锁(0.4) |
| `account_tiers.py` | 100% | 全锁(0.5) |
| `cookie_extractor.py` | 95% | 既有覆盖 |
| `watermark_client.py` | 93% | 既有覆盖 |
| `alert_notifier.py` | 97% | 既有覆盖 |

## 已知未覆盖（留待后续阶段的 live 冒烟）

需要 live 基建，P0 离线阶段无法覆盖，将在对应阶段做真·端到端冒烟：

- **生成主流程**（图片/视频）：需 live token。当前池 7 号（1 无封禁 / 4 GRANT_EXPIRED / 2 ST_REVOKED），grant 活性待验。
- **去水印 GPU**：需 `:18290` ProPainter 常驻服务（当前 active）。
- **打码 / 浏览器**：需有头环境 + 持久化 profile。

## 对后续阶段的意义

- P1（config 去全局化）：改动后 `test_config_clamp` 必须仍绿，且覆盖率不降。
- P2（MODEL_CONFIG 数据化）：迁移后 `test_model_catalog` + `test_model_resolver_golden` 必须字节等价通过——这是"数据搬运行为等价"的客观证明。
- P3（repository 抽取）：`test_db_token_crud` 必须仍绿。
- 对外契约：任何阶段 `test_protocol_contract` 必须仍绿,否则下游(知人/博客/公众号)受影响。
