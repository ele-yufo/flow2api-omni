"""Architectural fitness guard: src/ must have zero undefined names.

重构中把代码抽到新模块时,极易漏带原文件的 module-level import/全局
(如 types / NODRIVER_AVAILABLE / uc),而离线 mock 测试常常覆盖不到那条真实
执行路径 → 只有 live 才炸(NameError)。本测试用 pyflakes 静态扫全 src 树的
'undefined name',把这类 bug 挡在提交前。

(本守卫因一次 live 冒烟连环抓出 3 个 undefined-name 回归而新增。)
"""
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"


def test_src_has_no_undefined_names():
    pyflakes = None
    try:
        import pyflakes  # noqa: F401
        pyflakes = True
    except ImportError:
        pyflakes = False
    if not pyflakes:
        pytest.skip("pyflakes 未安装(pip install pyflakes)")

    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(SRC)],
        capture_output=True, text=True,
    )
    undefined = [
        line for line in (result.stdout + result.stderr).splitlines()
        if "undefined name" in line
    ]
    assert not undefined, "src/ 出现 undefined name(抽取时漏带 import/全局):\n" + "\n".join(undefined)
