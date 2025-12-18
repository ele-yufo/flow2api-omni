import asyncio
import time
import re
import os
from typing import Optional, Dict
from playwright.async_api import async_playwright, BrowserContext, Page

from ..core.logger import debug_logger

# ... (保持原来的 parse_proxy_url 和 validate_browser_proxy_url 函数不变) ...
def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    """解析代理URL，分离协议、主机、端口、认证信息"""
    proxy_pattern = r'^(socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$'
    match = re.match(proxy_pattern, proxy_url)
    if match:
        protocol, username, password, host, port = match.groups()
        proxy_config = {'server': f'{protocol}://{host}:{port}'}
        if username and password:
            proxy_config['username'] = username
            proxy_config['password'] = password
        return proxy_config
    return None

class BrowserCaptchaService:
    """浏览器自动化获取 reCAPTCHA token（持久化有头模式）"""

    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        """初始化服务"""
        # === 修改点 1: 设置为有头模式 ===
        self.headless = False 
        self.playwright = None
        # 注意: 持久化模式下，我们操作的是 context 而不是 browser
        self.context: Optional[BrowserContext] = None 
        self._initialized = False
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.db = db
        
        # === 修改点 2: 指定本地数据存储目录 ===
        # 这会在脚本运行目录下生成 browser_data 文件夹，用于保存你的登录状态
        self.user_data_dir = os.path.join(os.getcwd(), "browser_data")

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    # 首次调用不强制初始化，等待 get_token 时懒加载，或者可以在这里await
        return cls._instance

    async def initialize(self):
        """初始化持久化浏览器上下文"""
        if self._initialized and self.context:
            return

        try:
            proxy_url = None
            if self.db:
                captcha_config = await self.db.get_captcha_config()
                if captcha_config.browser_proxy_enabled and captcha_config.browser_proxy_url:
                    proxy_url = captcha_config.browser_proxy_url

            debug_logger.log_info(f"[BrowserCaptcha] 正在启动浏览器 (用户数据目录: {self.user_data_dir})...")
            self.playwright = await async_playwright().start()

            # 配置启动参数
            launch_options = {
                'headless': self.headless,
                'user_data_dir': self.user_data_dir, # 指定数据目录
                'viewport': {'width': 1280, 'height': 720}, # 设置默认窗口大小
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                ]
            }

            # 代理配置
            if proxy_url:
                proxy_config = parse_proxy_url(proxy_url)
                if proxy_config:
                    launch_options['proxy'] = proxy_config
                    debug_logger.log_info(f"[BrowserCaptcha] 使用代理: {proxy_config['server']}")

            # === 修改点 3: 使用 launch_persistent_context ===
            # 这会启动一个带有状态的浏览器窗口
            self.context = await self.playwright.chromium.launch_persistent_context(**launch_options)
            
            # 设置默认超时
            self.context.set_default_timeout(30000)

            self._initialized = True
            debug_logger.log_info(f"[BrowserCaptcha] ✅ 浏览器已启动 (Profile: {self.user_data_dir})")
            
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] ❌ 浏览器启动失败: {str(e)}")
            raise

    async def get_token(self, project_id: str) -> Optional[str]:
        """获取 reCAPTCHA token"""
        # 确保浏览器已启动
        if not self._initialized or not self.context:
            await self.initialize()

        start_time = time.time()
        page: Optional[Page] = None

        try:
            # === 修改点 4: 在现有上下文中新建标签页，而不是新建上下文 ===
            # 这样可以复用该上下文中已保存的 Cookie (你的登录状态)
            page = await self.context.new_page()

            website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
            debug_logger.log_info(f"[BrowserCaptcha] 访问页面: {website_url}")

            # 访问页面
            try:
                await page.goto(website_url, wait_until="domcontentloaded")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] 页面加载警告: {str(e)}")

            # --- 关键点：如果需要人工介入 ---
            # 你可以在这里加入一段逻辑，如果是第一次运行，或者检测到未登录，
            # 可以暂停脚本，等你手动操作完再继续。
            # 例如: await asyncio.sleep(30) 
            
            # ... (中间注入脚本和执行 reCAPTCHA 的代码逻辑与原版完全一致，此处省略以节省篇幅) ...
            # ... 请将原代码中从 "检查并注入 reCAPTCHA v3 脚本" 到 token 获取部分的代码复制到这里 ...
            
            # 这里为了演示，简写注入逻辑（请保留你原有的完整注入逻辑）:
            script_loaded = await page.evaluate("() => { return !!(window.grecaptcha && window.grecaptcha.execute); }")
            if not script_loaded:
                await page.evaluate(f"""
                    () => {{
                        const script = document.createElement('script');
                        script.src = 'https://www.google.com/recaptcha/api.js?render={self.website_key}';
                        script.async = true; script.defer = true;
                        document.head.appendChild(script);
                    }}
                """)
                # 等待加载... (保留你原有的等待循环)
                await page.wait_for_timeout(2000) 

            # 执行获取 Token (保留你原有的 execute 逻辑)
            token = await page.evaluate(f"""
                async () => {{
                    try {{
                        return await window.grecaptcha.execute('{self.website_key}', {{ action: 'FLOW_GENERATION' }});
                    }} catch (e) {{ return null; }}
                }}
            """)
            
            if token:
                debug_logger.log_info(f"[BrowserCaptcha] ✅ Token获取成功")
                return token
            else:
                debug_logger.log_error("[BrowserCaptcha] Token获取失败")
                return None

        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 异常: {str(e)}")
            return None
        finally:
            # === 修改点 5: 只关闭 Page (标签页)，不关闭 Context (浏览器窗口) ===
            if page:
                try:
                    await page.close()
                except:
                    pass

    async def close(self):
        """完全关闭浏览器（清理资源时调用）"""
        try:
            if self.context:
                await self.context.close() # 这会关闭整个浏览器窗口
                self.context = None
            
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
                
            self._initialized = False
            debug_logger.log_info("[BrowserCaptcha] 浏览器服务已关闭")
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] 关闭异常: {str(e)}")

    # 增加一个辅助方法，用于手动登录
    async def open_login_window(self):
        """调用此方法打开一个永久窗口供你登录Google"""
        await self.initialize()
        page = await self.context.new_page()
        await page.goto("https://accounts.google.com/")
        print("请在打开的浏览器中登录账号。登录完成后，无需关闭浏览器，脚本下次运行时会自动使用此状态。")