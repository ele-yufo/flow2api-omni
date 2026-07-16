"""Flow API Client for VideoFX (Veo)"""
import asyncio
import json
import contextvars
import time
import uuid
import random
import base64
import ssl
from typing import Dict, Any, Optional, List, Union, Callable, Awaitable
from urllib.parse import quote
import urllib.error
import urllib.request
from curl_cffi.requests import AsyncSession
from ..core.logger import debug_logger
from ..core.config import config
from .flow.http_headers import HeaderBuilder
from .captcha.api_solver import get_api_captcha_token
from .captcha.cooldown import CaptchaCooldownTracker
from .flow.transport import (
    build_remote_browser_http_timeout,
    stdlib_json_http_request,
    sync_json_http_request,
    sync_json_request_via_urllib,
)
from .flow.errors import get_retry_reason, is_captcha_rejection_reason, is_retryable_network_error, is_timeout_error, should_fallback_to_urllib
from .flow.response_parsers import extract_google_error_reason, extract_project_id_from_payload, extract_rotated_st_from_set_cookie, parse_json_response_text
from ..shared.storage.media_types import convert_to_jpeg, detect_image_mime_type
from .flow.request_builders import (
    build_image_request,
    build_image_upsample_request,
    build_video_concatenation_request,
    build_video_status_request,
    build_video_extend_request,
    build_video_upsample_request,
    build_video_image_request,
    build_video_reference_images_request,
    build_video_text_input,
    build_video_text_request,
)

try:
    import httpx
except ImportError:
    httpx = None


