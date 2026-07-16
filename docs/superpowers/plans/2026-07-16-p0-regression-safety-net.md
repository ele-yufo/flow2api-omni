# P0 回归安全网 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可复现、覆盖核心路径的特征测试(characterization)安全网,让后续 P1-P6 重构"只改结构不改行为"有客观校验依据。

**Architecture:** 固定测试解释器与配置(消除"用错 python"陷阱)→ 加隔离 fixture(测试永不碰生产 DB)→ 用 golden-file 特征测试锁住三类会被重构触及的表面:模型目录(锁 P2)、config clamp(锁 P1)、对外 OpenAI/Gemini 协议契约(锁下游消费方)。所有测试离线、确定性、秒级。

**Tech Stack:** pytest 9.x + pytest-subtests + pytest-cov;`.venv`(Python 3.13.5);现有 `unittest.mock` 风格;golden JSON 文件。

## Global Constraints

- 解释器**必须**是 `/opt/Projects/flow2api/.venv/bin/python`,严禁用系统 python(缺 tomli)。
- 测试**严禁**触碰生产库 `data/flow.db`(662MB,live);DB 测试一律用 `tmp_path` 临时库。
- **严禁**重启生产服务(`:8000` pid 存活)或改动 systemd 单元。
- **不改变任何运行时行为**;特征测试锁"当前真实行为",现有行为即使有 bug 也先锁,不在 P0 修。
- 对外契约(`/v1/chat/completions`、`:generateContent`、`:streamGenerateContent`、`/v1/models`、`/models`)是硬约束,黄金样本锁死。
- 全程在独立 git worktree/分支,不在 main 直接改。

---

## 执行前置(worktree 隔离,一次性)

P0 开始前用 `superpowers:using-git-worktrees` 建独立 worktree(当前在 main、且生产服务从主目录运行,物理隔离防止改动泄漏到 live 服务的静态文件/config)。worktree 内用绝对路径解释器 `/opt/Projects/flow2api/.venv/bin/python`,`src` 从 worktree 的 cwd 导入。本文件所有 `pytest`/`python` 命令均指该解释器。

## 路线说明(P0-P6 分批出计划)

本 plan 只覆盖 **P0**。P1-P6 各自在进入该阶段时、基于上一阶段执行揭示的真实调用点,再各出一份 `docs/superpowers/plans/` 计划。spec(`2026-07-16-incremental-refactor-shared-core-design.md`)持有 P0-P6 全局路线。

## File Structure(P0 新增/改动)

- Create: `pytest.ini` — 固定 testpaths、rootdir 导入、addopts。
- Create: `requirements-dev.txt` — 固定 dev 依赖(pytest / pytest-subtests / pytest-cov)。
- Create: `scripts/test.sh` — 唯一正确的测试入口(锁 `.venv` + 覆盖率)。
- Create: `tests/conftest.py` — 共享 fixture(临时 DB、样本请求、golden 助手、生产库守卫)。
- Create: `tests/golden/` — golden JSON 目录(目录 + `.gitkeep`)。
- Create: `tests/characterization/test_model_catalog.py` — 模型目录快照(锁 P2)。
- Create: `tests/characterization/test_model_resolver_golden.py` — 名称解析特征(锁 P2)。
- Create: `tests/characterization/test_account_tiers.py` — 分层逻辑特征。
- Create: `tests/characterization/test_config_clamp.py` — config clamp/默认值特征(锁 P1)。
- Create: `tests/characterization/test_protocol_contract.py` — OpenAI/Gemini 契约黄金样本(锁下游)。
- Create: `tests/characterization/test_db_token_crud.py` — token CRUD 特征(锁 P3,临时库)。
- Create: `docs/superpowers/plans/P0-BASELINE.md` — 覆盖率基线记录(非门禁)。

---

### Task 0.1: 固定测试工具链

**Files:**
- Create: `pytest.ini`
- Create: `requirements-dev.txt`
- Create: `scripts/test.sh`

**Interfaces:**
- Produces: `scripts/test.sh`(项目唯一测试入口,后续所有任务用它跑测试);`pytest.ini` 保证 `import src.*` 可解析。

- [ ] **Step 1: 写 `pytest.ini`**

