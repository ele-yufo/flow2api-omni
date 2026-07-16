"""Architectural fitness guard: src/shared/ MUST stay extractable.

二期"去水印独立 SaaS"的地基前提:shared/ 是一套可整体搬走的通用核心,
不得依赖任何 flow2api 业务模块(src.core / src.services / src.api)。

本测试是可执行契约——任何后续重构若让 shared 反向依赖业务层,这里立刻红。
既做静态 import 扫描(捕获"写下但未执行的" import),也做运行时导入泄漏检查
(捕获传递依赖),双保险。
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

SHARED_ROOT = Path(__file__).resolve().parents[2] / "src" / "shared"
BUSINESS_PREFIXES = ("src.core", "src.services", "src.api")
# shared 内部允许的相对上跳目标(均为 shared 的直接子包)
SHARED_INTERNAL_SIBLINGS = {"config", "telemetry", "auth", "storage", "gpu", "db", "shared"}

# 覆盖 shared/ 下每个可导入模块(以点路径给出)
SHARED_IMPORTABLE_MODULES = [
    "src.shared.async_utils",
    "src.shared.proxy_parse",
    "src.shared.config",
    "src.shared.telemetry",
    "src.shared.auth",
    "src.shared.storage.cache_helpers",
    "src.shared.storage.media_types",
    "src.shared.storage.file_cache",
    "src.shared.gpu.watermark_client",
    "src.shared.db",
]


def _iter_shared_py_files():
    return sorted(SHARED_ROOT.rglob("*.py"))


def _module_dotted_prefix(py_file: Path) -> str:
    """把 shared/auth/auth.py -> 该文件所在包的层级数,用于解析相对 import 越界。"""
    rel = py_file.relative_to(SHARED_ROOT.parent.parent)  # 相对仓库 src 的上一层
    return ".".join(rel.with_suffix("").parts)


def test_no_static_business_imports_in_shared():
    """静态扫描:shared/ 源码里不得出现指向业务模块的 import 语句。"""
    offenders = []
    for py_file in _iter_shared_py_files():
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # 绝对 import: from src.core.x import ...
                mod = node.module or ""
                if mod.startswith(("src.core", "src.services", "src.api",
                                   "core.", "services.", "api.")) or \
                   mod in ("core", "services", "api"):
                    offenders.append(f"{py_file.name}:{node.lineno} from {mod}")
                # 相对上跳 level>=2: from ..X —— X 必须是 shared 内部兄弟
                if node.level >= 2:
                    top = (mod.split(".")[0] if mod else "")
                    if top and top not in SHARED_INTERNAL_SIBLINGS:
                        offenders.append(
                            f"{py_file.name}:{node.lineno} from {'.'*node.level}{mod} (越出 shared)"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(("src.core", "src.services", "src.api")):
                        offenders.append(f"{py_file.name}:{node.lineno} import {alias.name}")
    assert not offenders, "shared/ 出现指向业务模块的 import:\n" + "\n".join(offenders)


def test_import_pulls_no_business_module():
    """运行时:在全新子进程里逐个导入 shared 模块,sys.modules 不得含任何业务模块。

    用子进程而非当前解释器 —— 避免污染 sys.modules 影响其它测试(曾因
    del sys.modules 破坏 test_alert_notifier 的字符串 patch 目标)。
    """
    repo_root = Path(__file__).resolve().parents[2]
    probe = (
        "import importlib, json, sys\n"
        f"mods = {SHARED_IMPORTABLE_MODULES!r}\n"
        f"biz = {BUSINESS_PREFIXES!r}\n"
        "out = {}\n"
        "for m in mods:\n"
        "    for k in [k for k in sys.modules if k.startswith(biz)]:\n"
        "        del sys.modules[k]\n"
        "    importlib.import_module(m)\n"
        "    leaked = sorted(k for k in sys.modules if k.startswith(biz))\n"
        "    if leaked: out[m] = leaked\n"
        "print(json.dumps(out))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"探针子进程失败:\n{result.stderr}"
    violations = json.loads(result.stdout.strip().splitlines()[-1])
    assert not violations, f"shared 模块传递性拉入业务模块: {violations}"
