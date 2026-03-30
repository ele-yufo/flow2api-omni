"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
支持常驻模式：为每个 project_id 自动创建常驻标签页，即时生成 token
"""
import asyncio
import time
import os
import sys
import subprocess
from typing import Optional, Dict, Any

from ..core.logger import debug_logger
from ..core.config import config


# ==================== Docker 环境检测 ====================
def _is_running_in_docker() -> bool:
    """检测是否在 Docker 容器中运行"""
    # 方法1: 检查 /.dockerenv 文件
    if os.path.exists('/.dockerenv'):
        return True
    # 方法2: 检查 cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except:
        pass
    # 方法3: 检查环境变量
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


def _is_truthy_env(name: str) -> bool:
    """判断环境变量是否为 true。"""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DOCKER_HEADED = (
    _is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA")
    or _is_truthy_env("ALLOW_DOCKER_BROWSER_CAPTCHA")
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED


# ==================== nodriver 自动安装 ====================
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


# 尝试导入 nodriver
uc = None
NODRIVER_AVAILABLE = False

if DOCKER_HEADED_BLOCKED:
    debug_logger.log_warning(
        "[BrowserCaptcha] 检测到 Docker 环境，默认禁用内置浏览器打码。"
        "如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb。"
    )
    print("[BrowserCaptcha] ⚠️ 检测到 Docker 环境，默认禁用内置浏览器打码")
    print("[BrowserCaptcha] 如需启用请设置 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb")
else:
    if IS_DOCKER and ALLOW_DOCKER_HEADED:
        debug_logger.log_warning(
            "[BrowserCaptcha] Docker 内置浏览器打码白名单已启用，请确保 DISPLAY/Xvfb 可用"
        )
        print("[BrowserCaptcha] ✅ Docker 内置浏览器打码白名单已启用")
    if _ensure_nodriver_installed():
        try:
            import nodriver as uc
            NODRIVER_AVAILABLE = True
        except ImportError as e:
            debug_logger.log_error(f"[BrowserCaptcha] nodriver 导入失败: {e}")
            print(f"[BrowserCaptcha] ❌ nodriver 导入失败: {e}")


class ResidentTabInfo:
    """常驻标签页信息结构"""
    def __init__(self, tab, project_id: str):
        self.tab = tab
        self.project_id = project_id
        self.recaptcha_ready = False
        self.created_at = time.time()
        self.last_used_at = time.time()  # 最后使用时间
        self.use_count = 0  # 使用次数


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（nodriver 有头模式）
    
    支持两种模式：
    1. 常驻模式 (Resident Mode): 为每个 project_id 保持常驻标签页，即时生成 token
    2. 传统模式 (Legacy Mode): 每次请求创建新标签页 (fallback)
    """

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        self.headless = False  # nodriver 有头模式
        self.browser = None
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        # 使用 None 让 nodriver 自动创建临时目录，避免目录锁定问题
        self.user_data_dir = None

        # 常驻模式相关属性 (支持多 project_id)
        self._resident_tabs: dict[str, 'ResidentTabInfo'] = {}  # project_id -> 常驻标签页信息
        self._resident_lock = asyncio.Lock()  # 保护常驻标签页操作
        self._max_resident_tabs = 5  # 最大常驻标签页数量（支持并发）
        self._idle_tab_ttl_seconds = 600  # 标签页空闲超时(秒)
        self._idle_reaper_task: Optional[asyncio.Task] = None  # 空闲回收任务

        # 兼容旧 API（保留 single resident 属性作为别名）
        self.resident_project_id: Optional[str] = None  # 向后兼容
        self.resident_tab = None                         # 向后兼容
        self._running = False                            # 向后兼容
        self._recaptcha_ready = False                    # 向后兼容
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self._resident_error_streaks: dict[str, int] = {}
        # 自定义站点打码常驻页（用于 score-test）
        self._custom_tabs: dict[str, Dict[str, Any]] = {}
        self._custom_lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    # 启动空闲标签页回收任务
                    cls._instance._idle_reaper_task = asyncio.create_task(
                        cls._instance._idle_tab_reaper_loop()
                    )
        return cls._instance

    async def reload_config(self):
        """热更新配置（从数据库重新加载）"""
        from ..core.config import config
        old_max_tabs = self._max_resident_tabs
        old_idle_ttl = self._idle_tab_ttl_seconds

        self._max_resident_tabs = config.personal_max_resident_tabs
        self._idle_tab_ttl_seconds = config.personal_idle_tab_ttl_seconds

        debug_logger.log_info(
            f"[BrowserCaptcha] Personal 配置已热更新: "
            f"max_tabs {old_max_tabs}->{self._max_resident_tabs}, "
            f"idle_ttl {old_idle_ttl}s->{self._idle_tab_ttl_seconds}s"
        )

    def _check_available(self):
        """检查服务是否可用"""
        if DOCKER_HEADED_BLOCKED:
            raise RuntimeError(
                "检测到 Docker 环境，默认禁用内置浏览器打码。"
                "如需启用请设置环境变量 ALLOW_DOCKER_HEADED_CAPTCHA=true，并提供 DISPLAY/Xvfb。"
            )
        if IS_DOCKER and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Docker 内置浏览器打码已启用，但 DISPLAY 未设置。"
                "请设置 DISPLAY（例如 :99）并启动 Xvfb。"
            )
        if not NODRIVER_AVAILABLE or uc is None:
            raise RuntimeError(
                "nodriver 未安装或不可用。"
                "请手动安装: pip install nodriver"
            )

    async def _idle_tab_reaper_loop(self):
        """空闲标签页回收循环"""
        while True:
            try:
                await asyncio.sleep(30)  # 每30秒检查一次
                current_time = time.time()
                tabs_to_close = []

                async with self._resident_lock:
                    for project_id, resident_info in list(self._resident_tabs.items()):
                        idle_seconds = current_time - resident_info.last_used_at
                        if idle_seconds >= self._idle_tab_ttl_seconds:
                            tabs_to_close.append(project_id)
                            debug_logger.log_info(
                                f"[BrowserCaptcha] project_id={project_id} 空闲 {idle_seconds:.0f}s，准备回收"
                            )

                for project_id in tabs_to_close:
                    await self._close_resident_tab(project_id)
                    self._resident_error_streaks.pop(project_id, None)

            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 空闲标签页回收异常: {e}")

    async def _evict_lru_tab_if_needed(self):
        """如果超过最大标签页数量，使用LRU策略淘汰最久未使用的标签页

        注意：此方法必须在 _resident_lock 内部调用
        """
        if len(self._resident_tabs) < self._max_resident_tabs:
            return

        # 找到最久未使用的标签页
        lru_project_id = None
        lru_last_used = float('inf')

        for project_id, resident_info in self._resident_tabs.items():
            if resident_info.last_used_at < lru_last_used:
                lru_last_used = resident_info.last_used_at
                lru_project_id = project_id

        if lru_project_id:
            debug_logger.log_info(
                f"[BrowserCaptcha] 标签页数量达到上限({self._max_resident_tabs})，"
                f"淘汰最久未使用的 project_id={lru_project_id}"
            )
            await self._close_resident_tab(lru_project_id)
            self._resident_error_streaks.pop(lru_project_id, None)

    async def initialize(self):
        """初始化 nodriver 浏览器"""
        # 检查服务是否可用
        try:
            self._check_available()
        except Exception as e:
            raise

        if self._initialized and self.browser:
            # 检查浏览器是否仍然存活
            try:
                # 尝试获取浏览器信息验证存活
                if self.browser.stopped:
                    debug_logger.log_warning("[BrowserCaptcha] 浏览器已停止，重新初始化...")
                    self._initialized = False
                else:
                    return
            except Exception:
                debug_logger.log_warning("[BrowserCaptcha] 浏览器无响应，重新初始化...")
                self._initialized = False

        try:
            if self.user_data_dir:
                debug_logger.log_info(f"[BrowserCaptcha] 正在启动 nodriver 浏览器 (用户数据目录: {self.user_data_dir})...")
                # 确保 user_data_dir 存在
                os.makedirs(self.user_data_dir, exist_ok=True)
            else:
                debug_logger.log_info(f"[BrowserCaptcha] 正在启动 nodriver 浏览器 (使用临时目录)...")

            browser_executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
            if browser_executable_path:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 使用指定浏览器可执行文件: {browser_executable_path}"
                )

            # 启动 nodriver 浏览器（后台启动，不占用前台）
            config = uc.Config(
                headless=self.headless,
                user_data_dir=self.user_data_dir,
                browser_executable_path=browser_executable_path,
                sandbox=False,
                browser_args=[
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--window-size=1280,720',
                    '--window-position=3000,3000',  # 窗口位置移到屏幕外
                    '--profile-directory=Default',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-sync',
                    '--disable-translate',
                    '--disable-default-apps',
                    '--no-first-run',
                    '--no-default-browser-check',
                ]
            )
            self.browser = await uc.start(config)

            self._initialized = True
            debug_logger.log_info(f"[BrowserCaptcha] ✅ nodriver 浏览器已启动 (Profile: {self.user_data_dir})")

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 浏览器启动失败: {str(e)}")
            raise

    # ========== 常驻模式 API ==========

    async def start_resident_mode(self, project_id: str):
        """启动常驻模式
        
        Args:
            project_id: 用于常驻的项目 ID
        """
        if self._running:
            debug_logger.log_warning("[BrowserCaptcha] 常驻模式已在运行")
            return
        
        await self.initialize()

        self.resident_project_id = project_id
        website_url = "https://labs.google/fx/api/auth/providers"

        debug_logger.log_info(f"[BrowserCaptcha] 启动常驻模式，访问页面: {website_url}")

        # 获取或创建标签页
        tabs = self.browser.tabs
        if tabs and len(tabs) > 0:
            self.resident_tab = tabs[0]
            debug_logger.log_info(f"[BrowserCaptcha] 复用现有标签页")
            await self.resident_tab.get(website_url)
        else:
            debug_logger.log_info(f"[BrowserCaptcha] 创建新标签页")
            self.resident_tab = await self.browser.get(website_url, new_tab=True)
        
        debug_logger.log_info("[BrowserCaptcha] 标签页已创建，等待页面加载...")
        
        # 等待页面加载完成（带重试机制）
        page_loaded = False
        for retry in range(60):
            try:
                await asyncio.sleep(1)
                ready_state = await self.resident_tab.evaluate("document.readyState")
                debug_logger.log_info(f"[BrowserCaptcha] 页面状态: {ready_state} (重试 {retry + 1}/60)")
                if ready_state == "complete":
                    page_loaded = True
                    break
            except ConnectionRefusedError as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 标签页连接丢失: {e}，尝试重新获取...")
                # 标签页可能已关闭，尝试重新创建
                try:
                    self.resident_tab = await self.browser.get(website_url, new_tab=True)
                    debug_logger.log_info("[BrowserCaptcha] 已重新创建标签页")
                except Exception as e2:
                    debug_logger.log_error(f"[BrowserCaptcha] 重新创建标签页失败: {e2}")
                await asyncio.sleep(2)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 等待页面异常: {e}，重试 {retry + 1}/15...")
                await asyncio.sleep(2)
        
        if not page_loaded:
            debug_logger.log_error("[BrowserCaptcha] 页面加载超时，常驻模式启动失败")
            return
        
        # 等待 reCAPTCHA 加载
        self._recaptcha_ready = await self._wait_for_recaptcha(self.resident_tab)
        
        if not self._recaptcha_ready:
            debug_logger.log_error("[BrowserCaptcha] reCAPTCHA 加载失败，常驻模式启动失败")
            return
        
        self._running = True
        debug_logger.log_info(f"[BrowserCaptcha] ✅ 常驻模式已启动 (project: {project_id})")

    async def stop_resident_mode(self, project_id: Optional[str] = None):
        """停止常驻模式
        
        Args:
            project_id: 指定要关闭的 project_id，如果为 None 则关闭所有常驻标签页
        """
        async with self._resident_lock:
            if project_id:
                # 关闭指定的常驻标签页
                await self._close_resident_tab(project_id)
                self._resident_error_streaks.pop(project_id, None)
                debug_logger.log_info(f"[BrowserCaptcha] 已关闭 project_id={project_id} 的常驻模式")
            else:
                # 关闭所有常驻标签页
                project_ids = list(self._resident_tabs.keys())
                for pid in project_ids:
                    resident_info = self._resident_tabs.pop(pid, None)
                    if resident_info and resident_info.tab:
                        try:
                            await resident_info.tab.close()
                        except Exception:
                            pass
                self._resident_error_streaks.clear()
                debug_logger.log_info(f"[BrowserCaptcha] 已关闭所有常驻标签页 (共 {len(project_ids)} 个)")
        
        # 向后兼容：清理旧属性
        if not self._running:
            return
        
        self._running = False
        if self.resident_tab:
            try:
                await self.resident_tab.close()
            except Exception:
                pass
            self.resident_tab = None
        
        self.resident_project_id = None
        self._recaptcha_ready = False

    async def _wait_for_document_ready(self, tab, retries: int = 30, interval_seconds: float = 1.0) -> bool:
        """等待页面文档加载完成。"""
        for _ in range(retries):
            try:
                ready_state = await tab.evaluate("document.readyState")
                if ready_state == "complete":
                    return True
            except Exception:
                pass
            await asyncio.sleep(interval_seconds)
        return False

    def _is_server_side_flow_error(self, error_text: str) -> bool:
        error_lower = (error_text or "").lower()
        return any(keyword in error_lower for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
            "\"reason\":\"internal\"",
            "server error",
            "upstream error",
        ])

    async def _clear_tab_site_storage(self, tab) -> Dict[str, Any]:
        """清理当前站点的本地存储状态，但保留 cookies 登录态。"""
        result = await tab.evaluate("""
            (async () => {
                const summary = {
                    local_storage_cleared: false,
                    session_storage_cleared: false,
                    cache_storage_deleted: [],
                    indexed_db_deleted: [],
                    indexed_db_errors: [],
                    service_worker_unregistered: 0,
                };

                try {
                    window.localStorage.clear();
                    summary.local_storage_cleared = true;
                } catch (e) {
                    summary.local_storage_error = String(e);
                }

                try {
                    window.sessionStorage.clear();
                    summary.session_storage_cleared = true;
                } catch (e) {
                    summary.session_storage_error = String(e);
                }

                try {
                    if (typeof caches !== 'undefined') {
                        const cacheKeys = await caches.keys();
                        for (const key of cacheKeys) {
                            const deleted = await caches.delete(key);
                            if (deleted) {
                                summary.cache_storage_deleted.push(key);
                            }
                        }
                    }
                } catch (e) {
                    summary.cache_storage_error = String(e);
                }

                try {
                    if (navigator.serviceWorker) {
                        const registrations = await navigator.serviceWorker.getRegistrations();
                        for (const registration of registrations) {
                            const ok = await registration.unregister();
                            if (ok) {
                                summary.service_worker_unregistered += 1;
                            }
                        }
                    }
                } catch (e) {
                    summary.service_worker_error = String(e);
                }

                try {
                    if (typeof indexedDB !== 'undefined' && typeof indexedDB.databases === 'function') {
                        const dbs = await indexedDB.databases();
                        const names = Array.from(new Set(
                            dbs
                                .map((item) => item && item.name)
                                .filter((name) => typeof name === 'string' && name)
                        ));
                        for (const name of names) {
                            try {
                                await new Promise((resolve) => {
                                    const request = indexedDB.deleteDatabase(name);
                                    request.onsuccess = () => resolve(true);
                                    request.onerror = () => resolve(false);
                                    request.onblocked = () => resolve(false);
                                });
                                summary.indexed_db_deleted.push(name);
                            } catch (e) {
                                summary.indexed_db_errors.push(`${name}: ${String(e)}`);
                            }
                        }
                    } else {
                        summary.indexed_db_unsupported = true;
                    }
                } catch (e) {
                    summary.indexed_db_errors.push(String(e));
                }

                return summary;
            })()
        """)
        return result if isinstance(result, dict) else {}

    async def _clear_resident_storage_and_reload(self, project_id: str) -> bool:
        """清理常驻标签页的站点数据并刷新，尝试原地自愈。"""
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)

        if not resident_info or not resident_info.tab:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 没有可清理的常驻标签页")
            return False

        try:
            cleanup_summary = await self._clear_tab_site_storage(resident_info.tab)
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 已清理站点存储，准备刷新恢复: {cleanup_summary}"
            )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 清理站点存储失败: {e}")
            return False

        try:
            resident_info.recaptcha_ready = False
            await resident_info.tab.reload()
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 清理后刷新标签页失败: {e}")
            return False

        if not await self._wait_for_document_ready(resident_info.tab, retries=30, interval_seconds=1.0):
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 清理后页面加载超时")
            return False

        resident_info.recaptcha_ready = await self._wait_for_recaptcha(resident_info.tab)
        if resident_info.recaptcha_ready:
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 清理后已恢复 reCAPTCHA")
            return True

        debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 清理后仍无法恢复 reCAPTCHA")
        return False

    async def _recreate_resident_tab(self, project_id: str) -> bool:
        """关闭并重建常驻标签页。"""
        async with self._resident_lock:
            await self._close_resident_tab(project_id)
            resident_info = await self._create_resident_tab(project_id)
            if resident_info is None:
                debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 重建常驻标签页失败")
                return False
            self._resident_tabs[project_id] = resident_info
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 已重建常驻标签页")
            return True

    async def _restart_browser_for_project(self, project_id: str) -> bool:
        """重启整个 nodriver 浏览器，并恢复指定 project 的常驻标签页。"""
        debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 准备重启 nodriver 浏览器以恢复")
        await self.close()
        await self.initialize()

        async with self._resident_lock:
            resident_info = await self._create_resident_tab(project_id)
            if resident_info is None:
                debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 浏览器重启后恢复常驻标签页失败")
                return False
            self._resident_tabs[project_id] = resident_info
            debug_logger.log_warning(f"[BrowserCaptcha] project_id={project_id} 浏览器重启后已恢复常驻标签页")
            return True

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        """上游生成接口异常时，对常驻标签页执行自愈恢复。"""
        if not project_id:
            return

        streak = self._resident_error_streaks.get(project_id, 0) + 1
        self._resident_error_streaks[project_id] = streak
        error_text = f"{error_reason or ''} {error_message or ''}".strip()
        error_lower = error_text.lower()
        debug_logger.log_warning(
            f"[BrowserCaptcha] project_id={project_id} 收到上游异常，streak={streak}, reason={error_reason}, detail={error_message[:200]}"
        )

        if not self._initialized or not self.browser:
            return

        # 403 错误：先清理缓存再重建
        if "403" in error_text or "forbidden" in error_lower or "recaptcha" in error_lower:
            debug_logger.log_warning(
                f"[BrowserCaptcha] project_id={project_id} 检测到 403/reCAPTCHA 错误，清理缓存并重建"
            )
            healed = await self._clear_resident_storage_and_reload(project_id)
            if not healed:
                await self._recreate_resident_tab(project_id)
            return

        # 服务端错误：根据连续失败次数决定恢复策略
        if self._is_server_side_flow_error(error_text):
            recreate_threshold = max(2, int(getattr(config, "browser_personal_recreate_threshold", 2) or 2))
            restart_threshold = max(3, int(getattr(config, "browser_personal_restart_threshold", 3) or 3))

            if streak >= restart_threshold:
                await self._restart_browser_for_project(project_id)
                return
            if streak >= recreate_threshold:
                await self._recreate_resident_tab(project_id)
                return

            healed = await self._clear_resident_storage_and_reload(project_id)
            if not healed:
                await self._recreate_resident_tab(project_id)
            return

        # 其他错误：直接重建标签页
        await self._recreate_resident_tab(project_id)

    async def _wait_for_recaptcha(self, tab) -> bool:
        """等待 reCAPTCHA 加载

        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[BrowserCaptcha] 注入 reCAPTCHA 脚本...")

        # 注入 reCAPTCHA Enterprise 脚本
        await tab.evaluate(f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/recaptcha/enterprise.js?render={self.website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """)

        # 等待 reCAPTCHA 加载（减少等待时间）
        for i in range(15):  # 减少到15次，最多7.5秒
            try:
                is_ready = await tab.evaluate(
                    "typeof grecaptcha !== 'undefined' && "
                    "typeof grecaptcha.enterprise !== 'undefined' && "
                    "typeof grecaptcha.enterprise.execute === 'function'"
                )

                if is_ready:
                    debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA 已就绪 (等待了 {i * 0.5}s)")
                    return True

                await tab.sleep(0.5)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 检查 reCAPTCHA 时异常: {e}")
                await tab.sleep(0.3)  # 异常时减少等待时间

        debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 加载超时")
        return False

    async def _wait_for_custom_recaptcha(
        self,
        tab,
        website_key: str,
        enterprise: bool = False,
    ) -> bool:
        """等待任意站点的 reCAPTCHA 加载，用于分数测试。"""
        debug_logger.log_info("[BrowserCaptcha] 检测自定义 reCAPTCHA...")

        ready_check = (
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
            "typeof grecaptcha.enterprise.execute === 'function'"
        ) if enterprise else (
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
        )
        script_path = "recaptcha/enterprise.js" if enterprise else "recaptcha/api.js"
        label = "Enterprise" if enterprise else "V3"

        is_ready = await tab.evaluate(ready_check)
        if is_ready:
            debug_logger.log_info(f"[BrowserCaptcha] 自定义 reCAPTCHA {label} 已加载")
            return True

        debug_logger.log_info("[BrowserCaptcha] 未检测到自定义 reCAPTCHA，注入脚本...")
        await tab.evaluate(f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/{script_path}?render={website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """)

        await tab.sleep(3)
        for i in range(20):
            is_ready = await tab.evaluate(ready_check)
            if is_ready:
                debug_logger.log_info(f"[BrowserCaptcha] 自定义 reCAPTCHA {label} 已加载（等待了 {i * 0.5} 秒）")
                return True
            await tab.sleep(0.5)

        debug_logger.log_warning("[BrowserCaptcha] 自定义 reCAPTCHA 加载超时")
        return False

    async def _execute_recaptcha_on_tab(self, tab, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """在指定标签页执行 reCAPTCHA 获取 token

        Args:
            tab: nodriver 标签页对象
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            reCAPTCHA token 或 None
        """
        # 生成唯一变量名避免冲突
        ts = int(time.time() * 1000)
        token_var = f"_recaptcha_token_{ts}"
        error_var = f"_recaptcha_error_{ts}"

        execute_script = f"""
            (() => {{
                window.{token_var} = null;
                window.{error_var} = null;

                try {{
                    grecaptcha.enterprise.ready(function() {{
                        grecaptcha.enterprise.execute('{self.website_key}', {{action: '{action}'}})
                            .then(function(token) {{
                                window.{token_var} = token;
                            }})
                            .catch(function(err) {{
                                window.{error_var} = err.message || 'execute failed';
                            }});
                    }});
                }} catch (e) {{
                    window.{error_var} = e.message || 'exception';
                }}
            }})()
        """

        # 注入执行脚本
        await tab.evaluate(execute_script)

        # 轮询等待结果（最多 30 秒）
        token = None
        for i in range(60):
            await tab.sleep(0.5)
            token = await tab.evaluate(f"window.{token_var}")
            if token:
                break
            error = await tab.evaluate(f"window.{error_var}")
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 错误: {error}")
                break

        # 清理临时变量
        try:
            await tab.evaluate(f"delete window.{token_var}; delete window.{error_var};")
        except:
            pass

        if token:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ Token 获取成功 (长度: {len(token)})")
        else:
            debug_logger.log_warning("[BrowserCaptcha] Token 获取失败，清理浏览器缓存...")
            # 打码失败，清理浏览器缓存
            await self._clear_browser_cache()

        return token

    async def _execute_custom_recaptcha_on_tab(
        self,
        tab,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """在指定标签页执行任意站点的 reCAPTCHA。"""
        ts = int(time.time() * 1000)
        token_var = f"_custom_recaptcha_token_{ts}"
        error_var = f"_custom_recaptcha_error_{ts}"
        execute_target = "grecaptcha.enterprise.execute" if enterprise else "grecaptcha.execute"

        execute_script = f"""
            (() => {{
                window.{token_var} = null;
                window.{error_var} = null;

                try {{
                    grecaptcha.ready(function() {{
                        {execute_target}('{website_key}', {{action: '{action}'}})
                            .then(function(token) {{
                                window.{token_var} = token;
                            }})
                            .catch(function(err) {{
                                window.{error_var} = err.message || 'execute failed';
                            }});
                    }});
                }} catch (e) {{
                    window.{error_var} = e.message || 'exception';
                }}
            }})()
        """

        await tab.evaluate(execute_script)

        token = None
        for _ in range(30):
            await tab.sleep(0.5)
            token = await tab.evaluate(f"window.{token_var}")
            if token:
                break
            error = await tab.evaluate(f"window.{error_var}")
            if error:
                debug_logger.log_error(f"[BrowserCaptcha] 自定义 reCAPTCHA 错误: {error}")
                break

        try:
            await tab.evaluate(f"delete window.{token_var}; delete window.{error_var};")
        except:
            pass

        if token:
            post_wait_seconds = 3
            try:
                post_wait_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
            except Exception:
                pass
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] 自定义 reCAPTCHA 已完成，额外等待 {post_wait_seconds:.1f}s 后返回 token"
                )
                await tab.sleep(post_wait_seconds)

        return token

    async def _verify_score_on_tab(self, tab, token: str, verify_url: str) -> Dict[str, Any]:
        """直接读取测试页面展示的分数，避免 verify.php 与页面显示口径不一致。"""
        _ = token
        _ = verify_url
        started_at = time.time()
        timeout_seconds = 25.0
        refresh_clicked = False
        last_snapshot: Dict[str, Any] = {}

        try:
            timeout_seconds = float(getattr(config, "browser_score_dom_wait_seconds", 25) or 25)
        except Exception:
            pass

        while (time.time() - started_at) < timeout_seconds:
            try:
                result = await tab.evaluate("""
                    (() => {
                        const bodyText = ((document.body && document.body.innerText) || "")
                            .replace(/\\u00a0/g, " ")
                            .replace(/\\r/g, "");
                        const patterns = [
                            { source: "current_score", regex: /Your score is:\\s*([01](?:\\.\\d+)?)/i },
                            { source: "selected_score", regex: /Selected Score Test:[\\s\\S]{0,400}?Score:\\s*([01](?:\\.\\d+)?)/i },
                            { source: "history_score", regex: /(?:^|\\n)\\s*Score:\\s*([01](?:\\.\\d+)?)\\s*;/i },
                        ];
                        let score = null;
                        let source = "";
                        for (const item of patterns) {
                            const match = bodyText.match(item.regex);
                            if (!match) continue;
                            const parsed = Number(match[1]);
                            if (!Number.isNaN(parsed) && parsed >= 0 && parsed <= 1) {
                                score = parsed;
                                source = item.source;
                                break;
                            }
                        }
                        const uaMatch = bodyText.match(/Current User Agent:\\s*([^\\n]+)/i);
                        const ipMatch = bodyText.match(/Current IP Address:\\s*([^\\n]+)/i);
                        return {
                            score,
                            source,
                            raw_text: bodyText.slice(0, 4000),
                            current_user_agent: uaMatch ? uaMatch[1].trim() : "",
                            current_ip_address: ipMatch ? ipMatch[1].trim() : "",
                            title: document.title || "",
                            url: location.href || "",
                        };
                    })()
                """)
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

            if isinstance(result, dict):
                last_snapshot = result
                score = result.get("score")
                if isinstance(score, (int, float)):
                    elapsed_ms = int((time.time() - started_at) * 1000)
                    return {
                        "verify_mode": "browser_page_dom",
                        "verify_elapsed_ms": elapsed_ms,
                        "verify_http_status": None,
                        "verify_result": {
                            "success": True,
                            "score": score,
                            "source": result.get("source") or "antcpt_dom",
                            "raw_text": result.get("raw_text") or "",
                            "current_user_agent": result.get("current_user_agent") or "",
                            "current_ip_address": result.get("current_ip_address") or "",
                            "page_title": result.get("title") or "",
                            "page_url": result.get("url") or "",
                        },
                    }

            if not refresh_clicked and (time.time() - started_at) >= 2:
                refresh_clicked = True
                try:
                    await tab.evaluate("""
                        (() => {
                            const nodes = Array.from(
                                document.querySelectorAll('button, input[type="button"], input[type="submit"], a')
                            );
                            const target = nodes.find((node) => {
                                const text = (node.innerText || node.textContent || node.value || "").trim();
                                return /Refresh score now!?/i.test(text);
                            });
                            if (target) {
                                target.click();
                                return true;
                            }
                            return false;
                        })()
                    """)
                except Exception:
                    pass

            await tab.sleep(0.5)

        elapsed_ms = int((time.time() - started_at) * 1000)
        if not isinstance(last_snapshot, dict):
            last_snapshot = {"raw": last_snapshot}

        return {
            "verify_mode": "browser_page_dom",
            "verify_elapsed_ms": elapsed_ms,
            "verify_http_status": None,
            "verify_result": {
                "success": False,
                "score": None,
                "source": "antcpt_dom_timeout",
                "raw_text": last_snapshot.get("raw_text") or "",
                "current_user_agent": last_snapshot.get("current_user_agent") or "",
                "current_ip_address": last_snapshot.get("current_ip_address") or "",
                "page_title": last_snapshot.get("title") or "",
                "page_url": last_snapshot.get("url") or "",
                "error": last_snapshot.get("error") or "未在页面中读取到分数",
            },
        }

    async def _extract_tab_fingerprint(self, tab) -> Optional[Dict[str, Any]]:
        """从 nodriver 标签页提取浏览器指纹信息。"""
        try:
            fingerprint = await tab.evaluate("""
                () => {
                    const ua = navigator.userAgent || "";
                    const lang = navigator.language || "";
                    const uaData = navigator.userAgentData || null;
                    let secChUa = "";
                    let secChUaMobile = "";
                    let secChUaPlatform = "";

                    if (uaData) {
                        if (Array.isArray(uaData.brands) && uaData.brands.length > 0) {
                            secChUa = uaData.brands
                                .map((item) => `"${item.brand}";v="${item.version}"`)
                                .join(", ");
                        }
                        secChUaMobile = uaData.mobile ? "?1" : "?0";
                        if (uaData.platform) {
                            secChUaPlatform = `"${uaData.platform}"`;
                        }
                    }

                    return {
                        user_agent: ua,
                        accept_language: lang,
                        sec_ch_ua: secChUa,
                        sec_ch_ua_mobile: secChUaMobile,
                        sec_ch_ua_platform: secChUaPlatform,
                    };
                }
            """)
            if not isinstance(fingerprint, dict):
                return None

            # personal 模式当前未单独配置浏览器代理，显式使用直连，避免与全局代理混淆。
            result: Dict[str, Any] = {"proxy_url": None}
            for key in ("user_agent", "accept_language", "sec_ch_ua", "sec_ch_ua_mobile", "sec_ch_ua_platform"):
                value = fingerprint.get(key)
                if isinstance(value, str) and value:
                    result[key] = value
            return result
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 提取 nodriver 指纹失败: {e}")
            return None

    # ========== 主要 API ==========

    async def get_token(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """获取 reCAPTCHA token

        自动常驻模式：如果该 project_id 没有常驻标签页，则自动创建并常驻

        Args:
            project_id: Flow项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        debug_logger.log_info(f"[BrowserCaptcha] get_token 开始: project_id={project_id}, action={action}, 当前标签页数={len(self._resident_tabs)}/{self._max_resident_tabs}")

        # 确保浏览器已初始化
        await self.initialize()
        self._last_fingerprint = None

        # 尝试从常驻标签页获取 token
        resident_info = self._resident_tabs.get(project_id)

        # 如果该 project_id 没有常驻标签页，则自动创建
        if resident_info is None:
            async with self._resident_lock:
                # 双重检查，避免并发创建
                resident_info = self._resident_tabs.get(project_id)
                if resident_info is None:
                    debug_logger.log_info(f"[BrowserCaptcha] 开始创建标签页 (project: {project_id}, 当前: {len(self._resident_tabs)}/{self._max_resident_tabs})")
                    # 先检查是否需要淘汰旧标签页
                    await self._evict_lru_tab_if_needed()

                    resident_info = await self._create_resident_tab(project_id)
                    if resident_info is None:
                        debug_logger.log_warning(f"[BrowserCaptcha] 创建标签页失败，fallback 到传统模式 (project: {project_id})")
                        return await self._get_token_legacy(project_id, action)
                    self._resident_tabs[project_id] = resident_info
                    debug_logger.log_info(f"[BrowserCaptcha] ✅ 标签页创建成功 (project: {project_id}, 当前共 {len(self._resident_tabs)}/{self._max_resident_tabs} 个)")
                else:
                    debug_logger.log_info(f"[BrowserCaptcha] 标签页已存在，复用 (project: {project_id})")

        debug_logger.log_info(f"[BrowserCaptcha] 准备执行打码 (project: {project_id}, 标签页使用次数: {resident_info.use_count if resident_info else 0})")

        # 使用常驻标签页生成 token（在锁外执行，避免阻塞）
        if resident_info and resident_info.recaptcha_ready and resident_info.tab:
            start_time = time.time()
            debug_logger.log_info(f"[BrowserCaptcha] 从常驻标签页即时生成 token (project: {project_id}, action: {action})...")
            try:
                token = await self._execute_recaptcha_on_tab(resident_info.tab, action)
                duration_ms = (time.time() - start_time) * 1000
                if token:
                    # 更新使用时间和计数
                    resident_info.last_used_at = time.time()
                    resident_info.use_count += 1
                    self._resident_error_streaks.pop(project_id, None)
                    self._last_fingerprint = await self._extract_tab_fingerprint(resident_info.tab)
                    debug_logger.log_info(
                        f"[BrowserCaptcha] ✅ Token生成成功（耗时 {duration_ms:.0f}ms, 使用次数: {resident_info.use_count}）"
                    )
                    return token
                else:
                    debug_logger.log_warning(f"[BrowserCaptcha] 常驻标签页生成失败 (project: {project_id})，尝试重建...")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 常驻标签页异常: {e}，尝试重建...")

            # 常驻标签页失效，尝试重建（重新获取锁）
            debug_logger.log_info(f"[BrowserCaptcha] 尝试重新获取 resident_lock 进行重建...")
            async with self._resident_lock:
                debug_logger.log_info(f"[BrowserCaptcha] 已获取 resident_lock，开始重建")
                await self._close_resident_tab(project_id)
                resident_info = await self._create_resident_tab(project_id)
                if resident_info:
                    self._resident_tabs[project_id] = resident_info

            debug_logger.log_info(f"[BrowserCaptcha] 已释放 resident_lock，重建完成")

            # 重建后立即尝试生成（在锁外执行）
            if resident_info:
                try:
                    token = await self._execute_recaptcha_on_tab(resident_info.tab, action)
                    if token:
                        resident_info.last_used_at = time.time()
                        resident_info.use_count += 1
                        self._resident_error_streaks.pop(project_id, None)
                        self._last_fingerprint = await self._extract_tab_fingerprint(resident_info.tab)
                        debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Token生成成功")
                        return token
                except Exception:
                    pass

        # 最终 Fallback: 使用传统模式
        debug_logger.log_warning(f"[BrowserCaptcha] 所有常驻方式失败，fallback 到传统模式 (project: {project_id})")
        legacy_token = await self._get_token_legacy(project_id, action)
        if legacy_token:
            self._resident_error_streaks.pop(project_id, None)
        return legacy_token

    async def _create_resident_tab(self, project_id: str) -> Optional[ResidentTabInfo]:
        """为指定 project_id 创建常驻标签页

        Args:
            project_id: 项目 ID

        Returns:
            ResidentTabInfo 对象，或 None（创建失败）
        """
        try:
            # 使用 Flow API 地址作为基础页面
            website_url = "https://labs.google/fx/api/auth/providers"
            debug_logger.log_info(f"[BrowserCaptcha] 为 project_id={project_id} 创建常驻标签页")

            # 检查是否已有该 project_id 的常驻标签页
            existing_tabs = [info.tab for info in self._resident_tabs.values() if info.tab]

            # 获取或创建标签页
            tabs = self.browser.tabs
            available_tab = None

            # 查找未被占用的标签页
            for tab in tabs:
                if tab not in existing_tabs:
                    available_tab = tab
                    break

            if available_tab:
                tab = available_tab
                debug_logger.log_info(f"[BrowserCaptcha] 复用未占用的标签页")
                await tab.get(website_url)
            else:
                debug_logger.log_info(f"[BrowserCaptcha] 创建新标签页")
                tab = await self.browser.get(website_url, new_tab=True)

            # 等待页面加载完成（减少等待时间）
            page_loaded = False
            for retry in range(10):  # 减少到10次，最多5秒
                try:
                    await asyncio.sleep(0.5)
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        page_loaded = True
                        debug_logger.log_info(f"[BrowserCaptcha] 页面已加载")
                        break
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 等待页面异常: {e}，重试 {retry + 1}/10...")
                    await asyncio.sleep(0.3)  # 减少重试间隔

            if not page_loaded:
                debug_logger.log_error(f"[BrowserCaptcha] 页面加载超时 (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return None

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error(f"[BrowserCaptcha] reCAPTCHA 加载失败 (project: {project_id})")
                try:
                    await tab.close()
                except:
                    pass
                return None

            # 创建常驻信息对象
            resident_info = ResidentTabInfo(tab, project_id)
            resident_info.recaptcha_ready = True

            debug_logger.log_info(f"[BrowserCaptcha] ✅ 常驻标签页创建成功 (project: {project_id})")
            return resident_info

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 创建常驻标签页异常: {e}")
            return None

    async def _close_resident_tab(self, project_id: str):
        """关闭指定 project_id 的常驻标签页

        Args:
            project_id: 项目 ID
        """
        resident_info = self._resident_tabs.pop(project_id, None)
        if resident_info and resident_info.tab:
            try:
                await resident_info.tab.close()
                debug_logger.log_info(f"[BrowserCaptcha] 已关闭 project_id={project_id} 的常驻标签页")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 关闭标签页时异常: {e}")

    async def invalidate_token(self, project_id: str):
        """当检测到 token 无效时调用，清除缓存并重建标签页

        Args:
            project_id: 项目 ID
        """
        debug_logger.log_warning(f"[BrowserCaptcha] Token 被标记为无效 (project: {project_id})，清除缓存并重建...")

        # 清除浏览器缓存
        await self._clear_browser_cache()

        # 重建标签页
        async with self._resident_lock:
            await self._close_resident_tab(project_id)
            resident_info = await self._create_resident_tab(project_id)
            if resident_info:
                self._resident_tabs[project_id] = resident_info
                debug_logger.log_info(f"[BrowserCaptcha] ✅ 标签页已重建 (project: {project_id})")
            else:
                debug_logger.log_error(f"[BrowserCaptcha] 标签页重建失败 (project: {project_id})")

    async def _get_token_legacy(self, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """传统模式获取 reCAPTCHA token（每次创建新标签页）

        Args:
            project_id: Flow项目ID
            action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        # 确保浏览器已启动
        if not self._initialized or not self.browser:
            await self.initialize()

        start_time = time.time()
        tab = None

        try:
            website_url = "https://labs.google/fx/api/auth/providers"
            debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 访问页面: {website_url}")

            # 获取或创建标签页
            tabs = self.browser.tabs
            if tabs and len(tabs) > 0:
                tab = tabs[0]
                debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 复用现有标签页")
                await tab.get(website_url)
            else:
                debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 创建新标签页")
                tab = await self.browser.get(website_url)

            # 等待页面完全加载（增加等待时间）
            debug_logger.log_info("[BrowserCaptcha] [Legacy] 等待页面加载...")
            await tab.sleep(3)

            # 等待页面 DOM 完成
            for _ in range(10):
                ready_state = await tab.evaluate("document.readyState")
                if ready_state == "complete":
                    break
                await tab.sleep(0.5)

            # 等待 reCAPTCHA 加载
            recaptcha_ready = await self._wait_for_recaptcha(tab)

            if not recaptcha_ready:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] reCAPTCHA 无法加载")
                return None

            # 执行 reCAPTCHA
            debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 执行 reCAPTCHA 验证 (action: {action})...")
            token = await self._execute_recaptcha_on_tab(tab, action)

            duration_ms = (time.time() - start_time) * 1000

            if token:
                self._last_fingerprint = await self._extract_tab_fingerprint(tab)
                debug_logger.log_info(f"[BrowserCaptcha] [Legacy] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
                return token
            else:
                debug_logger.log_error("[BrowserCaptcha] [Legacy] Token获取失败（返回null）")
                return None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] [Legacy] 获取token异常: {str(e)}")
            return None
        finally:
            # 关闭标签页（但保留浏览器）
            if tab:
                try:
                    await tab.close()
                except Exception:
                    pass

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        """返回最近一次打码时的浏览器指纹快照。"""
        if not self._last_fingerprint:
            return None
        return dict(self._last_fingerprint)

    async def _clear_browser_cache(self):
        """清理浏览器全部缓存"""
        if not self.browser:
            return

        try:
            debug_logger.log_info("[BrowserCaptcha] 开始清理浏览器缓存...")

            # 使用 Chrome DevTools Protocol 清理缓存
            # 清理所有类型的缓存数据
            await self.browser.connection.send(
                "Network.clearBrowserCache"
            )

            # 清理 Cookies
            await self.browser.connection.send(
                "Network.clearBrowserCookies"
            )

            # 清理存储数据（localStorage, sessionStorage, IndexedDB 等）
            await self.browser.connection.send(
                "Storage.clearDataForOrigin",
                {
                    "origin": "https://www.google.com",
                    "storageTypes": "all"
                }
            )

            debug_logger.log_info("[BrowserCaptcha] ✅ 浏览器缓存已清理")

        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] 清理缓存时异常: {e}")

    async def close(self):
        """关闭浏览器"""
        # 停止空闲回收任务
        if self._idle_reaper_task and not self._idle_reaper_task.done():
            self._idle_reaper_task.cancel()
            try:
                await self._idle_reaper_task
            except asyncio.CancelledError:
                pass

        # 先停止所有常驻模式（关闭所有常驻标签页）
        await self.stop_resident_mode()

        try:
            custom_items = list(self._custom_tabs.values())
            self._custom_tabs.clear()
            for item in custom_items:
                tab = item.get("tab") if isinstance(item, dict) else None
                if tab:
                    try:
                        await tab.close()
                    except Exception:
                        pass

            if self.browser:
                try:
                    await self.browser.stop()
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 关闭浏览器时出现异常: {str(e)}")
                finally:
                    self.browser = None

            self._initialized = False
            self._resident_tabs.clear()  # 确保清空常驻字典
            self._resident_error_streaks.clear()
            debug_logger.log_info("[BrowserCaptcha] 浏览器已关闭")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 关闭浏览器异常: {str(e)}")

    async def open_login_window(self):
        """打开登录窗口供用户手动登录 Google"""
        await self.initialize()
        tab = await self.browser.get("https://accounts.google.com/")
        debug_logger.log_info("[BrowserCaptcha] 请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")
        print("请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")

    # ========== Session Token 刷新 ==========

    async def refresh_session_token(self, project_id: str) -> Optional[str]:
        """从常驻标签页获取最新的 Session Token
        
        复用 reCAPTCHA 常驻标签页，通过刷新页面并从 cookies 中提取
        __Secure-next-auth.session-token
        
        Args:
            project_id: 项目ID，用于定位常驻标签页
            
        Returns:
            新的 Session Token，如果获取失败返回 None
        """
        # 确保浏览器已初始化
        await self.initialize()
        
        start_time = time.time()
        debug_logger.log_info(f"[BrowserCaptcha] 开始刷新 Session Token (project: {project_id})...")
        
        # 尝试获取或创建常驻标签页
        async with self._resident_lock:
            resident_info = self._resident_tabs.get(project_id)
            
            # 如果该 project_id 没有常驻标签页，则创建
            if resident_info is None:
                debug_logger.log_info(f"[BrowserCaptcha] project_id={project_id} 没有常驻标签页，正在创建...")
                resident_info = await self._create_resident_tab(project_id)
                if resident_info is None:
                    debug_logger.log_warning(f"[BrowserCaptcha] 无法为 project_id={project_id} 创建常驻标签页")
                    return None
                self._resident_tabs[project_id] = resident_info
        
        if not resident_info or not resident_info.tab:
            debug_logger.log_error(f"[BrowserCaptcha] 无法获取常驻标签页")
            return None
        
        tab = resident_info.tab
        
        try:
            # 刷新页面以获取最新的 cookies
            debug_logger.log_info(f"[BrowserCaptcha] 刷新常驻标签页以获取最新 cookies...")
            await tab.reload()
            
            # 等待页面加载完成
            for i in range(30):
                await asyncio.sleep(1)
                try:
                    ready_state = await tab.evaluate("document.readyState")
                    if ready_state == "complete":
                        break
                except Exception:
                    pass
            
            # 额外等待确保 cookies 已设置
            await asyncio.sleep(2)
            
            # 从 cookies 中提取 __Secure-next-auth.session-token
            # nodriver 可以通过 browser 获取 cookies
            session_token = None
            
            try:
                # 使用 nodriver 的 cookies API 获取所有 cookies
                cookies = await self.browser.cookies.get_all()
                
                for cookie in cookies:
                    if cookie.name == "__Secure-next-auth.session-token":
                        session_token = cookie.value
                        break
                        
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 通过 cookies API 获取失败: {e}，尝试从 document.cookie 获取...")
                
                # 备选方案：通过 JavaScript 获取 (注意：HttpOnly cookies 可能无法通过此方式获取)
                try:
                    all_cookies = await tab.evaluate("document.cookie")
                    if all_cookies:
                        for part in all_cookies.split(";"):
                            part = part.strip()
                            if part.startswith("__Secure-next-auth.session-token="):
                                session_token = part.split("=", 1)[1]
                                break
                except Exception as e2:
                    debug_logger.log_error(f"[BrowserCaptcha] document.cookie 获取失败: {e2}")
            
            duration_ms = (time.time() - start_time) * 1000
            
            if session_token:
                debug_logger.log_info(f"[BrowserCaptcha] ✅ Session Token 获取成功（耗时 {duration_ms:.0f}ms）")
                return session_token
            else:
                debug_logger.log_error(f"[BrowserCaptcha] ❌ 未找到 __Secure-next-auth.session-token cookie")
                return None
                
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 刷新 Session Token 异常: {str(e)}")
            
            # 常驻标签页可能已失效，尝试重建
            async with self._resident_lock:
                await self._close_resident_tab(project_id)
                resident_info = await self._create_resident_tab(project_id)
                if resident_info:
                    self._resident_tabs[project_id] = resident_info
                    # 重建后再次尝试获取
                    try:
                        cookies = await self.browser.cookies.get_all()
                        for cookie in cookies:
                            if cookie.name == "__Secure-next-auth.session-token":
                                debug_logger.log_info(f"[BrowserCaptcha] ✅ 重建后 Session Token 获取成功")
                                return cookie.value
                    except Exception:
                        pass
            
            return None

    # ========== 状态查询 ==========

    def is_resident_mode_active(self) -> bool:
        """检查是否有任何常驻标签页激活"""
        return len(self._resident_tabs) > 0 or self._running

    def get_resident_count(self) -> int:
        """获取当前常驻标签页数量"""
        return len(self._resident_tabs)

    def get_resident_project_ids(self) -> list[str]:
        """获取所有当前常驻的 project_id 列表"""
        return list(self._resident_tabs.keys())

    def get_resident_project_id(self) -> Optional[str]:
        """获取当前常驻的 project_id（向后兼容，返回第一个）"""
        if self._resident_tabs:
            return next(iter(self._resident_tabs.keys()))
        return self.resident_project_id

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """为任意站点执行 reCAPTCHA，用于分数测试等场景。

        与普通 legacy 模式不同，这里会复用同一个常驻标签页，避免每次冷启动新 tab。
        """
        await self.initialize()
        self._last_fingerprint = None

        cache_key = f"{website_url}|{website_key}|{1 if enterprise else 0}"
        warmup_seconds = float(getattr(config, "browser_score_test_warmup_seconds", 12) or 12)
        per_request_settle_seconds = float(
            getattr(config, "browser_score_test_settle_seconds", 2.5) or 2.5
        )
        max_retries = 2

        async with self._custom_lock:
            for attempt in range(max_retries):
                start_time = time.time()
                custom_info = self._custom_tabs.get(cache_key)
                tab = custom_info.get("tab") if isinstance(custom_info, dict) else None

                try:
                    if tab is None:
                        debug_logger.log_info(f"[BrowserCaptcha] [Custom] 创建常驻测试标签页: {website_url}")
                        tab = await self.browser.get(website_url, new_tab=True)
                        custom_info = {
                            "tab": tab,
                            "recaptcha_ready": False,
                            "warmed_up": False,
                            "created_at": time.time(),
                        }
                        self._custom_tabs[cache_key] = custom_info

                    page_loaded = False
                    for _ in range(20):
                        ready_state = await tab.evaluate("document.readyState")
                        if ready_state == "complete":
                            page_loaded = True
                            break
                        await tab.sleep(0.5)

                    if not page_loaded:
                        raise RuntimeError("自定义页面加载超时")

                    if not custom_info.get("recaptcha_ready"):
                        recaptcha_ready = await self._wait_for_custom_recaptcha(
                            tab=tab,
                            website_key=website_key,
                            enterprise=enterprise,
                        )
                        if not recaptcha_ready:
                            raise RuntimeError("自定义 reCAPTCHA 无法加载")
                        custom_info["recaptcha_ready"] = True

                    try:
                        await tab.evaluate("""
                            (() => {
                                try {
                                    const body = document.body || document.documentElement;
                                    const width = window.innerWidth || 1280;
                                    const height = window.innerHeight || 720;
                                    const x = Math.max(24, Math.floor(width * 0.38));
                                    const y = Math.max(24, Math.floor(height * 0.32));
                                    const moveEvent = new MouseEvent('mousemove', {
                                        bubbles: true,
                                        clientX: x,
                                        clientY: y
                                    });
                                    const overEvent = new MouseEvent('mouseover', {
                                        bubbles: true,
                                        clientX: x,
                                        clientY: y
                                    });
                                    window.focus();
                                    window.dispatchEvent(new Event('focus'));
                                    document.dispatchEvent(moveEvent);
                                    document.dispatchEvent(overEvent);
                                    if (body) {
                                        body.dispatchEvent(moveEvent);
                                        body.dispatchEvent(overEvent);
                                    }
                                    window.scrollTo(0, Math.min(320, document.body?.scrollHeight || 320));
                                } catch (e) {}
                            })()
                        """)
                    except Exception:
                        pass

                    if not custom_info.get("warmed_up"):
                        if warmup_seconds > 0:
                            debug_logger.log_info(
                                f"[BrowserCaptcha] [Custom] 首次预热测试页面 {warmup_seconds:.1f}s 后再执行 token"
                            )
                            try:
                                await tab.evaluate("""
                                    (() => {
                                        try {
                                            window.scrollTo(0, Math.min(240, document.body.scrollHeight || 240));
                                            window.dispatchEvent(new Event('mousemove'));
                                            window.dispatchEvent(new Event('focus'));
                                        } catch (e) {}
                                    })()
                                """)
                            except Exception:
                                pass
                            await tab.sleep(warmup_seconds)
                        custom_info["warmed_up"] = True
                    elif per_request_settle_seconds > 0:
                        debug_logger.log_info(
                            f"[BrowserCaptcha] [Custom] 复用测试标签页，执行前额外等待 {per_request_settle_seconds:.1f}s"
                        )
                        await tab.sleep(per_request_settle_seconds)

                    debug_logger.log_info(f"[BrowserCaptcha] [Custom] 使用常驻测试标签页执行验证 (action: {action})...")
                    token = await self._execute_custom_recaptcha_on_tab(
                        tab=tab,
                        website_key=website_key,
                        action=action,
                        enterprise=enterprise,
                    )

                    duration_ms = (time.time() - start_time) * 1000
                    if token:
                        extracted_fingerprint = await self._extract_tab_fingerprint(tab)
                        if not extracted_fingerprint:
                            try:
                                fallback_ua = await tab.evaluate("navigator.userAgent || ''")
                                fallback_lang = await tab.evaluate("navigator.language || ''")
                                extracted_fingerprint = {
                                    "user_agent": fallback_ua or "",
                                    "accept_language": fallback_lang or "",
                                    "proxy_url": None,
                                }
                            except Exception:
                                extracted_fingerprint = None
                        self._last_fingerprint = extracted_fingerprint
                        debug_logger.log_info(
                            f"[BrowserCaptcha] [Custom] ✅ 常驻测试标签页 Token获取成功（耗时 {duration_ms:.0f}ms）"
                        )
                        return token

                    raise RuntimeError("自定义 token 获取失败（返回 null）")
                except Exception as e:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] [Custom] 尝试 {attempt + 1}/{max_retries} 失败: {str(e)}"
                    )
                    stale_info = self._custom_tabs.pop(cache_key, None)
                    stale_tab = stale_info.get("tab") if isinstance(stale_info, dict) else None
                    if stale_tab:
                        try:
                            await stale_tab.close()
                        except Exception:
                            pass
                    if attempt >= max_retries - 1:
                        debug_logger.log_error(f"[BrowserCaptcha] [Custom] 获取token异常: {str(e)}")
                        return None

            return None

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        """在同一个常驻标签页里获取 token 并直接校验页面分数。"""
        token_started_at = time.time()
        token = await self.get_custom_token(
            website_url=website_url,
            website_key=website_key,
            action=action,
            enterprise=enterprise,
        )
        token_elapsed_ms = int((time.time() - token_started_at) * 1000)

        if not token:
            return {
                "token": None,
                "token_elapsed_ms": token_elapsed_ms,
                "verify_mode": "browser_page",
                "verify_elapsed_ms": 0,
                "verify_http_status": None,
                "verify_result": {},
            }

        cache_key = f"{website_url}|{website_key}|{1 if enterprise else 0}"
        async with self._custom_lock:
            custom_info = self._custom_tabs.get(cache_key)
            tab = custom_info.get("tab") if isinstance(custom_info, dict) else None
            if tab is None:
                raise RuntimeError("页面分数测试标签页不存在")
            verify_payload = await self._verify_score_on_tab(tab, token, verify_url)

        return {
            "token": token,
            "token_elapsed_ms": token_elapsed_ms,
            **verify_payload,
        }