```ini
[pytest]
testpaths = tests
pythonpath = .
addopts = -q
filterwarnings =
    ignore::DeprecationWarning
```

- [ ] **Step 2: 写 `requirements-dev.txt`**

```
pytest>=9.0,<10
pytest-subtests>=0.13
pytest-cov>=5.0
```

- [ ] **Step 3: 写 `scripts/test.sh`**

```bash
#!/usr/bin/env bash
# 唯一正确的测试入口:锁定 .venv 解释器(系统 python 缺 tomli)。
set -euo pipefail
VENV_PY="/opt/Projects/flow2api/.venv/bin/python"
cd "$(dirname "$0")/.."
exec "$VENV_PY" -m pytest "$@"
```

- [ ] **Step 4: 装 dev 依赖 + 赋可执行**

Run:
```bash
/opt/Projects/flow2api/.venv/bin/python -m pip install -r requirements-dev.txt
chmod +x scripts/test.sh
```
Expected: pytest-cov 装好,无报错。

- [ ] **Step 5: 复验现有测试仍全绿**

Run: `bash scripts/test.sh`
Expected: `103 passed, 32 subtests passed`(或更多),0 failed。

- [ ] **Step 6: Commit**

```bash
git add pytest.ini requirements-dev.txt scripts/test.sh
git commit -m "test(p0): pin .venv interpreter + pytest config + dev deps"
```

---

### Task 0.2: 共享 fixture + golden 助手 + 生产库守卫

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/golden/.gitkeep`

**Interfaces:**
- Produces:
  - fixture `temp_db_path(tmp_path) -> str` — 临时 sqlite 路径。
  - fixture `openai_chat_request() -> dict` — 最小 OpenAI 请求体。
  - fixture `gemini_generate_request() -> dict` — 最小 Gemini 请求体。
  - helper `assert_golden(name: str, actual)` — golden-file 比对;`REGEN_GOLDEN=1` 时写入 `tests/golden/<name>.json`,否则严格比对。
  - autouse guard `_forbid_prod_db` — 任何测试若打开 `data/flow.db` 立即失败。

- [ ] **Step 1: 写 `tests/conftest.py`**

```python
"""Shared fixtures + golden-file characterization helper for P0 safety net."""
import json
import os
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"
PROD_DB = (Path(__file__).parent.parent / "data" / "flow.db").resolve()


def assert_golden(name: str, actual) -> None:
    """Compare `actual` against tests/golden/<name>.json.

    REGEN_GOLDEN=1 writes the golden (first-time capture). Otherwise strict compare.
    Serialization is canonical (sorted keys) so dict ordering never causes churn.
    """
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{name}.json"
    payload = json.dumps(actual, sort_keys=True, ensure_ascii=False, indent=2)
    if os.environ.get("REGEN_GOLDEN") == "1" or not path.exists():
        path.write_text(payload, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    assert payload == expected, (
        f"Golden mismatch for {name!r}. "
        f"If this change is intentional, rerun with REGEN_GOLDEN=1 and review the diff."
    )


@pytest.fixture
def temp_db_path(tmp_path) -> str:
    return str(tmp_path / "test_flow.db")


@pytest.fixture
def openai_chat_request() -> dict:
    return {
        "model": "gemini-3.1-flash-image-landscape",
        "messages": [{"role": "user", "content": "a red apple on a wooden table"}],
        "stream": True,
    }


@pytest.fixture
def gemini_generate_request() -> dict:
    return {
        "contents": [
            {"role": "user", "parts": [{"text": "a red apple on a wooden table"}]}
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"},
        },
    }


@pytest.fixture(autouse=True)
def _forbid_prod_db(monkeypatch):
    """Fail loudly if any test tries to open the live production DB."""
    import sqlite3

    real_connect = sqlite3.connect

    def guarded(database, *args, **kwargs):
        try:
            if Path(str(database)).resolve() == PROD_DB:
                raise AssertionError(
                    f"Test attempted to open production DB {PROD_DB}. Use temp_db_path."
                )
        except (OSError, ValueError):
            pass
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded)
    yield
