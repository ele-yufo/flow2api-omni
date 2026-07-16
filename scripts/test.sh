#!/usr/bin/env bash
# 唯一正确的测试入口:锁定 .venv 解释器(系统 python 缺 tomli)。
set -euo pipefail
VENV_PY="/opt/Projects/flow2api/.venv/bin/python"
cd "$(dirname "$0")/.."
exec "$VENV_PY" -m pytest "$@"
