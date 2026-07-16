"""Runtime environment detection for browser captcha (Docker / headed-allow flags).

Extracted from browser_captcha_personal. Pure detection + the derived module-level flags
(computed once at import, same as before). is_truthy_env locked by
tests/characterization/test_captcha_environment.py.
"""
import os


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