```

- [ ] **Step 2: 建 golden 目录占位**

Run: `mkdir -p tests/golden && touch tests/golden/.gitkeep`

- [ ] **Step 3: 验证 conftest 不破坏现有测试**

Run: `bash scripts/test.sh`
Expected: 现有 103 tests 仍全绿(新 fixture 是 additive;autouse guard 对不碰 prod DB 的测试无影响)。

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py tests/golden/.gitkeep
git commit -m "test(p0): shared fixtures, golden helper, prod-DB guard"
```

---

### Task 0.3: 模型目录快照(锁 P2 数据化)

**Files:**
- Create: `tests/characterization/__init__.py`
- Create: `tests/characterization/test_model_catalog.py`
- Create: `tests/golden/model_catalog.json`(由 REGEN 生成)
- Create: `tests/golden/openai_model_list.json`
- Create: `tests/golden/gemini_model_catalog.json`

**Interfaces:**
- Consumes: `generation_handler.MODEL_CONFIG`(dict);`routes._get_openai_model_catalog()`、`routes._get_gemini_model_catalog()`。
- Produces: 三个 golden 文件,P2 把 MODEL_CONFIG 迁到数据文件后须保持三者字节等价。

- [ ] **Step 1: 写测试(先建 `__init__.py`)**

`tests/characterization/__init__.py`:(空文件)

`tests/characterization/test_model_catalog.py`:
```python
"""Characterization: lock the full model catalog before P2 data migration."""
from tests.conftest import assert_golden


def test_model_config_snapshot():
    from src.services.generation_handler import MODEL_CONFIG

    snapshot = {k: v for k, v in MODEL_CONFIG.items()}
    assert_golden("model_catalog", snapshot)


def test_openai_model_list_snapshot():
    from src.api.routes import _get_openai_model_catalog

    assert_golden("openai_model_list", _get_openai_model_catalog())


def test_gemini_model_catalog_snapshot():
    from src.api.routes import _get_gemini_model_catalog

    assert_golden("gemini_model_catalog", _get_gemini_model_catalog())
```

- [ ] **Step 2: 首次运行捕获 golden**

Run: `REGEN_GOLDEN=1 bash scripts/test.sh tests/characterization/test_model_catalog.py -v`
Expected: 3 passed;生成 3 个 golden JSON。

- [ ] **Step 3: 人工核验 golden 合理**

Run: `/opt/Projects/flow2api/.venv/bin/python -c "import json;d=json.load(open('tests/golden/model_catalog.json'));print(len(d),'models;', 'gemini_omni_t2v_4s' in d, 'veo_3_1_t2v_fast_landscape' in d)"`
Expected: 打印模型数(应达上百个)且两个已知 key 均为 True。若为空或缺 key,说明捕获错误,排查后重捕。

- [ ] **Step 4: 二次运行验证锁生效**

Run: `bash scripts/test.sh tests/characterization/test_model_catalog.py -v`
Expected: 3 passed(严格比对通过)。

- [ ] **Step 5: Commit**

```bash
git add tests/characterization/__init__.py tests/characterization/test_model_catalog.py tests/golden/model_catalog.json tests/golden/openai_model_list.json tests/golden/gemini_model_catalog.json
git commit -m "test(p0): model catalog golden snapshot (locks P2 data migration)"
```

---

### Task 0.4: model_resolver 名称解析特征(锁 P2)

**Files:**
- Create: `tests/characterization/test_model_resolver_golden.py`
- Create: `tests/golden/model_resolver_cases.json`

**Interfaces:**
- Consumes: `model_resolver.resolve_model_name(model, request=None, model_config=MODEL_CONFIG) -> str`。
- Produces: golden 覆盖 图片(带/不带 aspectRatio/imageSize)、视频(横竖)、omni、已是完整 key、未知名 六类。

- [ ] **Step 1: 写测试**

