"""Configuration management for Flow2API"""
import os
import tomli
from pathlib import Path
from typing import Dict, Any, Optional

from .cors import CorsConfigMixin


def _default_config_path() -> Path:
    """Resolve config/setting.toml by walking up from this file — location-independent.

    Keeps working regardless of how deep under src/ this module lives.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / "setting.toml"
        if candidate.exists():
            return candidate
    # Fallback: repo root is 4 levels up from src/shared/config/provider.py
    return here.parents[3] / "config" / "setting.toml"


class Config(CorsConfigMixin):
    """Application configuration"""

    def __init__(self, config_path: Optional[str] = None):
        # config_path 可选:二期多租户可按需构造独立配置实例(每租户一个 toml)。
        self._config_path = Path(config_path) if config_path else _default_config_path()
        self._config = self._load_config()
        self._admin_username: Optional[str] = None
        self._admin_password: Optional[str] = None

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from the resolved setting.toml path."""
        with open(self._config_path, "rb") as f:
            return tomli.load(f)

    def reload_config(self):
        """Reload configuration from file"""
        self._config = self._load_config()

    def get_raw_config(self) -> Dict[str, Any]:
        """Get raw configuration dictionary"""
        return self._config

    @property
    def admin_username(self) -> str:
        # If admin_username is set from database, use it; otherwise fall back to config file
        if self._admin_username is not None:
            return self._admin_username
        return self._config["global"]["admin_username"]

    @admin_username.setter
    def admin_username(self, value: str):
        self._admin_username = value
        self._config["global"]["admin_username"] = value

    def set_admin_username_from_db(self, username: str):
        """Set admin username from database"""
        self._admin_username = username

    # Flow2API specific properties
    @property
    def flow_labs_base_url(self) -> str:
        """Google Labs base URL for project management"""
        return self._config["flow"]["labs_base_url"]

    @property
    def flow_api_base_url(self) -> str:
        """Google AI Sandbox API base URL for generation"""
        return self._config["flow"]["api_base_url"]

    @property
    def flow_timeout(self) -> int:
        timeout = self._config.get("flow", {}).get("timeout", 120)
        try:
            return max(5, int(timeout))
        except Exception:
            return 120

    @property
    def flow_max_retries(self) -> int:
        retries = self._config.get("flow", {}).get("max_retries", 3)
        try:
            return max(1, int(retries))
        except Exception:
            return 3

    @property
    def flow_image_request_timeout(self) -> int:
        """图片生成单次 HTTP 请求超时(秒)。"""
        default_timeout = min(self.flow_timeout, 40)
        timeout = self._config.get("flow", {}).get(
            "image_request_timeout",
            default_timeout
        )
        try:
            return max(5, int(timeout))
        except Exception:
            return self.flow_timeout

    @property
    def flow_image_timeout_retry_count(self) -> int:
        """图片生成遇到网络超时时的快速重试次数。"""
        retry_count = self._config.get("flow", {}).get("image_timeout_retry_count", 1)
        try:
            return max(0, min(3, int(retry_count)))
        except Exception:
            return 1

    @property
    def flow_image_timeout_retry_delay(self) -> float:
        """图片生成网络超时重试前等待秒数。"""
        delay = self._config.get("flow", {}).get("image_timeout_retry_delay", 0.8)
        try:
            return max(0.0, min(5.0, float(delay)))
        except Exception:
            return 0.8

    @property
    def flow_image_timeout_use_media_proxy_fallback(self) -> bool:
        """网络超时时是否切换媒体代理重试。"""
        return bool(
            self._config.get("flow", {}).get(
                "image_timeout_use_media_proxy_fallback",
                True
            )
        )

    @property
    def flow_image_prefer_media_proxy(self) -> bool:
        """图片生成是否优先走媒体代理链路。"""
        return bool(
            self._config.get("flow", {}).get(
                "image_prefer_media_proxy",
                False
            )
        )

    @property
    def flow_browser_submit_enabled(self) -> bool:
        """是否优先在打码浏览器内提交带 reCAPTCHA token 的 Flow 请求。

        默认关闭：实测中浏览器内 JS fetch 仍会被 Google 标记 UNUSUAL_ACTIVITY，
        且额外引入 CORS / tab 生命周期 / JS eval 失败等不稳定因素。
        现在统一走 HTTP 提交 + 真实浏览器指纹 + chrome110 impersonate 路径。
        """
        return bool(self._config.get("flow", {}).get("browser_submit_enabled", False))

    @property
    def flow_browser_submit_fallback_enabled(self) -> bool:
        """浏览器内提交失败时是否回退到服务端 HTTP 客户端。"""
        return bool(self._config.get("flow", {}).get("browser_submit_fallback_enabled", True))

    @property
    def flow_image_slot_wait_timeout(self) -> float:
        """图片硬并发槽位等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("image_slot_wait_timeout", 120)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 120.0

    @property
    def flow_image_launch_soft_limit(self) -> int:
        """图片生成前置发车软并发上限(0 表示关闭软整形，仅使用硬并发)。"""
        value = self._config.get("flow", {}).get("image_launch_soft_limit", 0)
        try:
            return max(0, min(200, int(value)))
        except Exception:
            return 0

    @property
    def flow_image_launch_wait_timeout(self) -> float:
        """图片前置发车软并发等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("image_launch_wait_timeout", 180)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 180.0

    @property
    def flow_image_launch_stagger_ms(self) -> int:
        """图片请求前置发车间隔(毫秒)，用于平滑同批突发。"""
        value = self._config.get("flow", {}).get("image_launch_stagger_ms", 0)
        try:
            return max(0, min(5000, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_slot_wait_timeout(self) -> float:
        """视频硬并发槽位等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("video_slot_wait_timeout", 120)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 120.0

    @property
    def flow_video_launch_soft_limit(self) -> int:
        """视频生成前置发车软并发上限(0 表示关闭软整形，仅使用硬并发)。"""
        value = self._config.get("flow", {}).get("video_launch_soft_limit", 0)
        try:
            return max(0, min(200, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_launch_wait_timeout(self) -> float:
        """视频前置发车软并发等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("video_launch_wait_timeout", 180)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 180.0

    @property
    def flow_video_launch_stagger_ms(self) -> int:
        """视频请求前置发车间隔(毫秒)，用于平滑同批突发。"""
        value = self._config.get("flow", {}).get("video_launch_stagger_ms", 0)
        try:
            return max(0, min(5000, int(value)))
        except Exception:
            return 0

    @property
    def poll_interval(self) -> float:
        return self._config["flow"]["poll_interval"]

    @property
    def max_poll_attempts(self) -> int:
        return self._config["flow"]["max_poll_attempts"]

    @property
    def server_host(self) -> str:
        return self._config["server"]["host"]

    @property
    def server_port(self) -> int:
        return self._config["server"]["port"]

    @property
    def debug_enabled(self) -> bool:
        return self._config.get("debug", {}).get("enabled", False)

    @property
    def debug_log_requests(self) -> bool:
        return self._config.get("debug", {}).get("log_requests", True)

    @property
    def debug_log_responses(self) -> bool:
        return self._config.get("debug", {}).get("log_responses", True)

    @property
    def debug_mask_token(self) -> bool:
        return self._config.get("debug", {}).get("mask_token", True)

    @property
    def debug_log_max_bytes(self) -> int:
        value = self._config.get("debug", {}).get("log_max_bytes", 50 * 1024 * 1024)
        try:
            return max(1024 * 1024, int(value))
        except Exception:
            return 50 * 1024 * 1024

    @property
    def debug_log_backup_count(self) -> int:
        value = self._config.get("debug", {}).get("log_backup_count", 5)
        try:
            return max(1, min(50, int(value)))
        except Exception:
            return 5

    # Mutable properties for runtime updates
    @property
    def api_key(self) -> str:
        return self._config["global"]["api_key"]

    @api_key.setter
    def api_key(self, value: str):
        self._config["global"]["api_key"] = value

    @property
    def admin_password(self) -> str:
        # If admin_password is set from database, use it; otherwise fall back to config file
        if self._admin_password is not None:
            return self._admin_password
        return self._config["global"]["admin_password"]

    @admin_password.setter
    def admin_password(self, value: str):
        self._admin_password = value
        self._config["global"]["admin_password"] = value

    def set_admin_password_from_db(self, password: str):
        """Set admin password from database"""
        self._admin_password = password

    def set_debug_enabled(self, enabled: bool):
        """Set debug mode enabled/disabled"""
        if "debug" not in self._config:
            self._config["debug"] = {}
        self._config["debug"]["enabled"] = enabled

    @property
    def image_timeout(self) -> int:
        """Get image generation timeout in seconds"""
        return self._config.get("generation", {}).get("image_timeout", 300)

    def set_image_timeout(self, timeout: int):
        """Set image generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["image_timeout"] = timeout

    @property
    def video_timeout(self) -> int:
        """Get video generation timeout in seconds"""
        return self._config.get("generation", {}).get("video_timeout", 1500)

    def set_video_timeout(self, timeout: int):
        """Set video generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["video_timeout"] = timeout

    @property
    def polling_mode_enabled(self) -> bool:
        """Get polling mode enabled status."""
        return self.call_logic_mode == "polling"

    @property
    def call_logic_mode(self) -> str:
        """Get call logic mode (default or polling)."""
        call_logic = self._config.get("call_logic", {})
        mode = call_logic.get("call_mode")
        if mode in ("default", "polling"):
            return mode
        if call_logic.get("polling_mode_enabled", False):
            return "polling"
        return "default"

    def set_polling_mode_enabled(self, enabled: bool):
        """Set polling mode enabled/disabled."""
        self.set_call_logic_mode("polling" if enabled else "default")

    def set_call_logic_mode(self, mode: str):
        """Set call logic mode (default or polling)."""
        normalized = "polling" if mode == "polling" else "default"
        if "call_logic" not in self._config:
            self._config["call_logic"] = {}
        self._config["call_logic"]["call_mode"] = normalized
        self._config["call_logic"]["polling_mode_enabled"] = normalized == "polling"

    @property
    def upsample_timeout(self) -> int:
        """Get upsample (4K/2K) timeout in seconds"""
        return self._config.get("generation", {}).get("upsample_timeout", 300)

    def set_upsample_timeout(self, timeout: int):
        """Set upsample (4K/2K) timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["upsample_timeout"] = timeout

    # Cache configuration
    @property
    def cache_enabled(self) -> bool:
        """Get cache enabled status"""
        return self._config.get("cache", {}).get("enabled", False)

    def set_cache_enabled(self, enabled: bool):
        """Set cache enabled status"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["enabled"] = enabled

    @property
    def cache_timeout(self) -> int:
        """Get cache timeout in seconds"""
        return self._config.get("cache", {}).get("timeout", 7200)

    def set_cache_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["timeout"] = timeout

    @property
    def cache_base_url(self) -> str:
        """Get cache base URL"""
        return self._config.get("cache", {}).get("base_url", "")

    def set_cache_base_url(self, base_url: str):
        """Set cache base URL"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["base_url"] = base_url

    # Watermark removal (Pro video de-watermark via resident ProPainter service)
    @property
    def watermark_enabled(self) -> bool:
        """Whether to de-watermark Pro (TIER_ONE) videos via the resident service."""
        return bool(self._config.get("watermark", {}).get("enabled", False))

    @property
    def watermark_service_url(self) -> str:
        """Base URL of the local de-watermark service (no trailing slash)."""
        return self._config.get("watermark", {}).get("service_url", "http://127.0.0.1:18290").rstrip("/")

    @property
    def watermark_timeout_seconds(self) -> int:
        """Max seconds to wait for the de-watermark service per video."""
        value = self._config.get("watermark", {}).get("timeout_seconds", 120)
        try:
            return max(10, min(600, int(value)))
        except Exception:
            return 120

    # Captcha configuration
    @property
    def captcha_method(self) -> str:
        """Get captcha method"""
        return self._config.get("captcha", {}).get("captcha_method", "yescaptcha")

    def set_captcha_method(self, method: str):
        """Set captcha method"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["captcha_method"] = method

    @property
    def browser_launch_background(self) -> bool:
        """有头浏览器打码是否默认后台启动，避免抢占前台窗口。"""
        return self._config.get("captcha", {}).get("browser_launch_background", True)

    def set_browser_launch_background(self, enabled: bool):
        """设置有头浏览器打码是否后台启动。"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["browser_launch_background"] = bool(enabled)

    @property
    def browser_recaptcha_settle_seconds(self) -> float:
        """有头打码在 reload/clr 就绪后的额外等待秒数。"""
        value = self._config.get("captcha", {}).get("browser_recaptcha_settle_seconds", 3.0)
        try:
            return max(0.0, min(10.0, float(value)))
        except Exception:
            return 3.0

    @property
    def captcha_persistent_profile_enabled(self) -> bool:
        """是否让 personal (nodriver) 打码使用持久化 Chrome profile。

        开启后，每次启动 nodriver 都复用同一个 user-data-dir，profile 内的
        Google 登录态 cookie 会让 reCAPTCHA Enterprise 把请求按"已登录账号"
        评分，显著降低 PUBLIC_ERROR_UNUSUAL_ACTIVITY。

        默认关闭：第一次启用前必须先用图形界面 chrome --user-data-dir=<path>
        登录目标账号，否则 nodriver 启动后是匿名状态，等同于现有行为。
        """
        return bool(self._config.get("captcha", {}).get("persistent_profile_enabled", False))

    @property
    def captcha_persistent_profile_path(self) -> str:
        """持久化 profile 的 user-data-dir 路径。

        必须是 flow2api 进程有读写权限的固定目录。该目录会被 nodriver 和
        GUI Chrome 共享（不可同时打开）。
        """
        return str(
            self._config.get("captcha", {}).get(
                "persistent_profile_path", "/opt/flow2api-profiles/ultra"
            )
        ).strip()

    @property
    def browser_idle_ttl_seconds(self) -> int:
        value = self._config.get("captcha", {}).get("browser_idle_ttl_seconds", 600)
        try:
            return max(60, int(value))
        except Exception:
            return 600

    @property
    def personal_max_resident_tabs(self) -> int:
        """内置浏览器打码的共享标签页上限"""
        value = self._config.get("captcha", {}).get("personal_max_resident_tabs", 5)
        try:
            return max(1, min(50, int(value)))  # 限制在1-50之间
        except Exception:
            return 5

    @property
    def personal_project_pool_size(self) -> int:
        """单个 Token 默认维护的项目池数量，仅影响项目轮换。"""
        value = self._config.get("captcha", {}).get("personal_project_pool_size", 4)
        try:
            return max(1, min(50, int(value)))
        except Exception:
            return 4

    @property
    def personal_idle_tab_ttl_seconds(self) -> int:
        """内置浏览器打码标签页空闲超时(秒)"""
        value = self._config.get("captcha", {}).get("personal_idle_tab_ttl_seconds", 600)
        try:
            return max(60, int(value))
        except Exception:
            return 600

    def set_personal_max_resident_tabs(self, value: int):
        """设置内置浏览器打码的共享标签页上限"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_max_resident_tabs"] = max(1, min(50, int(value)))

    def set_personal_project_pool_size(self, value: int):
        """设置单个 Token 默认维护的项目池数量，仅影响项目轮换"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_project_pool_size"] = max(1, min(50, int(value)))

    def set_personal_idle_tab_ttl_seconds(self, value: int):
        """设置内置浏览器打码标签页空闲超时(秒)"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_idle_tab_ttl_seconds"] = max(60, int(value))

    @property
    def yescaptcha_api_key(self) -> str:
        """Get YesCaptcha API key"""
        return self._config.get("captcha", {}).get("yescaptcha_api_key", "")

    def set_yescaptcha_api_key(self, api_key: str):
        """Set YesCaptcha API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_api_key"] = api_key

    @property
    def yescaptcha_base_url(self) -> str:
        """Get YesCaptcha base URL"""
        return self._config.get("captcha", {}).get("yescaptcha_base_url", "https://api.yescaptcha.com")

    def set_yescaptcha_base_url(self, base_url: str):
        """Set YesCaptcha base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_base_url"] = base_url

    @property
    def capmonster_api_key(self) -> str:
        """Get CapMonster API key"""
        return self._config.get("captcha", {}).get("capmonster_api_key", "")

    def set_capmonster_api_key(self, api_key: str):
        """Set CapMonster API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_api_key"] = api_key

    @property
    def capmonster_base_url(self) -> str:
        """Get CapMonster base URL"""
        return self._config.get("captcha", {}).get("capmonster_base_url", "https://api.capmonster.cloud")

    def set_capmonster_base_url(self, base_url: str):
        """Set CapMonster base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_base_url"] = base_url

    @property
    def ezcaptcha_api_key(self) -> str:
        """Get EzCaptcha API key"""
        return self._config.get("captcha", {}).get("ezcaptcha_api_key", "")

    def set_ezcaptcha_api_key(self, api_key: str):
        """Set EzCaptcha API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["ezcaptcha_api_key"] = api_key

    @property
    def ezcaptcha_base_url(self) -> str:
        """Get EzCaptcha base URL"""
        return self._config.get("captcha", {}).get("ezcaptcha_base_url", "https://api.ez-captcha.com")

    def set_ezcaptcha_base_url(self, base_url: str):
        """Set EzCaptcha base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["ezcaptcha_base_url"] = base_url

    @property
    def capsolver_api_key(self) -> str:
        """Get CapSolver API key"""
        return self._config.get("captcha", {}).get("capsolver_api_key", "")

    def set_capsolver_api_key(self, api_key: str):
        """Set CapSolver API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capsolver_api_key"] = api_key

    @property
    def capsolver_base_url(self) -> str:
        """Get CapSolver base URL"""
        return self._config.get("captcha", {}).get("capsolver_base_url", "https://api.capsolver.com")

    def set_capsolver_base_url(self, base_url: str):
        """Set CapSolver base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capsolver_base_url"] = base_url

    @property
    def remote_browser_base_url(self) -> str:
        """Get remote browser captcha service base URL"""
        return self._config.get("captcha", {}).get("remote_browser_base_url", "")

    def set_remote_browser_base_url(self, base_url: str):
        """Set remote browser captcha service base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["remote_browser_base_url"] = (base_url or "").strip()

    @property
    def remote_browser_api_key(self) -> str:
        """Get remote browser captcha service API key"""
        return self._config.get("captcha", {}).get("remote_browser_api_key", "")

    def set_remote_browser_api_key(self, api_key: str):
        """Set remote browser captcha service API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["remote_browser_api_key"] = (api_key or "").strip()

    @property
    def remote_browser_timeout(self) -> int:
        """Get remote browser captcha request timeout (seconds)"""
        timeout = self._config.get("captcha", {}).get("remote_browser_timeout", 60)
        try:
            return max(5, int(timeout))
        except Exception:
            return 60

    def set_remote_browser_timeout(self, timeout: int):
        """Set remote browser captcha request timeout (seconds)"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        try:
            normalized = max(5, int(timeout))
        except Exception:
            normalized = 60
        self._config["captcha"]["remote_browser_timeout"] = normalized

    # ========== ST 自我续命 / 保活 ==========
    @property
    def st_keepalive_enabled(self) -> bool:
        return bool(self._config.get("token", {}).get("st_keepalive_enabled", True))

    @property
    def st_keepalive_interval_hours(self) -> int:
        try:
            return int(self._config.get("token", {}).get("st_keepalive_interval_hours", 24))
        except (TypeError, ValueError):
            return 24

    @property
    def st_browser_refresh_enabled(self) -> bool:
        # 默认 False：多账号池下浏览器只登录一个号，用它刷新会把别的号 ST 写错
        return bool(self._config.get("token", {}).get("st_browser_refresh_enabled", False))

    # ========== 浏览器保活器（独立进程 flow2api-keepalive.service）==========
    def _keepalive_int(
        self,
        key: str,
        default: int,
        minimum: int,
        maximum: int = None,
    ) -> int:
        try:
            value = int(self._config.get("keepalive", {}).get(key, default))
        except (TypeError, ValueError):
            value = default
        value = max(minimum, value)
        return min(maximum, value) if maximum is not None else value

    # AT 直驱范式：常驻 nodriver 浏览器定期刷新 labs.google flow 页触发 OAuth 续期，
    # 从 /fx/api/auth/session 拿新 AT(+rotated ST) 写库，主服务直接用活 AT，
    # 绕过"旧 ST 会话 grant 已死、st_to_at 换不出活 AT"的死结。
    # 独立进程读 toml 即可，不入 DB（部署期常量，无运行时动态改需求）。
    @property
    def keepalive_browser_enabled(self) -> bool:
        return bool(self._config.get("keepalive", {}).get("browser_enabled", False))

    @property
    def keepalive_browser_interval_seconds(self) -> int:
        return self._keepalive_int("browser_interval_seconds", 1200, 60)

    @property
    def keepalive_browser_initial_delay_seconds(self) -> int:
        return self._keepalive_int("browser_initial_delay_seconds", 120, 0, 3600)

    @property
    def keepalive_browser_retired_interval_seconds(self) -> int:
        return self._keepalive_int("browser_retired_interval_seconds", 43200, 3600)

    @property
    def keepalive_browser_reconcile_interval_seconds(self) -> int:
        return self._keepalive_int("browser_reconcile_interval_seconds", 15, 5, 300)

    @property
    def keepalive_browser_max_concurrent_refreshes(self) -> int:
        return self._keepalive_int("browser_max_concurrent_refreshes", 1, 1, 10)

    @property
    def keepalive_browser_max_concurrent_launches(self) -> int:
        return self._keepalive_int("browser_max_concurrent_launches", 1, 1, 10)

    @property
    def keepalive_browser_retry_base_seconds(self) -> int:
        return self._keepalive_int("browser_retry_base_seconds", 60, 10, 3600)

    @property
    def keepalive_browser_retry_max_seconds(self) -> int:
        return self._keepalive_int("browser_retry_max_seconds", 1800, 30, 21600)

    @property
    def keepalive_browser_human_retry_seconds(self) -> int:
        return self._keepalive_int("browser_human_retry_seconds", 21600, 300, 86400)

    @property
    def keepalive_onboarding_display(self) -> str:
        value = str(self._config.get("keepalive", {}).get("onboarding_display", ":11")).strip()
        return value or ":11"

    @property
    def keepalive_onboarding_session_ttl_seconds(self) -> int:
        return self._keepalive_int("onboarding_session_ttl_seconds", 1800, 300, 7200)

    @property
    def keepalive_browser_token_ids(self) -> list:
        """逗号分隔的 token id 列表，每号一个独立常驻 profile。"""
        raw = self._config.get("keepalive", {}).get("browser_token_ids", "")
        return [int(x.strip()) for x in str(raw).split(",") if x.strip().isdigit()]

    @property
    def keepalive_browser_profile_base(self) -> str:
        return str(self._config.get("keepalive", {}).get("browser_profile_base", "/opt/flow2api-profiles")).strip()

    @property
    def keepalive_browser_proxy(self) -> str:
        return str(self._config.get("keepalive", {}).get("browser_proxy", "http://127.0.0.1:7890")).strip()

    @property
    def keepalive_browser_display(self) -> str:
        return str(self._config.get("keepalive", {}).get("browser_display", ":10")).strip()

    @property
    def keepalive_browser_settle_seconds(self) -> float:
        try:
            return max(0.0, float(self._config.get("keepalive", {}).get("browser_settle_seconds", 8.0)))
        except (TypeError, ValueError):
            return 8.0

    @property
    def min_credits_to_select(self) -> int:
        try:
            return int(self._config.get("call_logic", {}).get("min_credits_to_select", 1))
        except (TypeError, ValueError):
            return 1

    @property
    def alert_webhook_url(self) -> str:
        # 优先环境变量（密钥不进 git），回退 toml [admin] alert_webhook_url
        env = os.environ.get("FLOW2API_ALERT_WEBHOOK_URL")
        if env:
            return env.strip()
        return str(self._config.get("admin", {}).get("alert_webhook_url", "") or "")

    @property
    def alert_pool_low_threshold(self) -> int:
        try:
            return int(self._config.get("admin", {}).get("alert_pool_low_threshold", 2))
        except (TypeError, ValueError):
            return 2


# Settings 是 Config 的规范别名(shared 地基命名);Config 名保留以兼容既有 import。
Settings = Config

# Global config instance (single app-wide mutable provider; DB overrides 回灌至此)
config = Config()
