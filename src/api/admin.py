"""Admin API routes"""
import asyncio
import json
from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator
from typing import Optional, List, Dict, Any, Literal
import secrets
import time
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession
from ..core.auth import AuthManager
from ..core.database import Database
from ..core.config import config
from ..services.token_manager import TokenDeletionConflictError, TokenManager
from ..services.proxy_manager import ProxyManager
from ..services.concurrency_manager import ConcurrencyManager
from ..services.onboarding import OnboardingService, OnboardingServiceError
from ..core.cookie_extractor import extract_session_token
from ..core.models import OnboardingJob
from ..core.token_states import TOKEN_REASON_MANUAL_DISABLED

try:
    import httpx
except ImportError:
    httpx = None


from ..services.flow.transport import stdlib_json_http_request, sync_json_http_request
from .admin_helpers import (
    _build_proxy_map,
    _build_remote_browser_http_timeout,
    _extract_error_summary,
    _guess_client_hints_from_user_agent,
    _guess_impersonate_from_user_agent,
    _mask_token,
    _normalize_http_base_url,
    _parse_json_response_text,
    _truncate_text,
    _validate_browser_proxy_url,
)

router = APIRouter()

# Dependency injection
token_manager: Optional[TokenManager] = None
proxy_manager: Optional[ProxyManager] = None
db: Optional[Database] = None
concurrency_manager: Optional[ConcurrencyManager] = None
onboarding_service: Optional[OnboardingService] = None

# Store active admin session tokens (in production, use Redis or database)
active_admin_tokens = set()
SUPPORTED_API_CAPTCHA_METHODS = {"yescaptcha", "capmonster", "ezcaptcha", "capsolver"}


def _get_remote_browser_client_config() -> tuple[str, str, int]:
    base_url = _normalize_http_base_url(config.remote_browser_base_url)
    api_key = (config.remote_browser_api_key or "").strip()
    if not api_key:
        raise RuntimeError("远程打码服务 API Key 未配置")
    timeout = max(5, int(config.remote_browser_timeout or 60))
    return base_url, api_key, timeout


_ADMIN_HTTP_ERROR_PREFIX = "远程打码服务请求失败"


async def _stdlib_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple[int, Optional[Any], str]:
    """委托 services.flow.transport（去重）。保留 admin 侧错误文案。"""
    return await stdlib_json_http_request(
        method, url, headers, payload, timeout, error_prefix=_ADMIN_HTTP_ERROR_PREFIX)


async def _sync_json_http_request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    timeout: int,
) -> tuple[int, Optional[Any], str]:
    """委托 services.flow.transport（去重）。保留 admin 侧错误文案。"""
    return await sync_json_http_request(
        method, url, headers, payload, timeout, error_prefix=_ADMIN_HTTP_ERROR_PREFIX)


async def _resolve_score_test_verify_proxy(
    captcha_method: str,
    browser_proxy_enabled: bool,
    browser_proxy_url: str
) -> tuple[Optional[Dict[str, str]], bool, str, str]:
    """
    选择 score-test 的 verify 请求代理，优先与浏览器打码代理保持一致。
    返回: (proxies, used, source, proxy_url)
    """
    # 浏览器打码模式优先使用 browser_proxy，确保与取 token 出口一致
    if captcha_method in {"browser", "personal"} and browser_proxy_enabled and browser_proxy_url:
        proxy_map = _build_proxy_map(browser_proxy_url)
        if proxy_map:
            return proxy_map, True, "captcha_browser_proxy", browser_proxy_url

    # 退回请求代理配置
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxy_map = _build_proxy_map(proxy_cfg.proxy_url)
                if proxy_map:
                    return proxy_map, True, "request_proxy", proxy_cfg.proxy_url
    except Exception:
        pass

    return None, False, "none", ""


async def _solve_recaptcha_with_api_service(
    method: str,
    website_url: str,
    website_key: str,
    action: str,
    enterprise: bool = False
) -> Optional[str]:
    """使用当前配置的第三方打码服务获取 token。"""
    if method == "yescaptcha":
        client_key = config.yescaptcha_api_key
        base_url = config.yescaptcha_base_url
        task_type = "RecaptchaV3TaskProxylessM1"
    elif method == "capmonster":
        client_key = config.capmonster_api_key
        base_url = config.capmonster_base_url
        task_type = "RecaptchaV3TaskProxyless"
    elif method == "ezcaptcha":
        client_key = config.ezcaptcha_api_key
        base_url = config.ezcaptcha_base_url
        task_type = "ReCaptchaV3TaskProxylessS9"
    elif method == "capsolver":
        client_key = config.capsolver_api_key
        base_url = config.capsolver_base_url
        task_type = "ReCaptchaV3EnterpriseTaskProxyLess" if enterprise else "ReCaptchaV3TaskProxyLess"
    else:
        raise RuntimeError(f"不支持的打码方式: {method}")

    if not client_key:
        raise RuntimeError(f"{method} API Key 未配置")

    task: Dict[str, Any] = {
        "websiteURL": website_url,
        "websiteKey": website_key,
        "type": task_type,
        "pageAction": action,
    }

    if enterprise and method == "capsolver":
        task["isEnterprise"] = True

    create_url = f"{base_url.rstrip('/')}/createTask"
    get_url = f"{base_url.rstrip('/')}/getTaskResult"

    # Do not use curl_cffi impersonation for captcha API JSON endpoints: some ASGI servers
    # (for example FastAPI/Uvicorn) may receive an empty body and return 422.
    async with AsyncSession() as session:
        create_resp = await session.post(
            create_url,
            json={"clientKey": client_key, "task": task},
            timeout=30
        )
        create_json = create_resp.json()
        task_id = create_json.get("taskId")

        if not task_id:
            error_desc = create_json.get("errorDescription") or create_json.get("errorMessage") or str(create_json)
            raise RuntimeError(f"{method} createTask 失败: {error_desc}")

        for _ in range(40):
            poll_resp = await session.post(
                get_url,
                json={"clientKey": client_key, "taskId": task_id},
                timeout=30
            )
            poll_json = poll_resp.json()
            if poll_json.get("status") == "ready":
                solution = poll_json.get("solution", {}) or {}
                token = solution.get("gRecaptchaResponse") or solution.get("token")
                if token:
                    return token
                raise RuntimeError(f"{method} 返回结果缺少 token: {poll_json}")

            if poll_json.get("errorId") not in (None, 0):
                error_desc = poll_json.get("errorDescription") or poll_json.get("errorMessage") or str(poll_json)
                raise RuntimeError(f"{method} getTaskResult 失败: {error_desc}")

            await asyncio.sleep(3)

    raise RuntimeError(f"{method} 获取 token 超时")


