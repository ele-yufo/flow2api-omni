#!/bin/sh
set -eu

echo "[entrypoint] starting flow2api (headless browser mode)"
exec python main.py
