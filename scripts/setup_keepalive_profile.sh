#!/usr/bin/env bash
# Documented operator command: setup_keepalive_profile.sh <token_id> [display]
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
PYTHON_BIN="${FLOW2API_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
    printf 'Configured Python interpreter is not executable: %s\n' "$PYTHON_BIN" >&2
    exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/setup_keepalive_profile.py" "$@"