async def _score_test_with_remote_browser_service(
    website_url: str,
    website_key: str,
    verify_url: str,
    action: str,
    enterprise: bool = False,
) -> Dict[str, Any]:
    """调用远程有头打码服务执行页面内打码+分数校验。"""
    base_url, api_key, timeout = _get_remote_browser_client_config()
    endpoint = f"{base_url}/api/v1/custom-score"
    request_payload = {
        "website_url": website_url,
        "website_key": website_key,
        "verify_url": verify_url,
        "action": action,
        "enterprise": enterprise,
    }

    status_code, response_payload, response_text = await _sync_json_http_request(
        method="POST",
        url=endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        payload=request_payload,
        timeout=timeout,
    )

    if status_code >= 400:
        detail = ""
        if isinstance(response_payload, dict):
            detail = response_payload.get("detail") or response_payload.get("message") or str(response_payload)
        if not detail:
            detail = (response_text or "").strip()
        raise RuntimeError(f"远程打码服务请求失败 (HTTP {status_code}): {detail or '未知错误'}")

    if not isinstance(response_payload, dict):
        raise RuntimeError("远程打码服务返回格式错误")
    return response_payload


def set_dependencies(
    tm: Optional[TokenManager],
    pm: Optional[ProxyManager],
    database: Optional[Database],
    cm: Optional[ConcurrencyManager] = None,
    onboarding: Optional[OnboardingService] = None,
):
    """Set service instances used by the admin router."""
    global token_manager, proxy_manager, db, concurrency_manager, onboarding_service
    token_manager = tm
    proxy_manager = pm
    db = database
    concurrency_manager = cm
    onboarding_service = onboarding


# ========== Request Models ==========

class LoginRequest(BaseModel):
    username: str
    password: str


class AddTokenRequest(BaseModel):
    st: Optional[str] = None
    raw: Optional[str] = None  # 粘贴 cookies.txt 全文 / Cookie 头 / JSON，自动抽取 ST
    project_id: Optional[str] = None  # 用户可选输入project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1


def resolve_st_from_request(st: Optional[str], raw: Optional[str]) -> str:
    """优先用 raw 粘贴内容抽取 ST；否则用直接传入的 st。两者皆空抛 ValueError。"""
    if raw and raw.strip():
        return extract_session_token(raw)
    if st and st.strip():
        return st.strip()
    raise ValueError("必须提供 st 或 raw（cookies.txt/Cookie头/JSON）之一")


class UpdateTokenRequest(BaseModel):
    st: Optional[str] = None  # 留空时仅更新元数据；提供时必须通过账号身份验证
    project_id: Optional[str] = None  # 用户可选输入project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    image_enabled: Optional[bool] = None
    video_enabled: Optional[bool] = None
    image_concurrency: Optional[int] = None
    video_concurrency: Optional[int] = None


class ProxyConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    media_proxy_enabled: Optional[bool] = None
    media_proxy_url: Optional[str] = None


class ProxyTestRequest(BaseModel):
    proxy_url: str
    test_url: Optional[str] = "https://labs.google/"
    timeout_seconds: Optional[int] = 15


class CaptchaScoreTestRequest(BaseModel):
    website_url: Optional[str] = "https://antcpt.com/score_detector/"
    website_key: Optional[str] = "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf"
    action: Optional[str] = "homepage"
    verify_url: Optional[str] = "https://antcpt.com/score_detector/verify.php"
    enterprise: Optional[bool] = False


class GenerationConfigRequest(BaseModel):
    image_timeout: int
    video_timeout: int


class CallLogicConfigRequest(BaseModel):
    call_mode: str


