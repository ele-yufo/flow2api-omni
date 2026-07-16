# P2 模型目录数据化 Implementation Plan

> **For agentic workers:** 单原子重构,golden 完全锁定(test_model_catalog)。改结构不改行为。

**Goal:** 把 generation_handler.py 里 ~1180 行硬编码的 `MODEL_CONFIG` 巨表抽成数据文件 + 加载器,generation_handler 立减约 38%。

**Architecture:** 130 条手写 entry → `src/core/model_catalog_data.json`(纯数据);32 条 omni entry 保留 `_build_gemini_omni_entries()` 生成器(声明式、参数集中,勿扁平化);`src/core/model_catalog.py` 加载 JSON + 生成 omni + 组装 `MODEL_CONFIG`。generation_handler 改为 `from ..core.model_catalog import MODEL_CONFIG`。

**验证门:** `test_model_catalog.py` 三个快照(MODEL_CONFIG / OpenAI list / Gemini catalog)必须 **不带 REGEN 严格通过**——字节等价即"数据搬运行为等价"的证明。openai list 是有序 list,故 base 顺序 + omni 追加顺序必须与原一致。

## Global Constraints

- 解释器 `bash scripts/test.sh`(锁 .venv)。
- 不改行为;golden 不 REGEN,必须原样通过。
- MODEL_CONFIG 名保留在 generation_handler 命名空间(routes 经它 re-export,零改动)。

## Tasks

### Task P2.1: 生成 base 数据文件 + catalog 加载器 + 改 generation_handler

**Files:**
- Create: `src/core/model_catalog_data.json`(130 条 base,程序化从 live 导出,原序)
- Create: `src/core/model_catalog.py`(load JSON + omni 常量/builder + 组装 MODEL_CONFIG)
- Modify: `src/services/generation_handler.py`(删 46-1270 巨块,顶部加 import)

**Interfaces:**
- Produces: `core.model_catalog.MODEL_CONFIG: Dict[str, Dict[str, Any]]`(162 条,与原逐字节等价)。

- [ ] Step 1: 程序化导出 base JSON(非 omni、原序)
- [ ] Step 2: 写 model_catalog.py(JSON + omni builder + 组装),独立验证组装结果 == 原 MODEL_CONFIG
- [ ] Step 3: 改 generation_handler(加 import,删巨块)
- [ ] Step 4: golden 严格通过(不 REGEN) + 全量绿
- [ ] Step 5: 确认 generation_handler 行数骤降,commit