```python
"""Characterization: lock resolve_model_name input→output across all branches."""
from tests.conftest import assert_golden


class _Req:
    """Minimal stand-in exposing generationConfig like ChatCompletionRequest."""
    def __init__(self, generation_config=None):
        self.generationConfig = generation_config
        self.generation_config = generation_config


def _resolve(model, gen_cfg=None):
    from src.core.model_resolver import resolve_model_name
    from src.services.generation_handler import MODEL_CONFIG

    req = _Req(gen_cfg) if gen_cfg is not None else None
    return resolve_model_name(model=model, request=req, model_config=MODEL_CONFIG)


def test_resolver_golden_matrix(subtests):
    cases = [
        ("img_base_default", "gemini-3.1-flash-image-landscape", None),
        ("img_full_key", "gemini-3.0-pro-image-square-2k", None),
        ("video_base_landscape", "veo_3_1_t2v_fast_landscape", None),
        ("omni_full_key", "gemini_omni_t2v_4s", None),
        ("unknown_model", "this-model-does-not-exist", None),
    ]
    results = {}
    for name, model, gen in cases:
        with subtests.test(case=name):
            results[name] = _resolve(model, gen)
    assert_golden("model_resolver_cases", results)
```

- [ ] **Step 2: 首次捕获**

Run: `REGEN_GOLDEN=1 bash scripts/test.sh tests/characterization/test_model_resolver_golden.py -v`
Expected: passed;生成 `model_resolver_cases.json`。

- [ ] **Step 3: 核验输出合理**

Run: `/opt/Projects/flow2api/.venv/bin/python -c "import json;print(json.load(open('tests/golden/model_resolver_cases.json')))"`
Expected: `unknown_model` 原样返回 `this-model-does-not-exist`;`img_full_key`/`omni_full_key` 原样返回;video/img base 解析成完整 key。若不符,记录实际值(特征测试锁"真实行为",不改代码)。

- [ ] **Step 4: 二次验证 + Commit**

Run: `bash scripts/test.sh tests/characterization/test_model_resolver_golden.py`
```bash
git add tests/characterization/test_model_resolver_golden.py tests/golden/model_resolver_cases.json
git commit -m "test(p0): model_resolver golden matrix (locks P2 resolver)"
```

---

### Task 0.5: account_tiers 分层逻辑特征

**Files:**
- Create: `tests/characterization/test_account_tiers.py`
- Create: `tests/golden/account_tiers.json`

**Interfaces:**
- Consumes: `account_tiers.{normalize_user_paygate_tier, get_paygate_tier_rank, get_paygate_tier_label, get_required_paygate_tier_for_model, supports_model_for_tier}`。

- [ ] **Step 1: 写测试**

```python
"""Characterization: lock paygate tier pure-function behavior."""
from tests.conftest import assert_golden


def test_account_tiers_golden():
    from src.core import account_tiers as at

    tiers = [None, "", "NOT_PAID", "TIER_ONE", "TIER_TWO", "garbage"]
    models = [
        "gemini-3.1-flash-image-landscape",
        "veo_3_1_t2v_fast_landscape",
        "gemini_omni_t2v_4s",
        "veo_3_1_t2v_fast_ultra",
    ]
    out = {
        "normalize": {str(t): at.normalize_user_paygate_tier(t) for t in tiers},
        "rank": {str(t): at.get_paygate_tier_rank(t) for t in tiers},
        "label": {str(t): at.get_paygate_tier_label(t) for t in tiers},
        "required": {m: at.get_required_paygate_tier_for_model(m) for m in models},
        "supports": {
            f"{m}|{t}": at.supports_model_for_tier(m, t)
            for m in models
            for t in ["NOT_PAID", "TIER_ONE", "TIER_TWO"]
        },
    }
    assert_golden("account_tiers", out)
```

- [ ] **Step 2: 捕获 + 核验 + 二次验证**

Run:
```bash
REGEN_GOLDEN=1 bash scripts/test.sh tests/characterization/test_account_tiers.py -v
bash scripts/test.sh tests/characterization/test_account_tiers.py
```
Expected: 两次均 passed。

- [ ] **Step 3: Commit**

```bash
git add tests/characterization/test_account_tiers.py tests/golden/account_tiers.json
git commit -m "test(p0): account_tiers golden (locks tier logic)"
```

---

### Task 0.6: config clamp/默认值特征(锁 P1)

**Files:**
- Create: `tests/characterization/test_config_clamp.py`
- Create: `tests/golden/config_clamp.json`