class ChangePasswordRequest(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str


class UpdateAPIKeyRequest(BaseModel):
    new_api_key: str


class UpdateDebugConfigRequest(BaseModel):
    enabled: bool


class UpdateAdminConfigRequest(BaseModel):
    error_ban_threshold: int


class ST2ATRequest(BaseModel):
    """ST转AT请求"""
    st: str


class ImportTokenItem(BaseModel):
    """导入Token项"""
    email: Optional[str] = None
    access_token: Optional[str] = None
    session_token: Optional[str] = None
    is_active: bool = True
    captcha_proxy_url: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1


class ImportTokensRequest(BaseModel):
    """导入Token请求"""
    tokens: List[ImportTokenItem]


class CreateOnboardingJobRequest(BaseModel):
    """Create a server-side XRDP onboarding job from allowlisted choices."""

    target_token_id: Optional[int] = None
    conflict_policy: Literal["reject", "archive_and_replace"] = "reject"
    requested_business_enabled: bool = False
    requested_keepalive_enabled: bool = False
    requested_runtime_mode: Literal["persistent", "warm"] = "warm"


class UpdateTokenLifecycleRequest(BaseModel):
    """Update one or more keepalive fields without changing business ownership."""

    keepalive_enabled: Optional[bool] = None
    runtime_mode: Optional[Literal["persistent", "warm"]] = None

    @model_validator(mode="after")
    def require_change(self):
        if self.keepalive_enabled is None and self.runtime_mode is None:
            raise ValueError("at least one lifecycle field is required")
        return self


# ========== Auth Middleware ==========

async def verify_admin_token(authorization: str = Header(None)):
    """Verify admin session token (NOT API key)"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")

    token = authorization[7:]

    # Check if token is in active session tokens
    if token not in active_admin_tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")

    return token


def _set_private_response_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _onboarding_job_payload(job: OnboardingJob) -> Dict[str, Any]:
    """Return only UI-safe onboarding metadata, excluding process identity fields."""
    return job.model_dump(
        mode="json",
        exclude={"id", "browser_pid", "browser_start_ticks"},
    )


def _raise_onboarding_http_error(error: Exception) -> None:
    if not isinstance(error, OnboardingServiceError):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "internal_error",
                "message": "Onboarding operation failed safely.",
            },
        ) from None

    status_code = 500
    if error.code in {"job_not_found", "target_not_found"}:
        status_code = 404
    elif error.code in {
        "invalid_job_state",
        "active_job_exists",
        "target_identity_mismatch",
        "profile_identity_mismatch",
        "duplicate_email",
        "login_required",
        "account_inspection_failed",
        "process_ownership_mismatch",
        "destination_conflict",
        "archive_conflict",
        "profile_not_found",
        "unsafe_profile_path",
        "final_validation_failed",
    }:
        status_code = 409
    elif error.code in {"process_launch_failed", "process_identity_unavailable"}:
        status_code = 503

    raise HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": str(error)},
    ) from None


def _require_onboarding_service() -> OnboardingService:
    if onboarding_service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "onboarding_unavailable",
                "message": "Onboarding service is not initialized.",
            },
        )
    return onboarding_service


def _reject_onboarding_deprecated() -> None:
    """Reject all onboarding state-machine routes with 410 Gone.

    The 2810-line ``OnboardingService`` state machine caused a production
    incident (forced re-logins, a destroyed valid session, the wrong XRDP
    Chrome window operated) and is permanently disabled. Routes stay
    registered (not removed) so misdirected clients get 410 instead of a
    404 that would look like a deploy regression. Use
    ``scripts/tokens.py onboard`` instead.
    """
    raise HTTPException(
        status_code=410,
        detail={
            "code": "onboarding_deprecated",
            "message": (
                "onboarding state machine is deprecated; "
                "use 'scripts/tokens.py onboard' instead"
            ),
        },
    )


def _account_lifecycle_payload(account, lifecycle) -> Dict[str, Any]:
    """Build the credential-free account/lifecycle view used by management UI."""
    to_iso = lambda value: value.isoformat() if hasattr(value, "isoformat") else value
    return {
        "id": account.id,
        "email": account.email,
        "name": account.name,
        "remark": account.remark,
        "is_active": account.is_active,
        "ban_reason": account.ban_reason,
        "banned_at": to_iso(account.banned_at) if account.banned_at else None,
        "credits": account.credits,
        "user_paygate_tier": account.user_paygate_tier,
        "membership_confirmed_status": lifecycle.membership_confirmed_status.value,
        "membership_candidate": lifecycle.membership_candidate.value,
        "membership_candidate_count": lifecycle.membership_candidate_count,
        "keepalive_enabled": lifecycle.keepalive_enabled,
        "runtime_mode": lifecycle.runtime_mode,
        "profile_state": lifecycle.profile_state,
        "verified_email": lifecycle.verified_email,
        "last_keepalive_success_at": to_iso(lifecycle.last_keepalive_success_at)
        if lifecycle.last_keepalive_success_at
        else None,
        "last_keepalive_status": lifecycle.last_keepalive_status,
        "next_due_at": to_iso(lifecycle.next_due_at) if lifecycle.next_due_at else None,
        "last_failure_at": to_iso(lifecycle.last_failure_at)
        if lifecycle.last_failure_at
        else None,
        "last_failure_code": lifecycle.last_failure_code,
        "last_observed_tier": lifecycle.last_observed_tier,
        "last_observed_at": to_iso(lifecycle.last_observed_at)
        if lifecycle.last_observed_at
        else None,
        "retired_at": to_iso(lifecycle.retired_at) if lifecycle.retired_at else None,
        "restored_at": to_iso(lifecycle.restored_at) if lifecycle.restored_at else None,
    }


# ========== Auth Endpoints ==========

@router.post("/api/admin/login")
async def admin_login(request: LoginRequest):
    """Admin login - returns session token (NOT API key)"""
    admin_config = await db.get_admin_config()

    if not AuthManager.verify_admin(request.username, request.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Generate independent session token
    session_token = f"admin-{secrets.token_urlsafe(32)}"

    # Store in active tokens
    active_admin_tokens.add(session_token)

    return {
        "success": True,
        "token": session_token,  # Session token (NOT API key)
        "username": admin_config.username
    }


@router.post("/api/admin/logout")
async def admin_logout(token: str = Depends(verify_admin_token)):
    """Admin logout - invalidate session token"""
    active_admin_tokens.discard(token)
    return {"success": True, "message": "退出登录成功"}


@router.post("/api/admin/change-password")
async def change_password(
    request: ChangePasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Change admin password"""
    admin_config = await db.get_admin_config()

    # Verify old password
    if not AuthManager.verify_admin(admin_config.username, request.old_password):
        raise HTTPException(status_code=400, detail="旧密码错误")

    # Update password and username in database
    update_params = {"password": request.new_password}
    if request.username:
        update_params["username"] = request.username

    await db.update_admin_config(**update_params)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    # 🔑 Invalidate all admin session tokens (force re-login for security)
    active_admin_tokens.clear()

    return {"success": True, "message": "密码修改成功,请重新登录"}


# ========== Token Management ==========

@router.get("/api/tokens")
async def get_tokens(token: str = Depends(verify_admin_token)):
    """Get all tokens with statistics"""
    token_rows = await db.get_all_tokens_with_stats()
    to_iso = lambda value: value.isoformat() if hasattr(value, "isoformat") else value

    return [{
        "id": row.get("id"),
        "has_st": bool(row.get("st")),
        "has_at": bool(row.get("at")),
        "at_expires": to_iso(row.get("at_expires")) if row.get("at_expires") else None,
        "email": row.get("email"),
        "name": row.get("name"),
        "remark": row.get("remark"),
        "is_active": bool(row.get("is_active")),
        "ban_reason": row.get("ban_reason"),
        "banned_at": to_iso(row.get("banned_at")) if row.get("banned_at") else None,
        "created_at": to_iso(row.get("created_at")) if row.get("created_at") else None,
        "last_used_at": to_iso(row.get("last_used_at")) if row.get("last_used_at") else None,
        "use_count": row.get("use_count"),
        "credits": row.get("credits"),
        "user_paygate_tier": row.get("user_paygate_tier"),
        "current_project_id": row.get("current_project_id"),
        "current_project_name": row.get("current_project_name"),
        "captcha_proxy_url": row.get("captcha_proxy_url") or "",
        "image_enabled": bool(row.get("image_enabled")),
        "video_enabled": bool(row.get("video_enabled")),
        "image_concurrency": row.get("image_concurrency"),
        "video_concurrency": row.get("video_concurrency"),
        "image_count": row.get("image_count", 0),
        "video_count": row.get("video_count", 0),
        "error_count": row.get("error_count", 0),
        "membership_confirmed_status": row.get("membership_confirmed_status") or "active",
        "membership_candidate": row.get("membership_candidate") or "unknown",
        "membership_candidate_count": row.get("membership_candidate_count") or 0,
        "keepalive_enabled": bool(row.get("keepalive_enabled")),
        "runtime_mode": row.get("runtime_mode") or "warm",
        "profile_state": row.get("profile_state") or "unprovisioned",
        "verified_email": row.get("verified_email"),
        "last_keepalive_success_at": to_iso(row.get("last_keepalive_success_at")) if row.get("last_keepalive_success_at") else None,
        "last_keepalive_status": row.get("last_keepalive_status"),
        "next_due_at": to_iso(row.get("next_due_at")) if row.get("next_due_at") else None,
        "last_failure_at": to_iso(row.get("last_failure_at")) if row.get("last_failure_at") else None,
        "last_failure_code": row.get("last_failure_code"),
        "last_observed_tier": row.get("last_observed_tier"),
        "last_observed_at": to_iso(row.get("last_observed_at")) if row.get("last_observed_at") else None,
        "retired_at": to_iso(row.get("retired_at")) if row.get("retired_at") else None,
        "restored_at": to_iso(row.get("restored_at")) if row.get("restored_at") else None,
    } for row in token_rows]


@router.post("/api/tokens/{token_id}/export")
async def export_token_credentials(
    token_id: int,
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """Explicitly export one account's credentials with cache prevention."""
    _set_private_response_headers(response)
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not initialized")
    account = await db.get_token(token_id)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail="Token not found",
            headers={"Cache-Control": "no-store"},
        )
    return {
        "success": True,
        "token": {
            "id": account.id,
            "email": account.email,
            "st": account.st,
            "at": account.at,
            "at_expires": account.at_expires.isoformat()
            if account.at_expires
            else None,
        },
    }


@router.post("/api/tokens")
async def add_token(
    request: AddTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Add a new token"""
    try:
        # ValueError（缺 st/raw 或抽取失败）由下方 except ValueError 统一返回 400；
        # 不要在此内层 try 里抛 HTTPException —— 会被外层 except Exception 误吞成 500。
        resolved_st = resolve_st_from_request(request.st, request.raw)
        new_token = await token_manager.add_token(
            st=resolved_st,
            project_id=request.project_id,  # 🆕 支持用户指定project_id
            project_name=request.project_name,
            remark=request.remark,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency
        )

        # 热更新并发限制，避免必须重启服务
        if concurrency_manager:
            await concurrency_manager.reset_token(
                new_token.id,
                image_concurrency=new_token.image_concurrency,
                video_concurrency=new_token.video_concurrency
            )

        return {
            "success": True,
            "message": "Token添加成功",
            "token": {
                "id": new_token.id,
                "email": new_token.email,
                "credits": new_token.credits,
                "project_id": new_token.current_project_id,
                "project_name": new_token.current_project_name
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加Token失败: {str(e)}")


@router.put("/api/tokens/{token_id}")
async def update_token(
    token_id: int,
    request: UpdateTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token metadata and optionally replace credentials after verification."""
    try:
        await token_manager.update_token(
            token_id=token_id,
            st=request.st.strip() if request.st and request.st.strip() else None,
            project_id=request.project_id,
            project_name=request.project_name,
            remark=request.remark,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency
        )

        # 热更新并发限制，确保管理台修改立即生效
        if concurrency_manager:
            updated_token = await token_manager.get_token(token_id)
            if updated_token:
                await concurrency_manager.reset_token(
                    token_id,
                    image_concurrency=updated_token.image_concurrency,
                    video_concurrency=updated_token.video_concurrency
                )

        return {"success": True, "message": "Token更新成功"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/tokens/{token_id}")
async def delete_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Delete token"""
    try:
        await token_manager.delete_token(token_id)
        if concurrency_manager:
            await concurrency_manager.remove_token(token_id)
        return {"success": True, "message": "Token删除成功"}
    except TokenDeletionConflictError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": error.code,
                "message": "Token deletion is blocked by an active onboarding job.",
                "job_id": error.job_id,
                "job_state": error.job_state,
            },
        ) from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/enable")
async def enable_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Enable token"""
    await token_manager.enable_token(token_id)
    return {"success": True, "message": "Token已启用"}


@router.post("/api/tokens/{token_id}/disable")
async def disable_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Disable token"""
    await token_manager.disable_token(token_id)
    return {"success": True, "message": "Token已禁用"}


@router.post("/api/tokens/{token_id}/refresh-credits")
async def refresh_credits(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """刷新Token余额 🆕"""
    try:
        credits = await token_manager.refresh_credits(token_id)
        return {
            "success": True,
            "message": "余额刷新成功",
            "credits": credits
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新余额失败: {str(e)}")


@router.post("/api/tokens/{token_id}/refresh-at")
async def refresh_at(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """手动刷新Token的AT (使用ST转换) 🆕
    
    如果 AT 刷新失败且处于 personal 模式，会自动尝试通过浏览器刷新 ST
    """
    from ..core.logger import debug_logger
    from ..core.config import config
    
    debug_logger.log_info(f"[API] 手动刷新 AT 请求: token_id={token_id}, captcha_method={config.captcha_method}")
    
    try:
        # 调用token_manager的内部刷新方法（包含 ST 自动刷新逻辑）
        success = await token_manager._refresh_at(token_id)

        if success:
            # 获取更新后的token信息
            updated_token = await token_manager.get_token(token_id)
            
            message = "AT刷新成功"
            if config.captcha_method == "personal":
                message += "（支持ST自动刷新）"
            
            debug_logger.log_info(f"[API] AT 刷新成功: token_id={token_id}")
            
            return {
                "success": True,
                "message": message,
                "token": {
                    "id": updated_token.id,
                    "email": updated_token.email,
                    "at_expires": updated_token.at_expires.isoformat() if updated_token.at_expires else None
                }
            }
        else:
            debug_logger.log_error(f"[API] AT 刷新失败: token_id={token_id}")
            
            error_detail = "AT刷新失败"
            if config.captcha_method != "personal":
                error_detail += f"（当前打码模式: {config.captcha_method}，ST自动刷新仅在 personal 模式下可用）"
            
            raise HTTPException(status_code=500, detail=error_detail)
    except HTTPException:
        raise
    except Exception as e:
        debug_logger.log_error(f"[API] 刷新AT异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"刷新AT失败: {str(e)}")


@router.post("/api/tokens/st2at")
async def st_to_at(
    request: ST2ATRequest,
    token: str = Depends(verify_admin_token)
):
    """Convert Session Token to Access Token (仅转换,不添加到数据库)"""
    try:
        result = await token_manager.flow_client.st_to_at(request.st)
        return {
            "success": True,
            "message": "ST converted to AT successfully",
            "access_token": result["access_token"],
            "email": result.get("user", {}).get("email"),
            "expires": result.get("expires")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/tokens/import")
async def import_tokens(
    request: ImportTokensRequest,
    token: str = Depends(verify_admin_token)
):
    """批量导入并严格验证每个账号的真实身份与 credits。"""
    added = 0
    updated = 0
    errors = []

    for idx, item in enumerate(request.tokens):
        try:
            st = str(item.session_token or "").strip()
            if not st:
                raise ValueError("缺少 session_token")

            snapshot = await token_manager.inspect_account(st)
            existing = await token_manager.find_token_by_email(snapshot.email)
            common_fields = {
                "captcha_proxy_url": item.captcha_proxy_url.strip() if item.captcha_proxy_url is not None else None,
                "image_enabled": item.image_enabled,
                "video_enabled": item.video_enabled,
                "image_concurrency": item.image_concurrency,
                "video_concurrency": item.video_concurrency,
            }

            if existing:
                await token_manager.update_token(
                    token_id=existing.id,
                    verified_snapshot=snapshot,
                    allow_auth_reactivate=False,
                    **common_fields,
                )
                updated += 1
            else:
                await token_manager.add_token(
                    st=snapshot.st,
                    verified_snapshot=snapshot,
                    is_active=item.is_active,
                    ban_reason=None if item.is_active else TOKEN_REASON_MANUAL_DISABLED,
                    **common_fields,
                )
                added += 1
        except Exception as e:
            errors.append(f"第{idx+1}项: {str(e)}")

    return {
        "success": True,
        "added": added,
        "updated": updated,
        "errors": errors if errors else None,
        "message": f"导入完成: 新增 {added} 个, 更新 {updated} 个" + (f", {len(errors)} 个失败" if errors else "")
    }


# ========== XRDP Onboarding and Account Lifecycle ==========

@router.get("/api/onboarding/config")
async def get_onboarding_config(
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """[DEPRECATED] Onboarding state machine is disabled; always returns 410."""
    _set_private_response_headers(response)
    _reject_onboarding_deprecated()


@router.post("/api/tokens/{token_id}/validate-profile")
async def validate_token_profile(
    token_id: int,
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """Read and verify one retained browser profile without persisting credentials."""
    _set_private_response_headers(response)
    try:
        profile = await _require_onboarding_service().validate_profile(token_id)
    except Exception as error:
        _raise_onboarding_http_error(error)
    return {"success": True, "profile": profile.model_dump(mode="json")}


@router.post("/api/onboarding/jobs")
async def create_onboarding_job(
    request: CreateOnboardingJobRequest,
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """[DEPRECATED] Onboarding state machine is disabled; always returns 410."""
    _set_private_response_headers(response)
    _reject_onboarding_deprecated()


@router.get("/api/onboarding/jobs")
async def list_onboarding_jobs(
    response: Response,
    target_token_id: Optional[int] = None,
    resolved_token_id: Optional[int] = None,
    state: Optional[str] = None,
    phase: Optional[str] = None,
    token: str = Depends(verify_admin_token),
):
    """[DEPRECATED] Onboarding state machine is disabled; always returns 410."""
    _set_private_response_headers(response)
    _reject_onboarding_deprecated()


@router.get("/api/onboarding/jobs/{job_id}")
async def get_onboarding_job(
    job_id: str,
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """[DEPRECATED] Onboarding state machine is disabled; always returns 410."""
    _set_private_response_headers(response)
    _reject_onboarding_deprecated()


@router.post("/api/onboarding/jobs/{job_id}/start")
async def start_onboarding_job(
    job_id: str,
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """[DEPRECATED] Onboarding state machine is disabled; always returns 410."""
    _set_private_response_headers(response)
    _reject_onboarding_deprecated()


@router.post("/api/onboarding/jobs/{job_id}/finalize")
async def finalize_onboarding_job(
    job_id: str,
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """[DEPRECATED] Onboarding state machine is disabled; always returns 410."""
    _set_private_response_headers(response)
    _reject_onboarding_deprecated()


@router.post("/api/onboarding/jobs/{job_id}/cancel")
async def cancel_onboarding_job(
    job_id: str,
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """[DEPRECATED] Onboarding state machine is disabled; always returns 410."""
    _set_private_response_headers(response)
    _reject_onboarding_deprecated()


@router.post("/api/onboarding/recover")
async def recover_onboarding_jobs(
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """[DEPRECATED] Onboarding state machine is disabled; always returns 410."""
    _set_private_response_headers(response)
    _reject_onboarding_deprecated()


@router.put("/api/tokens/{token_id}/lifecycle")
async def update_token_lifecycle(
    token_id: int,
    request: UpdateTokenLifecycleRequest,
    response: Response,
    token: str = Depends(verify_admin_token),
):
    """Update keepalive desired state without enabling or disabling business traffic."""
    _set_private_response_headers(response)
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not initialized")
    account = await db.get_token(token_id)
    lifecycle = await db.get_token_lifecycle(token_id)
    if account is None or lifecycle is None:
        raise HTTPException(status_code=404, detail="Token not found")
    try:
        await db.set_token_desired_state(
            token_id,
            **request.model_dump(exclude_none=True),
        )
        lifecycle = await db.get_token_lifecycle(token_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Token not found") from None
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(status_code=500, detail="Lifecycle update failed safely") from None
    return {
        "success": True,
        "account": _account_lifecycle_payload(account, lifecycle),
    }


# ========== Config Management ==========

@router.get("/api/config/proxy")
async def get_proxy_config(token: str = Depends(verify_admin_token)):
    """Get proxy configuration"""
    config = await proxy_manager.get_proxy_config()
    return {
        "success": True,
        "config": {
            "enabled": config.enabled,
            "proxy_url": config.proxy_url,
            "media_proxy_enabled": config.media_proxy_enabled,
            "media_proxy_url": config.media_proxy_url
        }
    }


@router.get("/api/proxy/config")
async def get_proxy_config_alias(token: str = Depends(verify_admin_token)):
    """Get proxy configuration (alias for frontend compatibility)"""
    config = await proxy_manager.get_proxy_config()
    return {
        "proxy_enabled": config.enabled,  # Frontend expects proxy_enabled
        "proxy_url": config.proxy_url,
        "media_proxy_enabled": config.media_proxy_enabled,
        "media_proxy_url": config.media_proxy_url
    }


@router.post("/api/proxy/config")
async def update_proxy_config_alias(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration (alias for frontend compatibility)"""
    try:
        await proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}


@router.post("/api/config/proxy")
async def update_proxy_config(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration"""
    try:
        await proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}


@router.post("/api/proxy/test")
async def test_proxy_connectivity(
    request: ProxyTestRequest,
    token: str = Depends(verify_admin_token)
):
    """测试代理是否可访问目标站点（默认 https://labs.google/）"""
    proxy_input = (request.proxy_url or "").strip()
    test_url = (request.test_url or "https://labs.google/").strip()
    timeout_seconds = int(request.timeout_seconds or 15)
    timeout_seconds = max(5, min(timeout_seconds, 60))

    if not proxy_input:
        return {
            "success": False,
            "message": "代理地址为空",
            "test_url": test_url
        }

    try:
        proxy_url = proxy_manager.normalize_proxy_url(proxy_input)
    except ValueError as e:
        return {
            "success": False,
            "message": str(e),
            "test_url": test_url
        }

    start_time = time.time()
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession() as session:
            resp = await session.get(
                test_url,
                proxies=proxies,
                timeout=timeout_seconds,
                impersonate="chrome120",
                allow_redirects=True,
                verify=False
            )

        elapsed_ms = int((time.time() - start_time) * 1000)
        status_code = resp.status_code
        final_url = str(resp.url)
        ok = 200 <= status_code < 400

        return {
            "success": ok,
            "message": "代理可用" if ok else f"代理可连通，但目标返回状态码 {status_code}",
            "test_url": test_url,
            "final_url": final_url,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return {
            "success": False,
            "message": f"代理测试失败: {str(e)}",
            "test_url": test_url,
            "elapsed_ms": elapsed_ms
        }


@router.get("/api/config/generation")
async def get_generation_config(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    config = await db.get_generation_config()
    return {
        "success": True,
        "config": {
            "image_timeout": config.image_timeout,
            "video_timeout": config.video_timeout
        }
    }


@router.post("/api/config/generation")
async def update_generation_config(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    await db.update_generation_config(request.image_timeout, request.video_timeout)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "生成配置更新成功"}


@router.get("/api/call-logic/config")
async def get_call_logic_config(token: str = Depends(verify_admin_token)):
    """Get token call logic configuration."""
    config_obj = await db.get_call_logic_config()
    call_mode = getattr(config_obj, "call_mode", None)
    if call_mode not in ("default", "polling"):
        call_mode = "polling" if getattr(config_obj, "polling_mode_enabled", False) else "default"
    return {
        "success": True,
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
        }
    }


@router.post("/api/call-logic/config")
async def update_call_logic_config(
    request: CallLogicConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token call logic configuration."""
    call_mode = request.call_mode if request.call_mode in ("default", "polling") else None
    if call_mode is None:
        raise HTTPException(status_code=400, detail="Invalid call_mode")

    await db.update_call_logic_config(call_mode)
    await db.reload_config_to_memory()

    return {
        "success": True,
        "message": "Token轮询模式保存成功",
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
        }
    }


# ========== System Info ==========

@router.get("/api/system/info")
async def get_system_info(token: str = Depends(verify_admin_token)):
    """Get system information"""
    stats = await db.get_system_info_stats()

    return {
        "success": True,
        "info": {
            "total_tokens": stats["total_tokens"],
            "active_tokens": stats["active_tokens"],
            "total_credits": stats["total_credits"],
            "version": "1.0.0"
        }
    }


# ========== Additional Routes for Frontend Compatibility ==========

@router.post("/api/login")
async def login(request: LoginRequest):
    """Login endpoint (alias for /api/admin/login)"""
    return await admin_login(request)


@router.post("/api/logout")
async def logout(token: str = Depends(verify_admin_token)):
    """Logout endpoint (alias for /api/admin/logout)"""
    return await admin_logout(token)


@router.get("/health")
async def health_check():
    """Public health check endpoint - no auth required"""
    try:
        stats = await db.get_dashboard_stats()
        has_active_tokens = stats.get("active_tokens", 0) > 0
    except Exception:
        return {"backend_running": True, "has_active_tokens": False}
    return {"backend_running": True, "has_active_tokens": has_active_tokens}


@router.get("/api/stats")
async def get_stats(token: str = Depends(verify_admin_token)):
    """Get statistics for dashboard"""
    return await db.get_dashboard_stats()


@router.get("/api/logs")
async def get_logs(
    limit: int = 100,
    token: str = Depends(verify_admin_token)
):
    """Get lightweight request logs for list view"""
    limit = max(1, min(limit, 100))
    logs = await db.get_logs(limit=limit, include_payload=False)

    result = []
    for log in logs:
        raw_status_code = log.get("status_code")
        try:
            status_code = int(raw_status_code) if raw_status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        result.append({
            "id": log.get("id"),
            "token_id": log.get("token_id"),
            "token_email": log.get("token_email"),
            "token_username": log.get("token_username"),
            "operation": log.get("operation"),
            "status_code": status_code if status_code is not None else raw_status_code,
            "duration": log.get("duration"),
            "status_text": log.get("status_text") or "",
            "progress": log.get("progress") or 0,
            "created_at": log.get("created_at"),
            "updated_at": log.get("updated_at"),
            "error_summary": _extract_error_summary(log.get("response_body_excerpt")) if status_code is not None and status_code >= 400 else "",
        })
    return result


@router.get("/api/logs/{log_id}")
async def get_log_detail(
    log_id: int,
    token: str = Depends(verify_admin_token)
):
    """Get single request log detail (payload loaded on demand)"""
    log = await db.get_log_detail(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="日志不存在")

    error_summary = _extract_error_summary(log.get("response_body"))

    return {
        "id": log.get("id"),
        "token_id": log.get("token_id"),
        "token_email": log.get("token_email"),
        "token_username": log.get("token_username"),
        "operation": log.get("operation"),
        "status_code": log.get("status_code"),
        "duration": log.get("duration"),
        "status_text": log.get("status_text") or "",
        "progress": log.get("progress") or 0,
        "created_at": log.get("created_at"),
        "updated_at": log.get("updated_at"),
        "error_summary": error_summary,
        "request_body": log.get("request_body"),
        "response_body": log.get("response_body")
    }


@router.delete("/api/logs")
async def clear_logs(token: str = Depends(verify_admin_token)):
    """Clear all logs"""
    try:
        await db.clear_all_logs()
        return {"success": True, "message": "所有日志已清空"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/admin/config")
async def get_admin_config(token: str = Depends(verify_admin_token)):
    """Get admin configuration"""
    admin_config = await db.get_admin_config()

    return {
        "admin_username": admin_config.username,
        "api_key": admin_config.api_key,
        "error_ban_threshold": admin_config.error_ban_threshold,
        "debug_enabled": config.debug_enabled  # Return actual debug status
    }


@router.post("/api/admin/config")
async def update_admin_config(
    request: UpdateAdminConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin configuration (error_ban_threshold)"""
    # Update error_ban_threshold in database
    await db.update_admin_config(error_ban_threshold=request.error_ban_threshold)

    return {"success": True, "message": "配置更新成功"}


@router.post("/api/admin/password")
async def update_admin_password(
    request: ChangePasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin password"""
    return await change_password(request, token)


@router.post("/api/admin/apikey")
async def update_api_key(
    request: UpdateAPIKeyRequest,
    token: str = Depends(verify_admin_token)
):
    """Update API key (for external API calls, NOT for admin login)"""
    # Update API key in database
    await db.update_admin_config(api_key=request.new_api_key)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "API Key更新成功"}


@router.post("/api/admin/debug")
async def update_debug_config(
    request: UpdateDebugConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update debug configuration"""
    try:
        # Update in-memory config only (not database)
        # This ensures debug mode is automatically disabled on restart
        config.set_debug_enabled(request.enabled)

        status = "enabled" if request.enabled else "disabled"
        return {"success": True, "message": f"Debug mode {status}", "enabled": request.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update debug config: {str(e)}")


@router.get("/api/generation/timeout")
async def get_generation_timeout(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    return await get_generation_config(token)


@router.post("/api/generation/timeout")
async def update_generation_timeout(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    await db.update_generation_config(request.image_timeout, request.video_timeout)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "生成配置更新成功"}


# ========== AT Auto Refresh Config ==========

@router.get("/api/token-refresh/config")
async def get_token_refresh_config(token: str = Depends(verify_admin_token)):
    """Get AT auto refresh configuration (默认启用)"""
    return {
        "success": True,
        "config": {
            "at_auto_refresh_enabled": True  # Flow2API默认启用AT自动刷新
        }
    }


@router.post("/api/token-refresh/enabled")
async def update_token_refresh_enabled(
    token: str = Depends(verify_admin_token)
):
    """Update AT auto refresh enabled (Flow2API固定启用,此接口仅用于前端兼容)"""
    return {
        "success": True,
        "message": "Flow2API的AT自动刷新默认启用且无法关闭"
    }


async def _sync_runtime_cache_config():
    from . import routes
    if routes.generation_handler and routes.generation_handler.file_cache:
        file_cache = routes.generation_handler.file_cache
        file_cache.set_timeout(config.cache_timeout)
        await file_cache.refresh_cleanup_task()

# ========== Cache Configuration Endpoints ==========

@router.get("/api/cache/config")
async def get_cache_config(token: str = Depends(verify_admin_token)):
    """Get cache configuration"""
    cache_config = await db.get_cache_config()

    # Calculate effective base URL
    effective_base_url = cache_config.cache_base_url if cache_config.cache_base_url else f"http://127.0.0.1:8000"

    return {
        "success": True,
        "config": {
            "enabled": cache_config.cache_enabled,
            "timeout": cache_config.cache_timeout,
            "base_url": cache_config.cache_base_url or "",
            "effective_base_url": effective_base_url
        }
    }


@router.post("/api/cache/enabled")
async def update_cache_enabled(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache enabled status"""
    enabled = request.get("enabled", False)
    await db.update_cache_config(enabled=enabled)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": f"缓存已{'启用' if enabled else '禁用'}"}


@router.post("/api/cache/config")
async def update_cache_config_full(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update complete cache configuration"""
    enabled = request.get("enabled")
    timeout = request.get("timeout")
    base_url = request.get("base_url")

    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="缓存超时时间必须为整数")
        if timeout < 0:
            raise HTTPException(status_code=400, detail="缓存超时时间不能小于 0")

    await db.update_cache_config(enabled=enabled, timeout=timeout, base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": "缓存配置更新成功"}


@router.post("/api/cache/base-url")
async def update_cache_base_url(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache base URL"""
    base_url = request.get("base_url", "")
    await db.update_cache_config(base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": "缓存Base URL更新成功"}


@router.post("/api/captcha/config")
async def update_captcha_config(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update captcha configuration"""
    captcha_method = request.get("captcha_method")
    yescaptcha_api_key = request.get("yescaptcha_api_key")
    yescaptcha_base_url = request.get("yescaptcha_base_url")
    capmonster_api_key = request.get("capmonster_api_key")
    capmonster_base_url = request.get("capmonster_base_url")
    ezcaptcha_api_key = request.get("ezcaptcha_api_key")
    ezcaptcha_base_url = request.get("ezcaptcha_base_url")
    capsolver_api_key = request.get("capsolver_api_key")
    capsolver_base_url = request.get("capsolver_base_url")
    remote_browser_base_url = request.get("remote_browser_base_url")
    remote_browser_api_key = request.get("remote_browser_api_key")
    remote_browser_timeout = request.get("remote_browser_timeout", 60)
    browser_proxy_enabled = request.get("browser_proxy_enabled", False)
    browser_proxy_url = request.get("browser_proxy_url", "")
    browser_count = request.get("browser_count", 1)
    personal_project_pool_size = request.get("personal_project_pool_size")
    personal_max_resident_tabs = request.get("personal_max_resident_tabs")
    personal_idle_tab_ttl_seconds = request.get("personal_idle_tab_ttl_seconds")

    # 验证浏览器代理URL格式
    if browser_proxy_enabled and browser_proxy_url:
        is_valid, error_msg = _validate_browser_proxy_url(browser_proxy_url)
        if not is_valid:
            return {"success": False, "message": error_msg}

    if remote_browser_base_url:
        try:
            remote_browser_base_url = _normalize_http_base_url(remote_browser_base_url)
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

    try:
        remote_browser_timeout = max(5, int(remote_browser_timeout or 60))
    except Exception:
        return {"success": False, "message": "远程打码超时时间必须是整数秒"}

    if captcha_method == "browser":
        return {"success": False, "message": "browser (playwright) 模式已禁用，请使用 personal 模式"}

    if captcha_method == "remote_browser":
        if not (remote_browser_base_url or "").strip():
            return {"success": False, "message": "remote_browser 模式需要配置远程打码服务地址"}
        if not (remote_browser_api_key or "").strip():
            return {"success": False, "message": "remote_browser 模式需要配置远程打码服务 API Key"}

    await db.update_captcha_config(
        captcha_method=captcha_method,
        yescaptcha_api_key=yescaptcha_api_key,
        yescaptcha_base_url=yescaptcha_base_url,
        capmonster_api_key=capmonster_api_key,
        capmonster_base_url=capmonster_base_url,
        ezcaptcha_api_key=ezcaptcha_api_key,
        ezcaptcha_base_url=ezcaptcha_base_url,
        capsolver_api_key=capsolver_api_key,
        capsolver_base_url=capsolver_base_url,
        remote_browser_base_url=remote_browser_base_url,
        remote_browser_api_key=remote_browser_api_key,
        remote_browser_timeout=remote_browser_timeout,
        browser_proxy_enabled=browser_proxy_enabled,
        browser_proxy_url=browser_proxy_url if browser_proxy_enabled else None,
        browser_count=max(1, int(browser_count)) if browser_count else 1,
        personal_project_pool_size=personal_project_pool_size,
        personal_max_resident_tabs=personal_max_resident_tabs,
        personal_idle_tab_ttl_seconds=personal_idle_tab_ttl_seconds
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    # browser (playwright) 模式已废弃且 captcha_method=="browser" 在 line 1556 已被拒，
    # 故无需再处理 browser 热重载分支。

    # 如果使用 personal 打码，热重载配置
    if captcha_method == "personal":
        try:
            from ..services.browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            await service.reload_config()
        except Exception as e:
            print(f"[Admin] Personal 配置热更新失败: {e}")

    return {"success": True, "message": "验证码配置更新成功"}


@router.get("/api/captcha/config")
async def get_captcha_config(token: str = Depends(verify_admin_token)):
    """Get captcha configuration"""
    captcha_config = await db.get_captcha_config()
    return {
        "captcha_method": captcha_config.captcha_method,
        "yescaptcha_api_key": captcha_config.yescaptcha_api_key,
        "yescaptcha_base_url": captcha_config.yescaptcha_base_url,
        "capmonster_api_key": captcha_config.capmonster_api_key,
        "capmonster_base_url": captcha_config.capmonster_base_url,
        "ezcaptcha_api_key": captcha_config.ezcaptcha_api_key,
        "ezcaptcha_base_url": captcha_config.ezcaptcha_base_url,
        "capsolver_api_key": captcha_config.capsolver_api_key,
        "capsolver_base_url": captcha_config.capsolver_base_url,
        "remote_browser_base_url": captcha_config.remote_browser_base_url,
        "remote_browser_api_key": captcha_config.remote_browser_api_key,
        "remote_browser_timeout": captcha_config.remote_browser_timeout,
        "browser_proxy_enabled": captcha_config.browser_proxy_enabled,
        "browser_proxy_url": captcha_config.browser_proxy_url or "",
        "browser_count": captcha_config.browser_count,
        "personal_project_pool_size": captcha_config.personal_project_pool_size,
        "personal_max_resident_tabs": captcha_config.personal_max_resident_tabs,
        "personal_idle_tab_ttl_seconds": captcha_config.personal_idle_tab_ttl_seconds
    }


@router.post("/api/captcha/score-test")
async def test_captcha_score(
    _request: Optional[CaptchaScoreTestRequest] = None,
    _token: str = Depends(verify_admin_token)
):
    """分数测试已禁用。"""
    raise HTTPException(status_code=403, detail="已禁用分数测试")


# ========== Plugin Configuration Endpoints ==========

@router.get("/api/plugin/config")
async def get_plugin_config(request: Request, token: str = Depends(verify_admin_token)):
    """Get plugin configuration"""
    plugin_config = await db.get_plugin_config()

    # Preserve the browser-visible scheme, host, port, and root path. Trusted proxy
    # headers are applied to the ASGI request scope before URL generation.
    connection_url = str(request.url_for("plugin_update_token"))

    return {
        "success": True,
        "config": {
            "connection_token": plugin_config.connection_token,
            "connection_url": connection_url,
            "auto_enable_on_update": plugin_config.auto_enable_on_update
        }
    }


@router.post("/api/plugin/config")
async def update_plugin_config(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update plugin configuration"""
    connection_token = request.get("connection_token", "")
    auto_enable_on_update = request.get("auto_enable_on_update", True)  # 默认开启

    # Generate random token if empty
    if not connection_token:
        connection_token = secrets.token_urlsafe(32)

    await db.update_plugin_config(
        connection_token=connection_token,
        auto_enable_on_update=auto_enable_on_update
    )

    return {
        "success": True,
        "message": "插件配置更新成功",
        "connection_token": connection_token,
        "auto_enable_on_update": auto_enable_on_update
    }


@router.post("/api/plugin/update-token")
async def plugin_update_token(request: dict, authorization: Optional[str] = Header(None)):
    """Receive token update from Chrome extension (no admin auth required, uses connection_token)"""
    # Verify connection token
    plugin_config = await db.get_plugin_config()

    # Extract token from Authorization header
    provided_token = None
    if authorization:
        if authorization.startswith("Bearer "):
            provided_token = authorization[7:]
        else:
            provided_token = authorization

    # Check if token matches
    if not plugin_config.connection_token or provided_token != plugin_config.connection_token:
        raise HTTPException(status_code=401, detail="Invalid connection token")

    # Extract session token from request
    session_token = request.get("session_token")

    if not session_token:
        raise HTTPException(status_code=400, detail="Missing session_token")

    try:
        snapshot = await token_manager.inspect_account(str(session_token).strip())
        existing_token = await token_manager.find_token_by_email(snapshot.email)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid session token: {str(e)}")

    if existing_token:
        try:
            was_inactive = not existing_token.is_active
            await token_manager.update_token(
                token_id=existing_token.id,
                verified_snapshot=snapshot,
                allow_auth_reactivate=plugin_config.auto_enable_on_update,
            )
            refreshed = await token_manager.get_token(existing_token.id)
            auto_enabled = bool(was_inactive and refreshed and refreshed.is_active)
            return {
                "success": True,
                "message": (
                    f"Token updated and authentication-recovered for {snapshot.email}"
                    if auto_enabled
                    else f"Token updated for {snapshot.email}"
                ),
                "action": "updated",
                "token_id": existing_token.id,
                "auto_enabled": auto_enabled,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update token: {str(e)}")

    try:
        new_token = await token_manager.add_token(
            st=snapshot.st,
            verified_snapshot=snapshot,
            remark="Added by Chrome Extension",
        )
        return {
            "success": True,
            "message": f"Token added for {new_token.email}",
            "action": "added",
            "token_id": new_token.id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add token: {str(e)}")
