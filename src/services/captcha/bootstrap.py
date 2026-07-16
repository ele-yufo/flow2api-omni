"""Browser-captcha dependency bootstrap — nodriver/playwright auto-install + chromium path.

Extracted from browser_captcha_personal. Subprocess-heavy setup (pip install, browser
path detection); moved verbatim. browser_captcha re-imports these.
"""
import os
import subprocess
import sys
from typing import Optional

from ...shared.telemetry import debug_logger


def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """运行 pip install 命令
    
    Args:
        package: 包名
        use_mirror: 是否使用国内镜像
    
    Returns:
        是否安装成功
    """
    cmd = [sys.executable, '-m', 'pip', 'install', package]
    if use_mirror:
        cmd.extend(['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])
    
    try:
        debug_logger.log_info(f"[BrowserCaptcha] 正在安装 {package}...")
        print(f"[BrowserCaptcha] 正在安装 {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ {package} 安装成功")
            print(f"[BrowserCaptcha] ✅ {package} 安装成功")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] {package} 安装异常: {e}")
        return False


def _ensure_nodriver_installed() -> bool:
    """确保 nodriver 已安装
    
    Returns:
        是否安装成功/已安装
    """
    try:
        import nodriver
        debug_logger.log_info("[BrowserCaptcha] nodriver 已安装")
        return True
    except ImportError:
        pass
    
    debug_logger.log_info("[BrowserCaptcha] nodriver 未安装，开始自动安装...")
    print("[BrowserCaptcha] nodriver 未安装，开始自动安装...")
    
    # 先尝试官方源
    if _run_pip_install('nodriver', use_mirror=False):
        return True
    
    # 官方源失败，尝试国内镜像
    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install('nodriver', use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ nodriver 自动安装失败，请手动安装: pip install nodriver")
    print("[BrowserCaptcha] ❌ nodriver 自动安装失败，请手动安装: pip install nodriver")
    return False


def _run_playwright_install(use_mirror: bool = False) -> bool:
    """安装 playwright chromium 浏览器，复用 browser 模式的安装方式。"""
    cmd = [sys.executable, '-m', 'playwright', 'install', 'chromium']
    env = os.environ.copy()

    if use_mirror:
        env['PLAYWRIGHT_DOWNLOAD_HOST'] = 'https://npmmirror.com/mirrors/playwright'

    try:
        debug_logger.log_info("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        print("[BrowserCaptcha] 正在安装 chromium 浏览器...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if result.returncode == 0:
            debug_logger.log_info("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            print("[BrowserCaptcha] ✅ chromium 浏览器安装成功")
            return True

        debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装失败: {result.stderr[:200]}")
        return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] chromium 安装异常: {e}")
        return False


def _ensure_playwright_installed() -> bool:
    """确保 playwright 可用，便于复用其 chromium 二进制。"""
    try:
        import playwright  # noqa: F401
        debug_logger.log_info("[BrowserCaptcha] playwright 已安装")
        return True
    except ImportError:
        pass

    debug_logger.log_info("[BrowserCaptcha] playwright 未安装，开始自动安装...")
    print("[BrowserCaptcha] playwright 未安装，开始自动安装...")

    if _run_pip_install('playwright', use_mirror=False):
        return True

    debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
    if _run_pip_install('playwright', use_mirror=True):
        return True

    debug_logger.log_error("[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright")
    print("[BrowserCaptcha] ❌ playwright 自动安装失败，请手动安装: pip install playwright")
    return False


def _detect_playwright_browser_path() -> Optional[str]:
    """读取 playwright 管理的 chromium 可执行文件路径。"""
    detect_script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    print(p.chromium.executable_path or '')\n"
    )
    env = os.environ.copy()
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0") or "0")

    try:
        result = subprocess.run(
            [sys.executable, "-c", detect_script],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        browser_path_lines = (result.stdout or "").strip().splitlines()
        browser_path = browser_path_lines[-1].strip() if browser_path_lines else ""
        if result.returncode == 0 and browser_path and os.path.exists(browser_path):
            debug_logger.log_info(f"[BrowserCaptcha] 检测到 playwright chromium: {browser_path}")
            return browser_path

        stderr_text = (result.stderr or "").strip()
        if stderr_text:
            debug_logger.log_warning(f"[BrowserCaptcha] 检测 playwright chromium 失败: {stderr_text[:200]}")
    except Exception as e:
        debug_logger.log_info(f"[BrowserCaptcha] 检测 playwright chromium 时出错: {e}")

    return None


def _ensure_playwright_browser_path() -> Optional[str]:
    """确保存在可复用的 chromium 二进制，并返回路径。"""
    browser_path = _detect_playwright_browser_path()
    if browser_path:
        return browser_path

    if not _ensure_playwright_installed():
        return None

    debug_logger.log_info("[BrowserCaptcha] playwright chromium 未安装，开始自动安装...")
    print("[BrowserCaptcha] playwright chromium 未安装，开始自动安装...")

    if not _run_playwright_install(use_mirror=False):
        debug_logger.log_info("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
        print("[BrowserCaptcha] 官方源安装失败，尝试国内镜像...")
        if not _run_playwright_install(use_mirror=True):
            debug_logger.log_error("[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium")
            print("[BrowserCaptcha] ❌ chromium 浏览器自动安装失败，请手动安装: python -m playwright install chromium")
            return None

    return _detect_playwright_browser_path()