**Interfaces:**
- Consumes: `core.config.Config`(从临时 toml 构造,不用全局单例);读取 clamp 型 property(如 `flow_timeout`、`flow_max_retries`、`min_credits_to_select` 等)。
- Produces: 锁住"坏输入→兜底值"的 clamp 行为,P1 引入 Settings provider 后须保持同样兜底。

- [ ] **Step 1: 先探明可从临时 toml 构造 Config**

Run:
```bash
/opt/Projects/flow2api/.venv/bin/python - <<'PY'
import inspect
from src.core.config import Config
print("Config.__init__:", inspect.signature(Config.__init__))
print("has _load_config:", hasattr(Config, "_load_config"))
PY
```
Expected: 看到 `__init__(self)` 与 `_load_config`。据此测试用 `monkeypatch` 替换 `_load_config` 返回受控 dict(避免依赖真实 toml 路径)。

- [ ] **Step 2: 写测试**

```python
"""Characterization: lock config clamp/default behavior before P1 Settings refactor."""
from tests.conftest import assert_golden


def _config_with(raw: dict):
    from src.core.config import Config

    cfg = Config.__new__(Config)          # 跳过 __init__ 的文件读取
    cfg._config = raw
    cfg._admin_username = None
    cfg._admin_password = None
    return cfg


def test_config_clamp_golden():
    # 各种坏/边界输入,锁兜底行为
    variants = {
        "empty": {},
        "bad_types": {"flow": {"timeout": "abc", "max_retries": -5}},
        "extreme": {"flow": {"timeout": 1, "max_retries": 999}},
    }
    out = {}
    for name, raw in variants.items():
        cfg = _config_with(raw)
        out[name] = {
            "flow_timeout": cfg.flow_timeout,
            "flow_max_retries": cfg.flow_max_retries,
            "min_credits_to_select": cfg.min_credits_to_select,
        }
    assert_golden("config_clamp", out)
```

- [ ] **Step 3: 捕获 + 核验**

Run: `REGEN_GOLDEN=1 bash scripts/test.sh tests/characterization/test_config_clamp.py -v`
Expected: passed。核验 `flow_timeout` 对 `"abc"` 兜底为 120、对 `1` 兜底为 5(下限);`flow_max_retries` 对 `-5` 兜底为 1。若属性名不存在报 AttributeError,说明该 property 名有出入 —— 用 `grep -n "def min_credits_to_select\|def flow_timeout" src/core/config.py` 校正后再捕获。

- [ ] **Step 4: 二次验证 + Commit**

```bash
bash scripts/test.sh tests/characterization/test_config_clamp.py
git add tests/characterization/test_config_clamp.py tests/golden/config_clamp.json
git commit -m "test(p0): config clamp golden (locks P1 Settings refactor)"
```

---

### Task 0.7: OpenAI/Gemini 协议契约黄金样本(锁下游消费方)

**Files:**
- Create: `tests/characterization/test_protocol_contract.py`
- Create: `tests/golden/openai_models_endpoint.json`
- Create: `tests/golden/gemini_models_endpoint.json`

**Interfaces:**
- Consumes: FastAPI `app`(`src.main:app`)经 `fastapi.testclient.TestClient`,只打**不触发真实生成**的只读端点:`GET /v1/models`、`GET /models`(模型列表)。生成端点需 live token,不在 P0 打(留 live 冒烟阶段)。
- Produces: 两个端点响应的 golden,锁死对外模型清单契约的结构与内容。

**说明:** 用 TestClient 打 app 会触发 `src.main` 的 import 期装配(实例化 DB 等)。为不碰生产库,先探 `Database()` 默认路径是否指向 `data/flow.db`;若是,用 `monkeypatch` 把默认 db 路径改到 `tmp_path`,再建 TestClient。此任务的 Step 1 即验证这一点。

- [ ] **Step 1: 探明 app import 是否碰生产库**

Run:
```bash
/opt/Projects/flow2api/.venv/bin/python - <<'PY'
from src.core.database import Database
import inspect
src = inspect.getsource(Database.__init__)
print(src)
PY
```
Expected: 看到默认 `db_path` 如何决定(是否硬编码 `data/flow.db`)。据输出决定 Step 2 是否需要 monkeypatch 环境变量或路径。**若 import `src.main` 会连生产库或起后台任务,则本任务改为只测 `_get_openai_model_catalog()`/`_get_gemini_model_catalog()` 的纯函数输出(已在 0.3 覆盖),并把端点 golden 降级为直接调用这两个函数包装成端点响应结构** —— 在 Step 2 记录实际决策。

