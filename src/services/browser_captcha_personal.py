"""
浏览器自动化获取 reCAPTCHA token
使用 nodriver (undetected-chromedriver 继任者) 实现反检测浏览器
"""
import asyncio
import time
import os
from typing import Optional

import nodriver as uc

from ..core.logger import debug_logger


class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（nodriver 有头模式）"""

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
                browser_args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-setuid-sandbox',
                    '--disable-gpu',
                    '--window-size=1280,720',
                ]
            )

            self._initialized = True
            debug_logger.log_info(f"[BrowserCaptcha] ✅ nodriver 浏览器已启动 (Profile: {self.user_data_dir})")

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 浏览器启动失败: {str(e)}")
            raise

    async def get_token(self, project_id: str) -> Optional[str]:
        """获取 reCAPTCHA token

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
            debug_logger.log_info(f"[BrowserCaptcha] 访问页面: {website_url}")

            # 新建标签页并访问页面
            tab = await self.browser.get(website_url)

            # 等待页面完全加载（增加等待时间）
            debug_logger.log_info("[BrowserCaptcha] 等待页面加载...")
            await tab.sleep(3)
            
            # 等待页面 DOM 完成
            for _ in range(10):
                ready_state = await tab.evaluate("document.readyState")
                if ready_state == "complete":
                    break
                await tab.sleep(0.5)

            # 检测 reCAPTCHA 是否已加载
            debug_logger.log_info("[BrowserCaptcha] 检测 reCAPTCHA...")
            
            # 页面使用的是 reCAPTCHA Enterprise，检查 grecaptcha.enterprise.execute
            is_enterprise = await tab.evaluate(
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && typeof grecaptcha.enterprise.execute === 'function'"
            )
            
            debug_logger.log_info(f"[BrowserCaptcha] 检测结果: is_enterprise={is_enterprise}")
            
            recaptcha_type = "enterprise" if is_enterprise else None

            # 如果没有检测到 reCAPTCHA，尝试注入脚本
            if not recaptcha_type:
                debug_logger.log_info("[BrowserCaptcha] 未检测到 reCAPTCHA，注入脚本...")
                
                # 注入标准版 reCAPTCHA 脚本
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
                        recaptcha_type = "enterprise"
                        debug_logger.log_info(f"[BrowserCaptcha] reCAPTCHA Enterprise 已加载（等待了 {i * 0.5} 秒）")
                        break
                    await tab.sleep(0.5)
                else:
                    debug_logger.log_warning("[BrowserCaptcha] reCAPTCHA 加载超时")

            if not recaptcha_type:
                debug_logger.log_error("[BrowserCaptcha] reCAPTCHA 无法加载")
                return None

            # 执行 reCAPTCHA 并获取 token（使用 window 变量传递异步结果）
            debug_logger.log_info(f"[BrowserCaptcha] 执行 reCAPTCHA 验证 (类型: {recaptcha_type})...")
            
            # 生成唯一变量名避免冲突
            ts = int(time.time() * 1000)
            token_var = f"_recaptcha_token_{ts}"
            error_var = f"_recaptcha_error_{ts}"
            
            # 根据类型选择正确的 API
            if recaptcha_type == "enterprise":
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
            else:
                execute_script = f"""
                    (() => {{
                        window.{token_var} = null;
                        window.{error_var} = null;
                        
                        try {{
                            if (grecaptcha.ready) {{
                                grecaptcha.ready(function() {{
                                    grecaptcha.execute('{self.website_key}', {{action: 'FLOW_GENERATION'}})
                                        .then(function(token) {{
                                            window.{token_var} = token;
                                        }})
                                        .catch(function(err) {{
                                            window.{error_var} = err.message || 'execute failed';
                                        }});
                                }});
                            }} else {{
                                grecaptcha.execute('{self.website_key}', {{action: 'FLOW_GENERATION'}})
                                    .then(function(token) {{
                                        window.{token_var} = token;
                                    }})
                                    .catch(function(err) {{
                                        window.{error_var} = err.message || 'execute failed';
                                    }});
                            }}
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
                    debug_logger.log_info(f"[BrowserCaptcha] Token 已获取（等待了 {i * 0.5} 秒）")
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

            duration_ms = (time.time() - start_time) * 1000

            if token:
                debug_logger.log_info(f"[BrowserCaptcha] ✅ Token获取成功（耗时 {duration_ms:.0f}ms）")
                return token
            else:
                debug_logger.log_error("[BrowserCaptcha] Token获取失败（返回null）")
                return None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 获取token异常: {str(e)}")
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