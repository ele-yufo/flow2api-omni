"""Runtime environment detection for browser captcha (Docker / headed-allow flags).

Extracted from browser_captcha_personal. Pure detection + the derived module-level flags
(computed once at import, same as before). is_truthy_env locked by
tests/characterization/test_captcha_environment.py.
"""
import asyncio
import os
import time


def is_running_in_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except Exception:
        pass
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


def is_truthy_env(name: str) -> bool:
    """判断环境变量是否为 true。"""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


IS_DOCKER = is_running_in_docker()
ALLOW_DOCKER_HEADED = (
    is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA")
    or is_truthy_env("ALLOW_DOCKER_BROWSER_CAPTCHA")
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED


async def wait_for_display_ready(display_value: str, timeout_seconds: float = 5.0):
    """Docker 有头模式下等待 Xvfb socket 就绪，避免容器重启后立刻拉起浏览器失败。"""
    if not (IS_DOCKER and display_value and display_value.startswith(":") and os.name == "posix"):
        return

    display_suffix = display_value.split(".", 1)[0].lstrip(":")
    if not display_suffix.isdigit():
        return

    socket_path = f"/tmp/.X11-unix/X{display_suffix}"
    deadline = time.monotonic() + max(0.5, float(timeout_seconds or 0))
    while time.monotonic() < deadline:
        if os.path.exists(socket_path):
            return
        await asyncio.sleep(0.1)

    raise RuntimeError(
        f"DISPLAY={display_value} 对应的 Xvfb socket 未就绪: {socket_path}"
    )