class FlowClient:
    """VideoFX API客户端"""

    def __init__(self, proxy_manager, db=None):
        self.proxy_manager = proxy_manager
        self.db = db  # Database instance for captcha config
        self.labs_base_url = config.flow_labs_base_url  # https://labs.google/fx/api
        self.api_base_url = config.flow_api_base_url    # https://aisandbox-pa.googleapis.com/v1
        self.timeout = config.flow_timeout
        # 请求头/UA 构造(含 per-account UA 缓存 + 静态浏览器头)已抽到 flow.http_headers
        self._header_builder = HeaderBuilder()
        # 当前请求链路绑定的浏览器指纹（基于 contextvar，避免并发串扰）
        self._request_fingerprint_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
            "flow_request_fingerprint",
            default=None
        )
        self._request_browser_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
            "flow_request_browser",
            default=None
        )
        self._remote_browser_prefill_last_sent: Dict[str, float] = {}
        self._captcha_cooldown = CaptchaCooldownTracker()

        # 发车策略改为“请求到就发”：
        # 不在 flow2api 本地对提交做批次整形或排队，避免把同批请求打成阶梯。

    def _generate_user_agent(self, account_id: str = None) -> str:
        """委托 HeaderBuilder(见 flow.http_headers)。"""
        return self._header_builder.generate_user_agent(account_id)

    def _build_request_headers(
        self,
        headers: Optional[Dict] = None,
        st_token: Optional[str] = None,
        at_token: Optional[str] = None,
        use_st: bool = False,
        use_at: bool = False,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """委托 HeaderBuilder(见 flow.http_headers)。"""
        return self._header_builder.build_request_headers(
            headers=headers, st_token=st_token, at_token=at_token,
            use_st=use_st, use_at=use_at, fingerprint=fingerprint,
        )

    @staticmethod
    def _get_user_agent_family(user_agent: str) -> str:
        return HeaderBuilder.get_user_agent_family(user_agent)

    def _select_impersonate_for_headers(self, headers: Dict[str, Any]) -> Optional[str]:
        """Choose a TLS impersonation that does not contradict the visible UA."""
        family = self._get_user_agent_family(str(headers.get("User-Agent", "")))
        if family in ("chrome", "edge"):
            return "chrome124"  # 与 UA 报的 Chrome 版本对齐(curl_cffi 0.7.3 最高支持 124)
        if family == "safari":
            return "safari15_3"
        return None

    def _captcha_cooldown_key(self, project_id: Optional[str]) -> str:
        """委托 CaptchaCooldownTracker。"""
        return self._captcha_cooldown.key(project_id)

    def _record_captcha_rejection(self, project_id: Optional[str]) -> float:
        """委托 CaptchaCooldownTracker。"""
        return self._captcha_cooldown.record_rejection(project_id)

    def _get_captcha_cooldown_delay(self, project_id: Optional[str]) -> float:
        """委托 CaptchaCooldownTracker。"""
        return self._captcha_cooldown.get_cooldown_delay(project_id)

    def _clear_captcha_rejection(self, project_id: Optional[str]):
        """委托 CaptchaCooldownTracker。"""
        self._captcha_cooldown.clear(project_id)

    async def _wait_for_captcha_cooldown(self, project_id: Optional[str], action: str):
        delay = self._get_captcha_cooldown_delay(project_id)
        if delay <= 0:
            return
        debug_logger.log_warning(
            f"[reCAPTCHA] 最近连续被上游拒绝，等待 {delay:.1f}s 后重新取 token: project_id={project_id}, action={action}"
        )
        await asyncio.sleep(delay)

    def _set_request_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        """设置当前请求链路的浏览器指纹上下文。"""
        self._request_fingerprint_ctx.set(dict(fingerprint) if fingerprint else None)

    def _set_request_browser_context(self, browser_context: Optional[Dict[str, Any]]):
        """绑定本次 reCAPTCHA token 对应的有头浏览器上下文。"""
        self._request_browser_ctx.set(dict(browser_context) if browser_context else None)

    def get_request_browser_context(self) -> Optional[Dict[str, Any]]:
        browser_context = self._request_browser_ctx.get()
        if not isinstance(browser_context, dict) or not browser_context:
            return None
        return dict(browser_context)

    def get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """获取当前请求链路绑定的浏览器指纹快照。"""
        fingerprint = self._request_fingerprint_ctx.get()
        if not isinstance(fingerprint, dict) or not fingerprint:
            return None
        return dict(fingerprint)

    def clear_request_fingerprint(self):
        """清理请求链路绑定的浏览器指纹。"""
        self._set_request_fingerprint(None)
        self._set_request_browser_context(None)

    def _payload_has_recaptcha_token(self, value: Any) -> bool:
        if isinstance(value, dict):
            recaptcha_context = value.get("recaptchaContext")
            if isinstance(recaptcha_context, dict) and recaptcha_context.get("token"):
                return True
            return any(self._payload_has_recaptcha_token(item) for item in value.values())
        if isinstance(value, list):
            return any(self._payload_has_recaptcha_token(item) for item in value)
        return False

    def _extract_project_id_from_payload(self, value: Any) -> Optional[str]:
        """委托 flow.response_parsers。"""
        return extract_project_id_from_payload(value)

    def _should_submit_via_captcha_browser(
        self,
        method: str,
        url: str,
        json_data: Optional[Dict[str, Any]],
    ) -> bool:
        if not config.flow_browser_submit_enabled:
            return False
        if method.upper() != "POST":
            return False
        if not isinstance(json_data, dict) or not self._payload_has_recaptcha_token(json_data):
            return False
        if not str(url or "").startswith(self.api_base_url):
            return False
        browser_context = self.get_request_browser_context()
        return bool(browser_context and browser_context.get("method") in {"personal", "browser"})

    async def _make_request_via_captcha_browser(
        self,
        method: str,
        url: str,
        headers: Dict[str, Any],
        json_data: Optional[Dict[str, Any]],
        timeout: int,
    ) -> Dict[str, Any]:
        browser_context = self.get_request_browser_context()
        if not browser_context:
            raise RuntimeError("missing captcha browser context")

        project_id = (
            browser_context.get("project_id")
            or self._extract_project_id_from_payload(json_data)
            or ""
        )
        submit_method = browser_context.get("method")
        debug_logger.log_info(
            f"[BROWSER SUBMIT] 使用有头浏览器提交 Flow 请求: method={submit_method}, "
            f"project_id={project_id}, url={url}"
        )

        if submit_method == "personal":
            from .browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(self.db)
            return await service.submit_flow_request(
                project_id=project_id,
                method=method,
                url=url,
                headers=headers,
                json_data=json_data,
                timeout_seconds=timeout,
            )

        # browser (playwright) 模式已废弃；main.py:54 强制把 captcha_method=="browser"
        # 改写为 personal，所以 submit_method=="browser" 不可达。删除避免维护成本。
        raise RuntimeError(f"unsupported captcha browser submit method: {submit_method}")

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        use_media_proxy: bool = False,
        respect_fingerprint_proxy: bool = True,
        force_urllib: bool = False,
        capture_set_cookie: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """统一HTTP请求处理

        Args:
            method: HTTP方法 (GET/POST)
            url: 完整URL
            headers: 请求头
            json_data: JSON请求体
            use_st: 是否使用ST认证 (Cookie方式)
            st_token: Session Token
            use_at: 是否使用AT认证 (Bearer方式)
            at_token: Access Token
            timeout: 自定义超时时间(秒)，不传则使用默认值
            use_media_proxy: 是否使用图片上传/下载代理
            respect_fingerprint_proxy: 是否优先使用打码浏览器指纹里的代理
        """
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if self.proxy_manager:
            if use_media_proxy and hasattr(self.proxy_manager, "get_media_proxy_url"):
                proxy_url = await self.proxy_manager.get_media_proxy_url()
            elif hasattr(self.proxy_manager, "get_request_proxy_url"):
                proxy_url = await self.proxy_manager.get_request_proxy_url()
            else:
                proxy_url = await self.proxy_manager.get_proxy_url()

        if respect_fingerprint_proxy and isinstance(fingerprint, dict) and "proxy_url" in fingerprint:
            proxy_url = fingerprint.get("proxy_url")
            if proxy_url == "":
                proxy_url = None
        request_timeout = timeout or self.timeout

        headers = self._build_request_headers(
            headers=headers,
            st_token=st_token,
            at_token=at_token,
            use_st=use_st,
            use_at=use_at,
            fingerprint=fingerprint,
        )
        impersonate = self._select_impersonate_for_headers(headers)

        # Log request
        if config.debug_enabled:
            proxy_for_log = proxy_url if proxy_url else "direct"
            debug_logger.log_info(
                "[FINGERPRINT] "
                f"present={bool(fingerprint)}, "
                f"ua_family={self._get_user_agent_family(str(headers.get('User-Agent', '')))}, "
                f"has_sec_ch_ua={bool(headers.get('sec-ch-ua'))}, "
                f"proxy={proxy_for_log}, "
                f"impersonate={impersonate or 'none'}"
            )
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=json_data,
                proxy=proxy_url
            )

        start_time = time.time()

        try:
            # 大 body 上传场景（如 uploadImage）跳过 curl_cffi，直接 urllib。
            # curl_cffi 的 Chrome impersonate 在 ~500KB+ base64 body 上有
            # HTTP/2 framing hang ~2min 的 bug；这条路径不需要 reCAPTCHA token，
            # 不依赖 chrome 指纹，走 urllib 干净利落。
            if force_urllib:
                return await asyncio.to_thread(
                    self._sync_json_request_via_urllib,
                    method.upper(),
                    url,
                    headers,
                    json_data,
                    proxy_url,
                    request_timeout,
                )

            if self._should_submit_via_captcha_browser(method, url, json_data):
                try:
                    return await self._make_request_via_captcha_browser(
                        method=method,
                        url=url,
                        headers=headers,
                        json_data=json_data,
                        timeout=request_timeout,
                    )
                except Exception as browser_submit_error:
                    debug_logger.log_warning(
                        f"[BROWSER SUBMIT] 有头浏览器提交失败: {browser_submit_error}"
                    )
                    if not config.flow_browser_submit_fallback_enabled:
                        raise
                    debug_logger.log_warning(
                        "[BROWSER SUBMIT] 已启用 fallback，回退到服务端 HTTP 客户端提交"
                    )

            async with AsyncSession() as session:
                request_kwargs = {
                    "headers": headers,
                    "proxy": proxy_url,
                    "timeout": request_timeout,
                }
                if impersonate:
                    request_kwargs["impersonate"] = impersonate

                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        **request_kwargs
                    )
                else:  # POST
                    response = await session.post(
                        url,
                        json=json_data,
                        **request_kwargs
                    )

                duration_ms = (time.time() - start_time) * 1000

                # Log response
                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms
                    )

                # 检查HTTP错误
                if response.status_code >= 400:
                    # 解析错误响应(纯解析已抽到 flow.response_parsers)
                    try:
                        error_reason = extract_google_error_reason(response.status_code, response.json())
                    except:
                        error_reason = f"HTTP Error {response.status_code}: {response.text[:200]}"
                    
                    # 失败时输出请求体和错误内容到控制台
                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(f"[API FAILED] Request Body: {debug_logger.format_data_for_log(json_data)}")
                    debug_logger.log_error(f"[API FAILED] Response: {response.text}")
                    
                    raise Exception(error_reason)

                if capture_set_cookie is not None:
                    try:
                        capture_set_cookie.extend(response.headers.get_list("set-cookie"))
                    except Exception:
                        single = response.headers.get("set-cookie")
                        if single:
                            capture_set_cookie.append(single)

                return response.json()

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            error_msg = str(e)

            # 如果不是我们自己抛出的异常，记录日志
            if "HTTP Error" not in error_msg and not any(x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(f"[API FAILED] Request Body: {debug_logger.format_data_for_log(json_data)}")
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")

            if self._should_fallback_to_urllib(error_msg):
                debug_logger.log_warning(
                    f"[HTTP FALLBACK] curl_cffi 请求失败，回退 urllib: {method.upper()} {url}"
                )
                try:
                    return await asyncio.to_thread(
                        self._sync_json_request_via_urllib,
                        method.upper(),
                        url,
                        headers,
                        json_data,
                        proxy_url,
                        request_timeout,
                    )
                except Exception as fallback_error:
                    debug_logger.log_error(
                        f"[HTTP FALLBACK] urllib 回退也失败: {fallback_error}"
                    )
                    # 不要把 curl=...; urllib=... 双 prefix 拼起来——这会破坏外层
                    # _is_retryable_network_error / _get_retry_reason 的 substring 匹配
                    # （curl 的措辞被 urllib= 段稀释），导致同类瞬断不被识别为可重试。
                    # 选 urllib 的错误作为 final message：urllib 是最后实际尝试的客户端，
                    # 它的错误措辞更接近真实网络层状态。
                    raise Exception(str(fallback_error)) from fallback_error

            raise Exception(f"Flow API request failed: {error_msg}")

    def _should_fallback_to_urllib(self, error_message: str) -> bool:
        """委托 flow.errors。"""
        return should_fallback_to_urllib(error_message)

    def _sync_json_request_via_urllib(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, Any]],
        json_data: Optional[Dict[str, Any]],
        proxy_url: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """委托 flow.transport。"""
        return sync_json_request_via_urllib(method, url, headers, json_data, proxy_url, timeout)

    def _is_timeout_error(self, error: Exception) -> bool:
        """委托 flow.errors。"""
        return is_timeout_error(error)

    def _is_retryable_network_error(self, error_str: str) -> bool:
        """委托 flow.errors。"""
        return is_retryable_network_error(error_str)

    def _get_control_plane_timeout(self) -> int:
        """控制轻量控制面请求的超时，避免认证/项目接口长时间挂起。"""
        return max(5, min(int(self.timeout or 0) or 120, 10))

    async def _acquire_image_launch_gate(
        self,
        token_id: Optional[int],
        token_image_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """图片请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_image_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    async def _acquire_video_launch_gate(
        self,
        token_id: Optional[int],
        token_video_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """视频请求不再做本地发车排队，直接进入取 token 并提交上游。"""
        return True, 0, 0

    async def _release_video_launch_gate(self, token_id: Optional[int]):
        """保留接口形状，当前无需释放任何本地发车状态。"""
        return

    async def _make_image_generation_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        attempt_trace: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """图片生成请求使用更短超时，并在网络超时时快速重试。"""
        request_timeout = config.flow_image_request_timeout
        total_attempts = max(1, config.flow_image_timeout_retry_count + 1)
        retry_delay = config.flow_image_timeout_retry_delay

        # 对于浏览器/远程浏览器打码链路，优先保持与打码时一致的出口。
        # 否则在首跳改走媒体代理时，容易触发 reCAPTCHA 校验失败并放大长尾。
        fingerprint = self._request_fingerprint_ctx.get()
        has_fingerprint_context = bool(isinstance(fingerprint, dict) and fingerprint)

        has_media_proxy = False
        if self.proxy_manager and config.flow_image_timeout_use_media_proxy_fallback:
            try:
                has_media_proxy = bool(await self.proxy_manager.get_media_proxy_url())
            except Exception:
                has_media_proxy = False
        prefer_media_first = bool(has_media_proxy and config.flow_image_prefer_media_proxy)

        if has_fingerprint_context and prefer_media_first:
            prefer_media_first = False
            debug_logger.log_info(
                "[IMAGE] 检测到打码浏览器指纹上下文，首跳固定走打码链路；"
                "媒体代理仅在网络超时时作为兜底回退。"
            )

        last_error: Optional[Exception] = None

        for attempt_index in range(total_attempts):
            if has_media_proxy:
                # 两次重试时采用“主链路 + 备链路”策略，避免每次都先卡在错误链路上。
                if attempt_index == 0:
                    prefer_media_proxy = prefer_media_first
                elif attempt_index == 1:
                    prefer_media_proxy = not prefer_media_first
                else:
                    prefer_media_proxy = prefer_media_first
            else:
                prefer_media_proxy = False
            route_label = "媒体代理链路" if prefer_media_proxy else "打码链路"
            http_attempt_started_at = time.time()
            http_attempt_info: Optional[Dict[str, Any]] = None
            if isinstance(attempt_trace, dict):
                http_attempt_info = {
                    "attempt": attempt_index + 1,
                    "route": route_label,
                    "timeout_seconds": request_timeout,
                    "used_media_proxy": bool(prefer_media_proxy),
                }
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=request_timeout,
                    use_media_proxy=prefer_media_proxy,
                    respect_fingerprint_proxy=not prefer_media_proxy,
                )
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int((time.time() - http_attempt_started_at) * 1000)
                    http_attempt_info["success"] = True
                    attempt_trace.setdefault("http_attempts", []).append(http_attempt_info)
                return result
            except Exception as e:
                last_error = e
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int((time.time() - http_attempt_started_at) * 1000)
                    http_attempt_info["success"] = False
                    http_attempt_info["timeout_error"] = bool(self._is_timeout_error(e))
                    http_attempt_info["error"] = str(e)[:240]
                    attempt_trace.setdefault("http_attempts", []).append(http_attempt_info)
                if not self._is_timeout_error(e) or attempt_index >= total_attempts - 1:
                    raise

                if has_media_proxy and total_attempts > 1:
                    next_prefer_media_proxy = (
                        not prefer_media_proxy if attempt_index == 0 else prefer_media_proxy
                    )
                else:
                    next_prefer_media_proxy = prefer_media_proxy
                next_route_label = "媒体代理链路" if next_prefer_media_proxy else "打码链路"
                debug_logger.log_warning(
                    f"[IMAGE] 图片生成请求网络超时，准备快速重试 "
                    f"({attempt_index + 2}/{total_attempts})，当前链路={route_label}，"
                    f"下一链路={next_route_label}，timeout={request_timeout}s"
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("图片生成请求失败")

    # ========== 认证相关 (使用ST) ==========

    @staticmethod
    def _extract_rotated_st_from_set_cookie(set_cookie_headers) -> Optional[str]:
        """委托 flow.response_parsers。"""
        return extract_rotated_st_from_set_cookie(set_cookie_headers)

    async def st_to_at(self, st: str) -> dict:
        """ST转AT；并捕获 labs.google 回发的轮换新 ST (rotated_st)。

        Returns:
            {
                "access_token": "AT",
                "expires": "2025-11-15T04:46:04.000Z",
                "user": {...},
                "rotated_st": "<新 ST 或不存在该键>"
            }
        """
        url = f"{self.labs_base_url}/auth/session"
        set_cookies: List[str] = []
        result = await self._make_request(
            method="GET",
            url=url,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
            capture_set_cookie=set_cookies,
        )
        rotated_st = self._extract_rotated_st_from_set_cookie(set_cookies)
        if rotated_st and rotated_st != st:
            result["rotated_st"] = rotated_st
        return result

    # ========== 项目管理 (使用ST) ==========

    async def create_project(self, st: str, title: str) -> str:
        """创建项目,返回project_id

        Args:
            st: Session Token
            title: 项目标题

        Returns:
            project_id (UUID)
        """
        url = f"{self.labs_base_url}/trpc/project.createProject"
        json_data = {
            "json": {
                "projectTitle": title,
                "toolName": "PINHOLE"
            }
        }
        max_retries = max(2, min(4, int(getattr(config, "flow_max_retries", 3) or 3)))
        request_timeout = max(self._get_control_plane_timeout(), min(self.timeout, 15))
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_st=True,
                    st_token=st,
                    timeout=request_timeout,
                )
                project_result = (
                    result.get("result", {})
                    .get("data", {})
                    .get("json", {})
                    .get("result", {})
                )
                project_id = project_result.get("projectId")
                if not project_id:
                    raise Exception("Invalid project.createProject response: missing projectId")
                return project_id
            except Exception as e:
                last_error = e
                retry_reason = "网络超时" if self._is_timeout_error(e) else self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[PROJECT] 创建项目失败，准备重试 ({retry_attempt + 2}/{max_retries}) "
                        f"title={title!r}, reason={retry_reason}: {e}"
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("创建项目失败")

    async def delete_project(self, st: str, project_id: str):
        """删除项目

        Args:
            st: Session Token
            project_id: 项目ID
        """
        url = f"{self.labs_base_url}/trpc/project.deleteProject"
        json_data = {
            "json": {
                "projectToDeleteId": project_id
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
        )

    # ========== 余额查询 (使用AT) ==========

    async def get_credits(self, at: str) -> dict:
        """查询余额

        Args:
            at: Access Token

        Returns:
            {
                "credits": 920,
                "userPaygateTier": "PAYGATE_TIER_ONE"
            }
        """
        url = f"{self.api_base_url}/credits"
        result = await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            timeout=self._get_control_plane_timeout(),
        )
        return result

    # ========== 图片上传 (使用AT) ==========

    def _detect_image_mime_type(self, image_bytes: bytes) -> str:
        """委托 shared.storage.media_types。"""
        return detect_image_mime_type(image_bytes)

    def _convert_to_jpeg(self, image_bytes: bytes) -> bytes:
        """委托 shared.storage.media_types。"""
        return convert_to_jpeg(image_bytes)

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id: Optional[str] = None
    ) -> str:
        """上传图片,返回mediaId

        Args:
            at: Access Token
            image_bytes: 图片字节数据
            aspect_ratio: 图片或视频宽高比（会自动转换为图片格式）
            project_id: 项目ID（新上传接口可使用）

        Returns:
            mediaId
        """
        # 转换视频aspect_ratio为图片aspect_ratio
        # VIDEO_ASPECT_RATIO_LANDSCAPE -> IMAGE_ASPECT_RATIO_LANDSCAPE
        # VIDEO_ASPECT_RATIO_PORTRAIT -> IMAGE_ASPECT_RATIO_PORTRAIT
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        # 自动检测图片 MIME 类型
        mime_type = self._detect_image_mime_type(image_bytes)

        # 编码为base64 (去掉前缀)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        # 优先尝试新版上传接口: /v1/flow/uploadImage
        # 若失败则自动回退到旧接口,保证兼容
        ext = "png" if "png" in mime_type else "jpg"
        upload_file_name = f"flow2api_upload_{int(time.time() * 1000)}.{ext}"
        new_url = f"{self.api_base_url}/flow/uploadImage"
        normalized_project_id = str(project_id or "").strip()
        new_client_context = {
            "tool": "PINHOLE"
        }
        if normalized_project_id:
            new_client_context["projectId"] = normalized_project_id

        new_json_data = {
            "clientContext": new_client_context,
            "fileName": upload_file_name,
            "imageBytes": image_base64,
            "isHidden": False,
            "isUserUploaded": True,
            "mimeType": mime_type
        }

        # 兼容回退：旧接口 :uploadUserImage
        legacy_url = f"{self.api_base_url}:uploadUserImage"
        legacy_json_data = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": mime_type,
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio
            },
            "clientContext": {
                "sessionId": self._generate_session_id(),
                "tool": "ASSET_MANAGER"
            }
        }
        max_retries = max(1, getattr(config, "flow_max_retries", 3))
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                new_result = await self._make_request(
                    method="POST",
                    url=new_url,
                    json_data=new_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True,
                    force_urllib=True,
                )
                media_id = (
                    new_result.get("media", {}).get("name")
                    or new_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                )
                if media_id:
                    return media_id
                raise Exception(f"Invalid upload response: missing media id, keys={list(new_result.keys())}")
            except Exception as new_upload_error:
                last_error = new_upload_error
                retry_reason = "网络超时" if self._is_timeout_error(new_upload_error) else self._get_retry_reason(str(new_upload_error))

                # 旧接口不携带 projectId，带项目上下文的上传一旦回退就可能把图片挂到错误项目。
                if normalized_project_id:
                    if retry_reason and retry_attempt < max_retries - 1:
                        debug_logger.log_warning(
                            f"[UPLOAD] Project-scoped upload 遇到{retry_reason}，准备重试新版接口 "
                            f"({retry_attempt + 2}/{max_retries}, project_id={normalized_project_id})..."
                        )
                        await asyncio.sleep(1)
                        continue
                    raise RuntimeError(
                        "Project-scoped image upload failed via /flow/uploadImage; "
                        "legacy :uploadUserImage fallback is disabled because it may attach media "
                        f"to a different project (project_id={normalized_project_id})."
                    ) from new_upload_error

                debug_logger.log_warning(
                    f"[UPLOAD] New upload API failed, fallback to legacy endpoint: {new_upload_error}"
                )

            try:
                legacy_result = await self._make_request(
                    method="POST",
                    url=legacy_url,
                    json_data=legacy_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True,
                    force_urllib=True,
                )

                media_id = (
                    legacy_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                    or legacy_result.get("media", {}).get("name")
                )
                if media_id:
                    return media_id
                raise Exception(f"Legacy upload response missing media id: keys={list(legacy_result.keys())}")
            except Exception as legacy_upload_error:
                last_error = legacy_upload_error
                retry_reason = "网络超时" if self._is_timeout_error(legacy_upload_error) else self._get_retry_reason(str(legacy_upload_error))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[UPLOAD] 上传遇到{retry_reason}，准备重试 ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("上传图片失败")

    # ========== 图片生成 (使用AT) - 同步返回 ==========

    async def generate_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None,
        token_id: Optional[int] = None,
        token_image_concurrency: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int], Awaitable[None]]] = None,
    ) -> tuple[dict, str, Dict[str, Any]]:
        """生成图片(同步返回)

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_name: NARWHAL / GEM_PIX / GEM_PIX_2 / IMAGEN_3_5
            aspect_ratio: 图片宽高比
            image_inputs: 参考图片列表(图生图时使用)

        Returns:
            (result, session_id, perf_trace)
            result: 上游返回的生成结果
            session_id: 本次成功图片生成请求使用的 sessionId
            perf_trace: 生成重试与链路耗时轨迹
        """
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"

        # 403/reCAPTCHA 重试逻辑
        max_retries = config.flow_max_retries
        last_error = None
        perf_trace: Dict[str, Any] = {
            "max_retries": max_retries,
            "generation_attempts": [],
        }
        
        for retry_attempt in range(max_retries):
            attempt_trace: Dict[str, Any] = {
                "attempt": retry_attempt + 1,
                "recaptcha_ok": False,
            }
            attempt_started_at = time.time()
            # 每次重试都重新获取 reCAPTCHA token
            recaptcha_started_at = time.time()
            if progress_callback is not None:
                await progress_callback("solving_image_captcha", 38)
            launch_gate_acquired = False
            launch_ok, launch_queue_ms, launch_stagger_ms = await self._acquire_image_launch_gate(
                token_id=token_id,
                token_image_concurrency=token_image_concurrency,
            )
            attempt_trace["launch_queue_ms"] = launch_queue_ms
            attempt_trace["launch_stagger_ms"] = launch_stagger_ms
            if not launch_ok:
                last_error = Exception("Image launch queue wait timeout")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="IMAGE_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_image_launch_gate(token_id)
            attempt_trace["recaptcha_ms"] = int((time.time() - recaptcha_started_at) * 1000)
            attempt_trace["recaptcha_ok"] = bool(recaptcha_token)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            if progress_callback is not None:
                await progress_callback("submitting_image", 48)
            session_id = self._generate_session_id()

            json_data = build_image_request(
                recaptcha_token=recaptcha_token,
                session_id=session_id,
                project_id=project_id,
                seed=random.randint(1, 999999),
                model_name=model_name,
                aspect_ratio=aspect_ratio,
                prompt=prompt,
                image_inputs=image_inputs,
                batch_id=str(uuid.uuid4()),
            )

            try:
                result = await self._make_image_generation_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    attempt_trace=attempt_trace,
                )
                attempt_trace["success"] = True
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                perf_trace["final_success_attempt"] = retry_attempt + 1
                self._clear_captcha_rejection(project_id)
                return result, session_id, perf_trace
            except Exception as e:
                last_error = e
                attempt_trace["success"] = False
                attempt_trace["error"] = str(e)[:240]
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # 所有重试都失败
        perf_trace["final_success_attempt"] = None
        if last_error is not None:
            raise last_error
        raise RuntimeError("图片生成请求失败")

    async def upsample_image(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
        user_paygate_tier: str = "PAYGATE_TIER_NOT_PAID",
        session_id: Optional[str] = None,
        token_id: Optional[int] = None
    ) -> str:
        """放大图片到 2K/4K

        Args:
            at: Access Token
            project_id: 项目ID
            media_id: 图片的 mediaId (从 batchGenerateImages 返回的 media[0]["name"])
            target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K 或 UPSAMPLE_IMAGE_RESOLUTION_4K
            user_paygate_tier: 用户等级 (如 PAYGATE_TIER_NOT_PAID / PAYGATE_TIER_ONE)
            session_id: 可选，复用图片生成请求的 sessionId

        Returns:
            base64 编码的图片数据
        """
        url = f"{self.api_base_url}/flow/upsampleImage"

        # 403/reCAPTCHA/500 重试逻辑 - 最多重试3次
        max_retries = config.flow_max_retries
        last_error = None

        for retry_attempt in range(max_retries):
            # 获取 reCAPTCHA token - 使用 IMAGE_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(
                project_id,
                action="IMAGE_GENERATION",
                token_id=token_id
            )
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise last_error
            upsample_session_id = session_id or self._generate_session_id()

            json_data = build_image_upsample_request(
                media_id=media_id,
                target_resolution=target_resolution,
                recaptcha_token=recaptcha_token,
                session_id=upsample_session_id,
                project_id=project_id,
                user_paygate_tier=user_paygate_tier,
            )

            # 4K/2K 放大使用专用超时，因为返回的 base64 数据量很大
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=config.upsample_timeout
                )

                # 返回 base64 编码的图片
                self._clear_captcha_rejection(project_id)
                return result.get("encodedImage", "")
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        if last_error is not None:
            raise last_error
        raise RuntimeError("图片放大请求失败")

    # ========== 视频生成 (使用AT) - 异步返回 ==========

    def _build_video_text_input(self, prompt: str, use_v2_model_config: bool = False) -> Dict[str, Any]:
        """委托 flow.request_builders。"""
        return build_video_text_input(prompt, use_v2_model_config)

    async def generate_video_text(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """文生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_t2v_fast 等
            aspect_ratio: 视频宽高比
            user_paygate_tier: 用户等级

        Returns:
            {
                "operations": [{
                    "operation": {"name": "task_id"},
                    "sceneId": "uuid",
                    "status": "MEDIA_GENERATION_STATUS_PENDING"
                }],
                "remainingCredits": 900
            }
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            json_data = build_video_text_request(
                recaptcha_token=recaptcha_token,
                session_id=self._generate_session_id(),
                project_id=project_id,
                user_paygate_tier=user_paygate_tier,
                aspect_ratio=aspect_ratio,
                seed=random.randint(1, 99999),
                text_input=self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                model_key=model_key,
                scene_id=str(uuid.uuid4()),
                use_v2_model_config=use_v2_model_config,
                batch_id=str(uuid.uuid4()) if use_v2_model_config else None,
            )

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                self._clear_captcha_rejection(project_id)
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        # 所有重试都失败
        if last_error is not None:
            raise last_error
        raise RuntimeError("视频生成请求失败 (T2V)")

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """图生视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_r2v_fast_landscape
            aspect_ratio: 视频宽高比
            reference_images: 参考图片列表 [{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "..."}]
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoReferenceImages"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] 生成",
                )
                if should_retry:
                    continue
                raise last_error
            json_data = build_video_reference_images_request(
                recaptcha_token=recaptcha_token,
                session_id=self._generate_session_id(),
                project_id=project_id,
                user_paygate_tier=user_paygate_tier,
                aspect_ratio=aspect_ratio,
                seed=random.randint(1, 99999),
                prompt=prompt,
                model_key=model_key,
                reference_images=reference_images,
                scene_id=str(uuid.uuid4()),
                batch_id=str(uuid.uuid4()),
            )

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                self._clear_captcha_rejection(project_id)
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] 生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        # 所有重试都失败
        if last_error is not None:
            raise last_error
        raise RuntimeError("视频生成请求失败 (R2V - reference images)")

    async def generate_video_start_end(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """收尾帧生成视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_i2v_s_fast_fl
            aspect_ratio: 视频宽高比
            start_media_id: 起始帧mediaId
            end_media_id: 结束帧mediaId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartAndEndImage"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首尾帧生成",
                )
                if should_retry:
                    continue
                raise last_error
            json_data = build_video_image_request(
                recaptcha_token=recaptcha_token,
                session_id=self._generate_session_id(),
                project_id=project_id,
                user_paygate_tier=user_paygate_tier,
                aspect_ratio=aspect_ratio,
                seed=random.randint(1, 99999),
                text_input=self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                model_key=model_key,
                start_media_id=start_media_id,
                end_media_id=end_media_id,
                scene_id=str(uuid.uuid4()),
                use_v2_model_config=use_v2_model_config,
                batch_id=str(uuid.uuid4()) if use_v2_model_config else None,
            )

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                self._clear_captcha_rejection(project_id)
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首尾帧生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        # 所有重试都失败
        if last_error is not None:
            raise last_error
        raise RuntimeError("视频生成请求失败 (start-end frames)")

    async def generate_video_start_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """仅首帧生成视频,返回task_id

        Args:
            at: Access Token
            project_id: 项目ID
            prompt: 提示词
            model_key: veo_3_1_i2v_s_fast_fl等
            aspect_ratio: 视频宽高比
            start_media_id: 起始帧mediaId
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartImage"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            # 每次重试都重新获取 reCAPTCHA token - 视频使用 VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首帧生成",
                )
                if should_retry:
                    continue
                raise last_error
            json_data = build_video_image_request(
                recaptcha_token=recaptcha_token,
                session_id=self._generate_session_id(),
                project_id=project_id,
                user_paygate_tier=user_paygate_tier,
                aspect_ratio=aspect_ratio,
                seed=random.randint(1, 99999),
                text_input=self._build_video_text_input(prompt, use_v2_model_config=use_v2_model_config),
                model_key=model_key,
                start_media_id=start_media_id,
                scene_id=str(uuid.uuid4()),
                use_v2_model_config=use_v2_model_config,
                batch_id=str(uuid.uuid4()) if use_v2_model_config else None,
            )

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                self._clear_captcha_rejection(project_id)
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] 首帧生成",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        # 所有重试都失败
        if last_error is not None:
            raise last_error
        raise RuntimeError("视频生成请求失败 (I2V - start image)")

    # ========== 视频放大 (Video Upsampler) ==========

    async def upsample_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str,
        model_key: str,
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """视频放大到 4K/1080P，返回 task_id

        Args:
            at: Access Token
            project_id: 项目ID
            video_media_id: 视频的 mediaId
            aspect_ratio: 视频宽高比 VIDEO_ASPECT_RATIO_PORTRAIT/LANDSCAPE
            resolution: VIDEO_RESOLUTION_4K 或 VIDEO_RESOLUTION_1080P
            model_key: veo_3_1_upsampler_4k 或 veo_3_1_upsampler_1080p

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoUpsampleVideo"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = config.flow_max_retries
        last_error = None
        
        for retry_attempt in range(max_retries):
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise last_error
            json_data = build_video_upsample_request(
                recaptcha_token=recaptcha_token,
                session_id=self._generate_session_id(),
                project_id=project_id,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                seed=random.randint(1, 99999),
                video_media_id=video_media_id,
                model_key=model_key,
                scene_id=str(uuid.uuid4()),
            )

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                self._clear_captcha_rejection(project_id)
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] 放大",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        if last_error is not None:
            raise last_error
        raise RuntimeError("视频放大请求失败")

    # ========== 视频延长 (使用AT) ==========

    async def extend_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        workflow_id: str,
        model_key: str,
        prompt: str = "Continue this video naturally, maintaining consistent visual style, motion, and environment.",
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """视频延长约8秒，返回 task_id

        Args:
            at: Access Token
            project_id: 项目ID
            video_media_id: 原始视频的 mediaId
            aspect_ratio: 视频宽高比 VIDEO_ASPECT_RATIO_PORTRAIT/LANDSCAPE
            workflow_id: 工作流ID
            model_key: 延长模型 key (veo_3_1_extend_landscape / veo_3_1_extend_portrait)
            prompt: 延长提示词
            user_paygate_tier: 用户等级

        Returns:
            同 generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoExtendVideo"

        # 403/reCAPTCHA 重试逻辑 - 最多重试3次
        max_retries = config.flow_max_retries
        last_error = None

        for retry_attempt in range(max_retries):
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] 延长",
                )
                if should_retry:
                    continue
                raise last_error
            json_data = build_video_extend_request(
                recaptcha_token=recaptcha_token,
                session_id=self._generate_session_id(),
                project_id=project_id,
                user_paygate_tier=user_paygate_tier,
                aspect_ratio=aspect_ratio,
                seed=random.randint(1, 99999),
                text_input=self._build_video_text_input(prompt, use_v2_model_config=True),
                model_key=model_key,
                workflow_id=workflow_id,
                video_media_id=video_media_id,
                batch_id=str(uuid.uuid4()),
            )

            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
                self._clear_captcha_rejection(project_id)
                return result
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] 延长",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        # 所有重试都失败
        if last_error is not None:
            raise last_error
        raise RuntimeError("视频延长请求失败")

    async def concatenate_videos(
        self,
        at: str,
        original_media_id: str,
        extended_media_id: str,
        original_duration_nanos: int = 8000,
        extended_start_offset: str = "1s",
    ) -> dict:
        """拼接原始视频和延长视频

        Args:
            at: Access Token
            original_media_id: 原始视频的 mediaGenerationId
            extended_media_id: 延长视频的 mediaGenerationId
            original_duration_nanos: 原始视频时长，默认 8000（Flow API lengthNanos 字段，实际非纳秒单位）
            extended_start_offset: 延长视频起始偏移，默认 "1s"（跳过1秒重叠）

        Returns:
            操作结果，包含 operation name 用于后续轮询
        """
        url = f"{self.api_base_url}:runVideoFxConcatenation"

        json_data = build_video_concatenation_request(
            original_media_id=original_media_id,
            extended_media_id=extended_media_id,
            original_duration_nanos=original_duration_nanos,
            extended_start_offset=extended_start_offset,
        )

        return await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )

    async def check_concatenation_status(self, at: str, operation_name: str) -> dict:
        """查询视频拼接状态

        Args:
            at: Access Token
            operation_name: concatenate_videos 返回的 operation name

        Returns:
            拼接状态结果
        """
        url = f"{self.api_base_url}:runVideoFxCheckConcatenationStatus"

        json_data = {
            "operation": {
                "operation": {
                    "name": operation_name
                }
            }
        }

        return await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )

    # ========== 任务轮询 (使用AT) ==========

    async def check_video_status(self, at: str, operations: Union[List[Dict], Dict[str, Any]]) -> dict:
        """查询视频生成状态

        Args:
            at: Access Token
            operations: 操作列表 [{"operation": {"name": "task_id"}, "sceneId": "...", "status": "..."}]

        Returns:
            {
                "operations": [{
                    "operation": {
                        "name": "task_id",
                        "metadata": {...}  # 完成时包含视频信息
                    },
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                }]
            }
        """
        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"

        json_data = build_video_status_request(operations)
        max_retries = max(1, getattr(config, "flow_max_retries", 3))
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                return await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at
                )
            except Exception as e:
                last_error = e
                retry_reason = "网络超时" if self._is_timeout_error(e) else self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[VIDEO POLL] 状态查询遇到{retry_reason}，准备重试 ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("视频状态查询失败")

    async def get_media_workflow_id(self, at: str, media_name: str, project_id: str) -> Optional[str]:
        """通过 media 格式轮询获取 workflowId"""
        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"
        json_data = {
            "media": [{"name": media_name, "projectId": project_id}]
        }
        try:
            result = await self._make_request(
                method="POST", url=url, json_data=json_data, use_at=True, at_token=at
            )
            media_list = result.get("media", [])
            if media_list:
                return media_list[0].get("workflowId")
        except Exception as e:
            debug_logger.log_error(f"[WORKFLOW_ID] Failed to get workflow_id for {media_name}: {e}")
        return None

    async def get_media_url(
        self,
        st: str,
        media_name: str,
        thumbnail: bool = False,
    ) -> Optional[str]:
        """通过 labs.google trpc 接口换签名 CDN URL。

        2026-05 起上游不再在 ``batchCheckAsyncVideoGenerationStatus`` 的
        ``media[].video.generatedVideo`` 里返回 ``fifeUrl``。前端流程改成：
        生成完成后 ``GET labs.google/fx/api/trpc/media.getMediaUrlRedirect
        ?name={media_id}``，服务端用 ST cookie 鉴权后返回 ``307`` ，``Location``
        头里的 ``https://flow-content.google/{video|image}/{name}?Expires=...
        &KeyName=...&Signature=...`` 才是真正可下载的签名 URL（有效约 5 小时）。

        Args:
            st: 业务账号的 ``__Secure-next-auth.session-token`` (ST)。
            media_name: ``media[0].name`` / ``primaryMediaId`` 这种 UUID。
            thumbnail: 取缩略图（image）还是原视频；默认拿完整 media。
        """
        if not media_name or not st:
            return None

        url = f"{self.labs_base_url}/trpc/media.getMediaUrlRedirect"
        params = {"name": media_name}
        if thumbnail:
            params["mediaUrlType"] = "MEDIA_URL_TYPE_THUMBNAIL"

        proxy_url = None
        if self.proxy_manager:
            try:
                proxy_url = await self.proxy_manager.get_proxy_url()
            except Exception:
                proxy_url = None

        headers = self._build_request_headers(
            st_token=st,
            use_st=True,
            fingerprint=self.get_request_fingerprint(),
        )
        impersonate = self._select_impersonate_for_headers(headers)

        try:
            async with AsyncSession() as session:
                kwargs = {
                    "headers": headers,
                    "params": params,
                    "proxy": proxy_url,
                    "timeout": self.timeout,
                    "allow_redirects": False,
                }
                if impersonate:
                    kwargs["impersonate"] = impersonate
                response = await session.get(url, **kwargs)
        except Exception as e:
            debug_logger.log_error(
                f"[MEDIA URL] 调 getMediaUrlRedirect 失败 media={media_name}: {e}"
            )
            return None

        status = getattr(response, "status_code", 0)
        if status in (301, 302, 303, 307, 308):
            location = response.headers.get("location") or response.headers.get("Location")
            if location:
                return location
            debug_logger.log_error(
                f"[MEDIA URL] {status} redirect 但缺 Location 头 media={media_name}"
            )
            return None

        debug_logger.log_error(
            f"[MEDIA URL] 期望 307 redirect 但拿到 HTTP {status} media={media_name}"
        )
        return None

    # ========== 媒体删除 (使用ST) ==========

    async def delete_media(self, st: str, media_names: List[str]):
        """删除媒体

        Args:
            st: Session Token
            media_names: 媒体ID列表
        """
        url = f"{self.labs_base_url}/trpc/media.deleteMedia"
        json_data = {
            "json": {
                "names": media_names
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st
        )

    # ========== 辅助方法 ==========

    async def _handle_retryable_generation_error(
        self,
        error: Exception,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        """统一处理生成链路的重试判定与打码自愈通知。"""
        error_str = str(error)
        retry_reason = self._get_retry_reason(error_str)
        # Only notify captcha service for retryable or captcha-related errors.
        # Non-retryable errors (INVALID_ARGUMENT, etc.) should not trigger browser rebuilds.
        if retry_reason:
            await self._notify_browser_captcha_error(
                browser_id=browser_id,
                project_id=project_id,
                error_reason=retry_reason,
                error_message=error_str,
            )
        if not retry_reason:
            return False

        is_terminal_attempt = retry_attempt >= max_retries - 1

        if is_terminal_attempt:
            debug_logger.log_warning(
                f"{log_prefix}遇到{retry_reason}，已达到最大重试次数({max_retries})，本次请求失败并执行关闭回收。"
            )
            return False

        debug_logger.log_warning(
            f"{log_prefix}遇到{retry_reason}，正在重新获取验证码重试 ({retry_attempt + 2}/{max_retries})..."
        )
        await asyncio.sleep(1)
        return True

    async def _handle_missing_recaptcha_token(
        self,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        token_error = Exception("Failed to obtain reCAPTCHA token")
        return await self._handle_retryable_generation_error(
            error=token_error,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            browser_id=browser_id,
            project_id=project_id,
            log_prefix=log_prefix,
        )

    def _get_retry_reason(self, error_str: str) -> Optional[str]:
        """委托 flow.errors。"""
        return get_retry_reason(error_str)

    def _is_captcha_rejection_reason(
        self,
        error_reason: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """委托 flow.errors。"""
        return is_captcha_rejection_reason(error_reason, error_message)

    async def notify_browser_captcha_request_success(self, project_id: Optional[str] = None) -> None:
        """通知浏览器打码服务：GENERATION 真正成功，清零该 slot 的失败 streak。

        必须由 generation_handler.py 在 ✅ 生成成功路径调用。这是判定 Google
        是否真接受 reCAPTCHA token 的唯一可靠信号（token 本地拿到不算）。
        """
        if not project_id or config.captcha_method != "personal":
            return
        try:
            from .browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(self.db)
            await service.report_flow_success(project_id)
        except Exception as e:
            debug_logger.log_warning(f"[reCAPTCHA] 通知 personal 打码服务 success 失败: {e}")

    async def _notify_browser_captcha_error(
        self,
        browser_id: Optional[Union[int, str]] = None,
        project_id: Optional[str] = None,
        error_reason: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """通知浏览器打码服务执行失败自愈。
        
        Args:
            browser_id: browser 模式使用的浏览器 ID
            project_id: personal 模式使用的 project_id
            error_reason: 已归类的错误原因
            error_message: 原始错误文本
        """
        if project_id and self._is_captcha_rejection_reason(error_reason, error_message):
            self._record_captcha_rejection(project_id)

        # browser (playwright) 模式已废弃 — main.py:54 强制改写为 personal
        if config.captcha_method == "personal" and project_id:
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA] 通知 personal 打码服务失败: {e}")
        elif config.captcha_method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/error",
                    json_data={"error_reason": error_reason or error_message or "upstream_error"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] 上报 error 失败: {e}")

    async def _notify_browser_captcha_request_finished(self, browser_id: Optional[Union[int, str]] = None):
        """通知有头浏览器：上游图片/视频请求已结束，可关闭对应打码浏览器。"""
        # browser (playwright) 模式已废弃；personal/resident 模式无需 finish 信号（标签页常驻）
        if config.captcha_method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/finish",
                    json_data={"status": "success"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] 上报 finish 失败: {e}")

    def _generate_session_id(self) -> str:
        """生成sessionId: ;timestamp"""
        return f";{int(time.time() * 1000)}"

    def _generate_scene_id(self) -> str:
        """生成sceneId: UUID"""
        return str(uuid.uuid4())

    def _get_remote_browser_service_config(self) -> tuple[str, str, int]:
        base_url = (config.remote_browser_base_url or "").strip().rstrip("/")
        api_key = (config.remote_browser_api_key or "").strip()
        timeout = max(5, int(config.remote_browser_timeout or 60))

        if not base_url:
            raise RuntimeError("remote_browser 服务地址未配置")
        if not api_key:
            raise RuntimeError("remote_browser API Key 未配置")

        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise RuntimeError("remote_browser 服务地址格式错误")

        return base_url, api_key, timeout

    @staticmethod
    def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
        """委托 flow.transport。"""
        return build_remote_browser_http_timeout(read_timeout)

    @staticmethod
    def _parse_json_response_text(text: str) -> Optional[Any]:
        """委托 flow.response_parsers。"""
        return parse_json_response_text(text)

    @staticmethod
    async def _stdlib_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        """委托 flow.transport。"""
        return await stdlib_json_http_request(method, url, headers, payload, timeout)

    @staticmethod
    async def _sync_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        """委托 flow.transport。"""
        return await sync_json_http_request(method, url, headers, payload, timeout)

    async def _call_remote_browser_service(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        timeout_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        base_url, api_key, timeout = self._get_remote_browser_service_config()
        url = f"{base_url}{path}"
        effective_timeout = max(5, int(timeout_override or timeout))

        status_code, payload, response_text = await self._sync_json_http_request(
            method=method,
            url=url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=json_data,
            timeout=effective_timeout,
        )

        if status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or str(payload)
            if not detail:
                detail = (response_text or "").strip() or f"HTTP {status_code}"
            raise RuntimeError(f"remote_browser 请求失败: {detail}")

        if not isinstance(payload, dict):
            raise RuntimeError("remote_browser 返回格式错误")

        return payload

    async def prefill_remote_browser_pool(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        cooldown_seconds: float = 8.0,
    ) -> bool:
        """让本地 remote_browser 服务提前开始补池，尽量把取 token 等待搬到前面。"""
        if config.captcha_method != "remote_browser":
            return False

        normalized_project = str(project_id or "").strip()
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        if not normalized_project:
            return False

        cache_key = f"{normalized_project}|{normalized_action}|{int(token_id or 0)}"
        now_value = time.monotonic()
        last_sent = float(self._remote_browser_prefill_last_sent.get(cache_key, 0.0) or 0.0)
        if (now_value - last_sent) < max(0.5, float(cooldown_seconds)):
            return False

        try:
            await self._call_remote_browser_service(
                method="POST",
                path="/api/v1/prefill",
                json_data={
                    "project_id": normalized_project,
                    "action": normalized_action,
                    "token_id": token_id,
                },
                timeout_override=3,
            )
            self._remote_browser_prefill_last_sent[cache_key] = now_value
            return True
        except Exception as e:
            debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] prefill 失败: {e}")
            return False

    async def prefill_remote_browser_for_tokens(self, tokens: List[Any], action: str = "IMAGE_GENERATION") -> int:
        if config.captcha_method != "remote_browser":
            return 0

        unique_projects: List[str] = []
        seen_projects = set()
        for token in tokens or []:
            project_id = str(getattr(token, "current_project_id", "") or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            unique_projects.append(project_id)

        warmed = 0
        for project_id in unique_projects:
            if await self.prefill_remote_browser_pool(project_id, action=action):
                warmed += 1
        return warmed

    def _resolve_remote_browser_solve_timeout(self, action: str) -> int:
        base_timeout = max(5, int(config.remote_browser_timeout or 60))
        action_name = str(action or "").strip().upper()

        # 这里只是拿 reCAPTCHA token，不应该跟整条生成链路共用数百秒级超时。
        target_timeout = 45 if action_name == "VIDEO_GENERATION" else 35
        return max(12, min(base_timeout, target_timeout))

    async def _get_recaptcha_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None
    ) -> tuple[Optional[str], Optional[Union[int, str]]]:
        """获取reCAPTCHA token - 支持多种打码方式
        
        Args:
            project_id: 项目ID
            action: reCAPTCHA action类型
                - IMAGE_GENERATION: 图片生成和2K/4K图片放大 (默认)
                - VIDEO_GENERATION: 视频生成和视频放大
            token_id: 当前业务 token id（browser 模式下用于读取 token 级打码代理）
        
        Returns:
            (token, browser_id) 元组。
            - browser 模式: browser_id 为本地浏览器 ID
            - remote_browser 模式: browser_id 为远程 session_id
            - 其他模式: browser_id 为 None
        """
        captcha_method = config.captcha_method
        debug_logger.log_info(f"[reCAPTCHA] 开始获取 token: method={captcha_method}, project_id={project_id}, action={action}")
        await self._wait_for_captcha_cooldown(project_id, action)

        # 内置浏览器打码 (nodriver)
        if captcha_method == "personal":
            debug_logger.log_info(f"[reCAPTCHA] 使用 personal 模式")
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                debug_logger.log_info(f"[reCAPTCHA] 导入 BrowserCaptchaService 成功")
                service = await BrowserCaptchaService.get_instance(self.db)
                debug_logger.log_info(f"[reCAPTCHA] 获取服务实例成功，准备调用 get_token")
                token = await service.get_token(project_id, action)
                debug_logger.log_info(f"[reCAPTCHA] get_token 返回: present={bool(token)}, length={len(token) if token else 0}")
                if isinstance(token, str) and 0 < len(token) < 100:
                    debug_logger.log_error(
                        f"[reCAPTCHA] personal 模式返回了疑似伪 token (len={len(token)}), 丢弃避免提交"
                    )
                    token = None
                fingerprint = service.get_last_fingerprint() if token else None
                self._set_request_fingerprint(fingerprint if token else None)
                self._set_request_browser_context(
                    {"method": "personal", "project_id": project_id} if token else None
                )
                return token, None
            except RuntimeError as e:
                # 捕获 Docker 环境或依赖缺失的明确错误
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Personal] {error_msg}")
                print(f"[reCAPTCHA] ❌ 内置浏览器打码失败: {error_msg}")
                self.clear_request_fingerprint()
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 导入失败: {str(e)}")
                print(f"[reCAPTCHA] ❌ nodriver 未安装，请运行: pip install nodriver")
                self.clear_request_fingerprint()
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] 错误: {str(e)}")
                self.clear_request_fingerprint()
                return None, None
        # captcha_method == "browser" (playwright) 已废弃 — 见 main.py:54 自动改写。
        elif captcha_method == "remote_browser":
            try:
                solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                payload = await self._call_remote_browser_service(
                    method="POST",
                    path="/api/v1/solve",
                    json_data={
                        "project_id": project_id,
                        "action": action,
                        "token_id": token_id,
                    },
                    timeout_override=solve_timeout,
                )
                token = payload.get("token")
                session_id = payload.get("session_id")
                if isinstance(token, str) and 0 < len(token) < 100:
                    debug_logger.log_error(
                        f"[reCAPTCHA] remote_browser 返回了疑似伪 token (len={len(token)}), 丢弃避免提交"
                    )
                    token = None
                fingerprint = payload.get("fingerprint") if isinstance(payload.get("fingerprint"), dict) else None
                self._set_request_fingerprint(fingerprint if token else None)
                self._set_request_browser_context(None)
                if not token or not session_id:
                    raise RuntimeError(f"remote_browser 返回缺少 token/session_id: {payload}")
                return token, str(session_id)
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA RemoteBrowser] 错误: {str(e)}")
                self.clear_request_fingerprint()
                return None, None
        # API打码服务
        elif captcha_method in ["yescaptcha", "capmonster", "ezcaptcha", "capsolver"]:
            self.clear_request_fingerprint()
            token = await self._get_api_captcha_token(captcha_method, project_id, action)
            if isinstance(token, str) and 0 < len(token) < 100:
                debug_logger.log_error(
                    f"[reCAPTCHA] {captcha_method} 返回了疑似伪 token (len={len(token)}), 丢弃避免提交"
                )
                token = None
            return token, None
        else:
            debug_logger.log_info(f"[reCAPTCHA] 未知的打码方式: {captcha_method}")
            self.clear_request_fingerprint()
            return None, None

    async def _get_api_captcha_token(self, method: str, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
        """委托 captcha.api_solver。"""
        return await get_api_captcha_token(method, project_id, action)
