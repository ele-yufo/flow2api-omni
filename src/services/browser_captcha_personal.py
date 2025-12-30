"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
支持常驻模式：基于单一 project_id 保持常驻标签页，即时生成 token
"""
import asyncio
import time
import os
from typing import Optional

import nodriver as uc

from ..core.logger import debug_logger


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（nodriver 有头模式）
    
    支持两种模式：
    1. 常驻模式 (Resident Mode): 保持一个常驻标签页，即时生成 token
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
        # 持久化 profile 目录
        self.user_data_dir = os.path.join(os.getcwd(), "browser_data")
        
        # 常驻模式相关属性
        self.resident_project_id: Optional[str] = None  # 常驻 project_id
        self.resident_tab = None                         # 常驻标签页
        self._running = False                            # 常驻模式运行状态
        self._recaptcha_ready = False                    # reCAPTCHA 是否已加载

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
        return cls._instance

    async def initialize(self):
        """初始化 nodriver 浏览器"""
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
            debug_logger.log_info(f"[BrowserCaptcha] 正在启动 nodriver 浏览器 (用户数据目录: {self.user_data_dir})...")

            # 确保 user_data_dir 存在
            os.makedirs(self.user_data_dir, exist_ok=True)

            # 启动 nodriver 浏览器
            self.browser = await uc.start(
                headless=self.headless,
                user_data_dir=self.user_data_dir,
                sandbox=False,  # nodriver 需要此参数来禁用 sandbox
                browser_args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--window-size=1280,720',
                    '--profile-directory=Default',  # 跳过 Profile 选择器页面
                ]
            )

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
        website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
        
        debug_logger.log_info(f"[BrowserCaptcha] 启动常驻模式，访问页面: {website_url}")
        
        # 创建一个独立的新标签页（不使用 main_tab，避免被回收）
        self.resident_tab = await self.browser.get(website_url, new_tab=True)
        
        debug_logger.log_info("[BrowserCaptcha] 标签页已创建，等待页面加载...")
        
        # 等待页面加载完成（带重试机制）
        page_loaded = False
        for retry in range(15):
            try:
                await asyncio.sleep(1)
                ready_state = await self.resident_tab.evaluate("document.readyState")
                debug_logger.log_info(f"[BrowserCaptcha] 页面状态: {ready_state} (重试 {retry + 1}/15)")
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

    async def stop_resident_mode(self):
        """停止常驻模式"""
        if not self._running:
            return
        
        self._running = False
        
        # 关闭常驻标签页
        if self.resident_tab:
            try:
                await self.resident_tab.close()
            except Exception:
                pass
            self.resident_tab = None
        
        self.resident_project_id = None
        self._recaptcha_ready = False
        
        debug_logger.log_info("[BrowserCaptcha] 常驻模式已停止")

    async def _wait_for_recaptcha(self, tab) -> bool:
        """等待 reCAPTCHA 加载
        
        Returns:
            True if reCAPTCHA loaded successfully
        """
        debug_logger.log_info("[BrowserCaptcha] 检测 reCAPTCHA...")
        
        # 检查 grecaptcha.enterprise.execute
        is_enterprise = await tab.evaluate(
            "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
        )
        
        if is_enterprise:
            debug_logger.log_info("[BrowserCaptcha] reCAPTCHA Enterprise 已加载")
            return True
        
        # 尝试注入脚本
        debug_logger.log_info("[BrowserCaptcha] 未检测到 reCAPTCHA，注入脚本...")
        
        await tab.evaluate(f"""
            (() => {{
                if (document.querySelector('script[src*="recaptcha"]')) return;
                const script = document.createElement('script');
                script.src = 'https://www.google.com/recaptcha/api.js?render={self.website_key}';
                script.async = true;
                document.head.appendChild(script);
            }})()
        """)
        
        # 等待脚本加载
        await tab.sleep(3)
        
        # 轮询等待 reCAPTCHA 加载
        for i in range(20):
            is_enterprise = await tab.evaluate(
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
            )
            
            if is_enterprise:
                debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA Enterprise 已加载（等待了 {i * 0.5} 秒）")
                return True
            await tab.sleep(0.5)
        
        debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 加载超时")
        return False

    async def _execute_recaptcha_on_tab(self, tab) -> Optional[str]:
        """在指定标签页执行 reCAPTCHA 获取 token
        
        Args:
            tab: nodriver 标签页对象
            
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
                        grecaptcha.enterprise.execute('{self.website_key}', {{action: 'FLOW_GENERATION'}})
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
        
        # 轮询等待结果（最多 15 秒）
        token = None
        for i in range(30):
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
        
        return token

    # ========== 主要 API ==========

    async def get_token(self, project_id: str) -> Optional[str]:
        """获取 reCAPTCHA token
        
        常驻模式：直接从常驻标签页即时生成 token
        传统模式：每次创建新标签页 (fallback)

        Args:
            project_id: Flow项目ID

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        # 如果是常驻模式且 project_id 匹配，直接从常驻标签页生成
        if self._running and self.resident_project_id == project_id:
            if self._recaptcha_ready and self.resident_tab:
                start_time = time.time()
                debug_logger.log_info("[BrowserCaptcha] 从常驻标签页即时生成 token...")
                token = await self._execute_recaptcha_on_tab(self.resident_tab)
                duration_ms = (time.time() - start_time) * 1000
                if token:
                    debug_logger.log_info(f"[BrowserCaptcha] ✅ Token生成成功（耗时 {duration_ms:.0f}ms）")
                    return token
                else:
                    debug_logger.log_warning("[BrowserCaptcha] 常驻模式生成失败，fallback到传统模式")
            else:
                debug_logger.log_warning("[BrowserCaptcha] 常驻标签页未就绪，fallback到传统模式")
        
        # Fallback: 使用传统模式
        return await self._get_token_legacy(project_id)

    async def _get_token_legacy(self, project_id: str) -> Optional[str]:
        """传统模式获取 reCAPTCHA token（每次创建新标签页）

        Args:
            project_id: Flow项目ID

        Returns:
            reCAPTCHA token字符串，如果获取失败返回None
        """
        # 确保浏览器已启动
        if not self._initialized or not self.browser:
            await self.initialize()

        start_time = time.time()
        tab = None

        try:
            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] [Legacy] 访问页面: {website_url}")

            # 新建标签页并访问页面
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
            debug_logger.log_info("[BrowserCaptcha] [Legacy] 执行 reCAPTCHA 验证...")
            token = await self._execute_recaptcha_on_tab(tab)

            duration_ms = (time.time() - start_time) * 1000

            if token:
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

    async def close(self):
        """关闭浏览器"""
        # 先停止常驻模式
        await self.stop_resident_mode()
        
        try:
            if self.browser:
                try:
                    self.browser.stop()
                except Exception as e:
                    debug_logger.log_warning(f"[BrowserCaptcha] 关闭浏览器时出现异常: {str(e)}")
                finally:
                    self.browser = None

            self._initialized = False
            debug_logger.log_info("[BrowserCaptcha] 浏览器已关闭")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 关闭浏览器异常: {str(e)}")

    async def open_login_window(self):
        """打开登录窗口供用户手动登录 Google"""
        await self.initialize()
        tab = await self.browser.get("https://accounts.google.com/")
        debug_logger.log_info("[BrowserCaptcha] 请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")
        print("请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")

    # ========== 状态查询 ==========

    def is_resident_mode_active(self) -> bool:
        """检查常驻模式是否激活"""
        return self._running

    def get_queue_size(self) -> int:
        """获取当前缓存队列大小"""
        return self.token_queue.qsize()

    def get_resident_project_id(self) -> Optional[str]:
        """获取当前常驻的 project_id"""
        return self.resident_project_id