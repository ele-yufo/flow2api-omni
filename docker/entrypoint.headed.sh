#!/bin/sh
set -eu

export DISPLAY="${DISPLAY:-:99}"
export ALLOW_DOCKER_HEADED_CAPTCHA="${ALLOW_DOCKER_HEADED_CAPTCHA:-true}"
export XVFB_WHD="${XVFB_WHD:-1920x1080x24}"

echo "[entrypoint] starting Xvfb on ${DISPLAY} (${XVFB_WHD})"
Xvfb "${DISPLAY}" -screen 0 "${XVFB_WHD}" -ac -nolisten tcp +extension RANDR >/tmp/xvfb.log 2>&1 &

sleep 1

echo "[entrypoint] starting Fluxbox"
fluxbox >/tmp/fluxbox.log 2>&1 &

if [ -z "${BROWSER_EXECUTABLE_PATH:-}" ]; then
  BROWSER_EXECUTABLE_PATH="$(python - <<'PY'
from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as p:
        print(p.chromium.executable_path)
except Exception:
    print("")
PY
)"
  if [ -n "${BROWSER_EXECUTABLE_PATH}" ]; then
    export BROWSER_EXECUTABLE_PATH
    echo "[entrypoint] browser executable: ${BROWSER_EXECUTABLE_PATH}"
  fi
fi

exec python main.py