- [ ] **Step 2: 写测试(TestClient 路径,含生产库守卫)**

```python
"""Characterization: lock read-only model-list endpoints (OpenAI + Gemini contract)."""
import pytest
from tests.conftest import assert_golden


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 保证 app 装配期不落到生产库:把默认 DB 路径导到临时目录。
    monkeypatch.setenv("FLOW2API_DB_PATH", str(tmp_path / "app.db"))
    from fastapi.testclient import TestClient
    from src.main import app

    with TestClient(app) as c:
        yield c


def _normalize_models_payload(payload):
    # 去掉可能的易变字段(如 created 时间戳),只锁结构与 id 集合。
    if isinstance(payload, dict) and "data" in payload:
        return {"object": payload.get("object"),
                "ids": sorted(m.get("id") for m in payload["data"])}
    if isinstance(payload, dict) and "models" in payload:
        return {"ids": sorted(m.get("name") for m in payload["models"])}
    return payload


def test_openai_models_endpoint(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    assert_golden("openai_models_endpoint", _normalize_models_payload(r.json()))


def test_gemini_models_endpoint(client):
    r = client.get("/models")
    assert r.status_code == 200
    assert_golden("gemini_models_endpoint", _normalize_models_payload(r.json()))
```

- [ ] **Step 3: 捕获(若 Step 1 判定 TestClient 不可行则走降级方案)**

Run: `REGEN_GOLDEN=1 bash scripts/test.sh tests/characterization/test_protocol_contract.py -v`
Expected: 2 passed;生成 2 golden。若因 `FLOW2API_DB_PATH` 不被识别而触发生产库守卫失败,说明代码不支持该 env —— 改用 `monkeypatch.setattr` 直接替换 `src.main.db` 或按 Step 1 降级方案;记录实际做法。

- [ ] **Step 4: 核验端点真实返回了模型清单**

Run: `/opt/Projects/flow2api/.venv/bin/python -c "import json;print(len(json.load(open('tests/golden/openai_models_endpoint.json'))['ids']))"`
Expected: 模型 id 数为上百量级(与 0.3 快照量级一致)。

- [ ] **Step 5: 二次验证 + Commit**

```bash
bash scripts/test.sh tests/characterization/test_protocol_contract.py
git add tests/characterization/test_protocol_contract.py tests/golden/openai_models_endpoint.json tests/golden/gemini_models_endpoint.json
git commit -m "test(p0): OpenAI/Gemini model-list endpoint golden (locks downstream contract)"
```

---

### Task 0.8: token CRUD 特征(锁 P3 repository 抽取,临时库)

**Files:**
- Create: `tests/characterization/test_db_token_crud.py`
- Create: `tests/golden/db_token_crud.json`

**Interfaces:**
- Consumes: `core.database.Database(db_path=<temp>)`;其 `init_db` / token 增改查异步方法。
- Produces: 锁住 token 增→查→改 的可观察结果,P3 引入 repository 模式后须保持一致。

- [ ] **Step 1: 探明 token CRUD 异步方法真实签名**

Run:
```bash
grep -n "async def .*token\|async def init_db\|async def add_token\|async def get_all_tokens\|async def update_token" src/core/database.py | head -20
```
Expected: 得到 `init_db`、`add_token`、`update_token`、`get_*_tokens*` 的确切名与参数。**据实际签名**填 Step 2 的调用(下方代码按常见签名书写,若不符以 grep 结果为准修正)。

- [ ] **Step 2: 写测试(全程 tmp 库,受生产库守卫保护)**

```python
"""Characterization: lock token add→get→update observable behavior on a temp DB."""
import asyncio

from tests.conftest import assert_golden


def _scrub(row):
    """Drop volatile/secret fields; keep structural + logical fields."""
    if not isinstance(row, dict):
        return row
    drop = {"session_token", "access_token", "created_at", "updated_at",
            "last_used", "last_refresh", "id"}
    return {k: v for k, v in sorted(row.items()) if k not in drop}


def test_token_crud_golden(temp_db_path):
    from src.core.database import Database

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        # 按 Step 1 的真实签名调整以下三行:
        await db.add_token(session_token="ST_SAMPLE_VALUE_ENOUGH_LEN" * 4,
                           name="chartest")
        rows = await db.get_all_tokens_with_stats()
        first = rows[0] if rows else {}
        tok_id = first.get("id")
        if tok_id is not None:
            await db.update_token(tok_id, ban_reason="GRANT_EXPIRED")
        rows2 = await db.get_all_tokens_with_stats()
        return {
            "after_add": [_scrub(r) for r in rows],
            "after_update": [_scrub(r) for r in rows2],
        }

    out = asyncio.run(run())
    assert_golden("db_token_crud", out)
```

- [ ] **Step 3: 捕获 + 核验守卫未误伤**

Run: `REGEN_GOLDEN=1 bash scripts/test.sh tests/characterization/test_db_token_crud.py -v`
Expected: passed;golden 显示 `after_add` 有 1 行、`after_update` 的 `ban_reason` 变为 `GRANT_EXPIRED`。若签名不符报错,按 Step 1 结果修正调用再捕获。

- [ ] **Step 4: 二次验证 + Commit**

```bash
bash scripts/test.sh tests/characterization/test_db_token_crud.py
git add tests/characterization/test_db_token_crud.py tests/golden/db_token_crud.json
git commit -m "test(p0): token CRUD golden on temp DB (locks P3 repositories)"
```

---

### Task 0.9: 覆盖率基线 + P0 收口

**Files:**
- Create: `docs/superpowers/plans/P0-BASELINE.md`

**Interfaces:**
- Produces: 覆盖率基线文档(非门禁,供 P1+ 观察覆盖增长)。

- [ ] **Step 1: 全量跑 + 覆盖率**

Run: `bash scripts/test.sh --cov=src --cov-report=term-missing:skip-covered > /tmp/p0_cov.txt 2>&1; tail -40 /tmp/p0_cov.txt`
Expected: 全部 passed;打印各模块覆盖率。

- [ ] **Step 2: 记录基线**

写 `docs/superpowers/plans/P0-BASELINE.md`:
```markdown
# P0 安全网基线(2026-07-16)

## 测试入口
`bash scripts/test.sh`(锁 .venv;严禁系统 python)

## 特征测试覆盖(锁定的重构表面)
- 模型目录快照 → 锁 P2 数据化
- model_resolver 解析矩阵 → 锁 P2 resolver
- account_tiers → 锁分层逻辑
- config clamp → 锁 P1 Settings
- OpenAI/Gemini 模型清单端点 → 锁对外契约
- token CRUD(临时库) → 锁 P3 repository

## 覆盖率基线
（粘贴 Step 1 term 覆盖率汇总的关键行:总计 % + 4 巨核各自 %）

## 已知未覆盖(留待后续阶段 live 冒烟)
- 生成主流程(需 live token,账号池 1 无封禁号,grant 状态待验)
- 去水印 GPU(需 :18290 服务)
- 打码/浏览器(需有头环境)
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/P0-BASELINE.md
git commit -m "docs(p0): regression safety-net baseline"
```

---

## Self-Review(已执行)

- **Spec 覆盖**:P0 spec §5 测试策略 1(特征测试锁 4 巨核)→ Tasks 0.3/0.4/0.7/0.8;策略 3(协议黄金样本)→ 0.7;策略 4(shared 离线单测)→ 全部离线;"修测试环境"→ 0.1。覆盖完整。
- **占位符扫描**:每个 code step 有真实代码;golden 值走"运行捕获"合法特征技术,非占位。0.7/0.8 因需先探真实签名/装配,已把探测作为该任务 Step 1 并给出降级路径,非 TODO。
- **类型一致**:`assert_golden(name, actual)` 在 0.2 定义,0.3-0.8 一致调用;`temp_db_path` fixture 名一致;`resolve_model_name(model, request, model_config)` 与源码签名一致。
- **风险点显式标注**:0.7(app 装配可能碰生产库)、0.8(异步签名需 grep 校正)均在 Step 1 设探测门 + 降级方案,不盲写。
