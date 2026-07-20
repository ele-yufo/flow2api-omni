"""Token manager for Flow2API with AT auto-refresh"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from ..core.database import Database
from ..core.config import config
from ..core.models import Token, Project
from ..core.logger import debug_logger
from ..core.repositories.token_repository import TokenDeletionBlockedError
from ..core.token_states import (
    TOKEN_REASON_429_RATE_LIMIT,
    TOKEN_REASON_CONSECUTIVE_ERRORS,
    TOKEN_REASON_GRANT_EXPIRED,
    TOKEN_REASON_MANUAL_DISABLED,
    TOKEN_REASON_ST_REVOKED,
)
from .tokens.account_identity import (
    AccountIdentityError,
    VerifiedAccountSnapshot,
    inspect_account_identity,
    normalize_account_email,
)
from .tokens.at_refresh import should_refresh_at
from .tokens.locks import get_keyed_lock
from .tokens.project_naming import build_project_name, normalize_project_name_base
from .tokens.project_pool import ensure_project_pool as provision_project_pool
from .flow_client import FlowClient
from .proxy_manager import ProxyManager


class TokenDeletionConflictError(RuntimeError):
    """Service-layer conflict for a token owned by resumable onboarding."""

    code = "onboarding_job_blocks_token_deletion"

    def __init__(self, *, token_id: int, job_id: str, job_state: str):
        self.token_id = token_id
        self.job_id = job_id
        self.job_state = job_state
        super().__init__(
            f"Token {token_id} cannot be deleted while onboarding job "
            f"{job_id} is {job_state}. Complete or cancel the job first."
        )


class TokenManager:
    """Token lifecycle manager with AT auto-refresh"""

    def __init__(self, db: Database, flow_client: FlowClient):
        self.db = db
        self.flow_client = flow_client
        self._refresh_lock_guard = asyncio.Lock()
        self._project_lock_guard = asyncio.Lock()
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._project_locks: dict[int, asyncio.Lock] = {}
        self._refresh_futures: dict[int, asyncio.Task] = {}
        self._pool_low_alerted = False

    async def _get_token_lock(
        self,
        lock_map: dict[int, asyncio.Lock],
        guard: asyncio.Lock,
        token_id: int,
    ) -> asyncio.Lock:
        """委托 tokens.locks。"""
        return await get_keyed_lock(lock_map, guard, token_id)

    def _get_project_pool_size(self) -> int:
        """读取当前生效的单 Token 项目池大小配置。"""
        try:
            return max(1, min(50, int(config.personal_project_pool_size or 4)))
        except Exception:
            return 4

    def _sort_projects(self, projects: List[Project]) -> List[Project]:
        """Sort projects in a stable order for round-robin selection."""
        return sorted(projects, key=lambda project: (project.id or 0, project.project_id))

    def _normalize_project_name_base(self, project_name: Optional[str] = None) -> str:
        """委托 tokens.project_naming。"""
        return normalize_project_name_base(project_name)

    def _build_project_name(self, pool_index: int, base_name: Optional[str] = None) -> str:
        """委托 tokens.project_naming。"""
        return build_project_name(pool_index, base_name)

    async def get_personal_warmup_project_ids(
        self,
        tokens: Optional[List[Token]] = None,
        limit: Optional[int] = None,
    ) -> List[str]:
        """返回 personal 模式启动时建议预热的项目 ID 列表。"""
        token_list = tokens if tokens is not None else await self.get_all_tokens()
        pool_size = self._get_project_pool_size()
        warmup_ids: List[str] = []
        seen_projects: set[str] = set()

        try:
            warmup_limit = None if limit is None else max(1, int(limit))
        except Exception:
            warmup_limit = None

        for token in token_list:
            if not token or not token.is_active:
                continue

            candidate_ids: List[str] = []
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                candidate_ids.append(current_project_id)

            projects = [project for project in await self.db.get_projects_by_token(token.id) if project.is_active]
            for project in self._sort_projects(projects):
                project_id = str(project.project_id or "").strip()
                if project_id and project_id not in candidate_ids:
                    candidate_ids.append(project_id)

            for project_id in candidate_ids[:pool_size]:
                if project_id in seen_projects:
                    continue
                seen_projects.add(project_id)
                warmup_ids.append(project_id)
                if warmup_limit is not None and len(warmup_ids) >= warmup_limit:
                    return warmup_ids

        return warmup_ids

    async def _create_project_for_token(self, token: Token, pool_index: int, base_name: Optional[str] = None) -> Project:
        """Create a new pooled project for a token and persist it."""
        project_name = self._build_project_name(pool_index, base_name)
        project_id = await self.flow_client.create_project(token.st, project_name)
        debug_logger.log_info(
            f"[PROJECT] Created pooled project for token {token.id}: {project_name} ({project_id})"
        )
        project = Project(
            project_id=project_id,
            token_id=token.id,
            project_name=project_name,
        )
        project.id = await self.db.add_project(project)
        return project

    def _select_next_project(self, token: Token, projects: List[Project]) -> Project:
        """Select the next project from the pool in round-robin order."""
        ordered_projects = self._sort_projects(projects)
        if not ordered_projects:
            raise ValueError("No available projects for token")

        if len(ordered_projects) == 1:
            return ordered_projects[0]

        if token.current_project_id:
            for index, project in enumerate(ordered_projects):
                if project.project_id == token.current_project_id:
                    return ordered_projects[(index + 1) % len(ordered_projects)]

        return ordered_projects[0]

    # ========== Token CRUD ==========

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        return await self.db.get_all_tokens()

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        return await self.db.get_active_tokens()

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        return await self.db.get_token(token_id)

    async def delete_token(self, token_id: int):
        """Delete token"""
        token = await self.db.get_token(token_id)
        project_ids: List[str] = []
        if token:
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                project_ids.append(current_project_id)

        for project in await self.db.get_projects_by_token(token_id):
            project_id = str(project.project_id or "").strip()
            if project_id and project_id not in project_ids:
                project_ids.append(project_id)

        try:
            await self.db.delete_token(token_id)
        except TokenDeletionBlockedError as error:
            raise TokenDeletionConflictError(
                token_id=error.token_id,
                job_id=error.job_id,
                job_state=error.job_state,
            ) from error

        refresh_task = self._refresh_futures.pop(token_id, None)
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._refresh_locks.pop(token_id, None)
        self._project_locks.pop(token_id, None)

        if config.captcha_method == "personal" and project_ids:
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                for project_id in project_ids:
                    await service.stop_resident_mode(project_id)
            except Exception as e:
                debug_logger.log_warning(f"[DELETE_TOKEN] 清理 personal 浏览器状态失败: {e}")

    async def enable_token(self, token_id: int):
        """Explicitly enable a token and clear its prior business-disable reason."""
        await self.db.update_token(token_id, is_active=True)
        await self.db.clear_token_ban(token_id)
        await self.db.reset_error_count(token_id)
        await self._check_pool_low()

    async def disable_token(
        self,
        token_id: int,
        reason: str = TOKEN_REASON_MANUAL_DISABLED,
    ):
        """Disable a token with an explicit owner for the business-pool state."""
        await self.db.update_token(
            token_id,
            is_active=False,
            ban_reason=reason,
            banned_at=datetime.now(timezone.utc),
        )
        await self._check_pool_low()

    async def finalize_onboarding_account_state(
        self,
        token_id: int,
        *,
        keepalive_enabled: bool,
        runtime_mode: str,
        enable_business_if_pending: bool,
        completed_at: Optional[datetime] = None,
    ) -> None:
        """Atomically publish onboarding state without clearing externally owned bans."""
        await self.db.finalize_onboarding_account_state(
            token_id,
            keepalive_enabled=keepalive_enabled,
            runtime_mode=runtime_mode,
            enable_business_if_pending=enable_business_if_pending,
            completed_at=completed_at,
        )
        await self._check_pool_low()

    async def _check_pool_low(self) -> None:
        """检测可用账号数是否跌至阈值，带去重地触发"账号池告急"告警。
        活跃数回升至阈值之上时复位去重标志，恢复后可再次告警。"""
        try:
            from ..core.config import config
            active = await self.db.get_active_tokens()
            threshold = config.alert_pool_low_threshold
            if len(active) <= threshold and not self._pool_low_alerted:
                self._pool_low_alerted = True
                await self._alert(
                    title="账号池告急",
                    description=f"可用账号仅剩 {len(active)} 个（阈值 {threshold}），请尽快补充新 Pro 账号。",
                    fields=[("可用账号数", str(len(active)), True), ("阈值", str(threshold), True)],
                    severity="critical",
                )
            elif len(active) > threshold:
                self._pool_low_alerted = False
        except Exception as e:
            debug_logger.log_warning(f"[ALERT] 池告急检测异常被忽略: {e}")

    async def inspect_account(self, st: str) -> VerifiedAccountSnapshot:
        """Resolve real account identity, credentials, credits, and tier from Google."""
        return await inspect_account_identity(self.flow_client, st)

    async def _find_token_by_normalized_email(self, email: str) -> Optional[Token]:
        normalized = normalize_account_email(email)
        matches = [
            token
            for token in await self.db.get_all_tokens()
            if normalize_account_email(token.email) == normalized
        ]
        if len(matches) > 1:
            raise ValueError(f"账号邮箱存在重复记录: {email}")
        return matches[0] if matches else None

    async def find_token_by_email(self, email: str) -> Optional[Token]:
        """Find one exact normalized account or fail on ambiguous legacy rows."""
        return await self._find_token_by_normalized_email(email)

    # ========== Token添加 (支持Project创建) ==========

    async def add_token(
        self,
        st: str,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: bool = True,
        video_enabled: bool = True,
        image_concurrency: int = -1,
        video_concurrency: int = -1,
        captcha_proxy_url: Optional[str] = None,
        is_active: bool = True,
        ban_reason: Optional[str] = None,
        verified_snapshot: Optional[VerifiedAccountSnapshot] = None,
    ) -> Token:
        """Add a new token and prepare its pooled projects."""
        existing_token = await self.db.get_token_by_st(st)
        if existing_token:
            raise ValueError(f"Session Token 已属于账号: {existing_token.email}")

        debug_logger.log_info("[ADD_TOKEN] Validating account identity and credits...")
        try:
            snapshot = verified_snapshot or await self.inspect_account(st)
        except AccountIdentityError as exc:
            raise ValueError(f"账号验证失败: {exc}") from exc

        existing_email = await self._find_token_by_normalized_email(snapshot.email)
        if existing_email:
            raise ValueError(f"账号邮箱已存在: {existing_email.email}")
        rotated_owner = await self.db.get_token_by_st(snapshot.st)
        if rotated_owner:
            raise ValueError(f"轮换后的 Session Token 已属于账号: {rotated_owner.email}")

        base_project_name = self._normalize_project_name_base(project_name)
        project_pool_size = self._get_project_pool_size()
        pooled_projects: List[Project] = []

        if project_id:
            first_project_name = self._build_project_name(1, base_project_name)
            debug_logger.log_info(f"[ADD_TOKEN] Using provided project_id as pooled project #1: {project_id}")
            pooled_projects.append(Project(
                project_id=project_id,
                token_id=0,
                project_name=first_project_name,
                tool_name="PINHOLE"
            ))
        else:
            try:
                first_project_name = self._build_project_name(1, base_project_name)
                first_project_id = await self.flow_client.create_project(snapshot.st, first_project_name)
                debug_logger.log_info(f"[ADD_TOKEN] Created pooled project #1: {first_project_name} (ID: {first_project_id})")
                pooled_projects.append(Project(
                    project_id=first_project_id,
                    token_id=0,
                    project_name=first_project_name,
                    tool_name="PINHOLE"
                ))
            except Exception as e:
                raise ValueError(f"??????: {str(e)}")

        token = Token(
            st=snapshot.st,
            at=snapshot.at,
            at_expires=snapshot.at_expires,
            email=snapshot.email,
            name=snapshot.name,
            remark=remark,
            is_active=is_active,
            credits=snapshot.credits,
            user_paygate_tier=snapshot.user_paygate_tier,
            current_project_id=pooled_projects[0].project_id,
            current_project_name=pooled_projects[0].project_name,
            image_enabled=image_enabled,
            video_enabled=video_enabled,
            image_concurrency=image_concurrency,
            video_concurrency=video_concurrency,
            captcha_proxy_url=captcha_proxy_url,
            ban_reason=ban_reason,
            banned_at=datetime.now(timezone.utc) if ban_reason else None,
        )

        token_id = await self.db.add_token(token)
        token.id = token_id

        pooled_projects[0].token_id = token_id
        pooled_projects[0].id = await self.db.add_project(pooled_projects[0])

        while len(pooled_projects) < project_pool_size:
            new_project = await self._create_project_for_token(token, len(pooled_projects) + 1, base_project_name)
            pooled_projects.append(new_project)

        debug_logger.log_info(
            f"[ADD_TOKEN] Token added successfully (ID: {token_id}, Email: {snapshot.email}, pooled_projects={len(pooled_projects)})"
        )
        # 新增账号后活跃数可能回升至阈值之上，复位池告急去重标志
        await self._check_pool_low()
        return token
    async def update_token(
        self,
        token_id: int,
        st: Optional[str] = None,
        at: Optional[str] = None,
        at_expires: Optional[datetime] = None,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: Optional[bool] = None,
        video_enabled: Optional[bool] = None,
        image_concurrency: Optional[int] = None,
        video_concurrency: Optional[int] = None,
        captcha_proxy_url: Optional[str] = None,
        verified_snapshot: Optional[VerifiedAccountSnapshot] = None,
        allow_auth_reactivate: bool = True,
    ):
        """Update metadata and apply credential changes only after identity verification."""
        if not await self.db.get_token(token_id):
            raise ValueError("Token not found")

        snapshot = verified_snapshot
        if st is not None and snapshot is None:
            snapshot = await self.inspect_account(st)
        if snapshot is not None:
            await self.db.apply_verified_account_snapshot(
                token_id,
                snapshot,
                allow_auth_reactivate=allow_auth_reactivate,
            )
        elif at is not None or at_expires is not None:
            raise ValueError("AT fields require an identity-verified account snapshot")

        update_fields = {}
        if project_id is not None:
            update_fields["current_project_id"] = project_id
        if project_name is not None:
            update_fields["current_project_name"] = project_name
        if remark is not None:
            update_fields["remark"] = remark
        if image_enabled is not None:
            update_fields["image_enabled"] = image_enabled
        if video_enabled is not None:
            update_fields["video_enabled"] = video_enabled
        if image_concurrency is not None:
            update_fields["image_concurrency"] = image_concurrency
        if video_concurrency is not None:
            update_fields["video_concurrency"] = video_concurrency
        if captcha_proxy_url is not None:
            update_fields["captcha_proxy_url"] = captcha_proxy_url

        if update_fields:
            await self.db.update_token(token_id, **update_fields)

    # ========== AT自动刷新逻辑 (核心) ==========

    def _should_refresh_at(self, token: Token) -> bool:
        """委托 tokens.at_refresh。"""
        return should_refresh_at(token)

    def needs_at_refresh(self, token: Optional[Token]) -> bool:
        """供调度层快速判断当前 token 是否大概率会触发 AT 刷新。"""
        if not token:
            return True
        return self._should_refresh_at(token)

    async def ensure_valid_token(self, token: Optional[Token]) -> Optional[Token]:
        """确保 token 的 AT 可用，并在必要时返回刷新后的最新对象。"""
        if not token:
            return None

        if not self._should_refresh_at(token):
            return token

        if not await self._refresh_at(token.id):
            return None

        return await self.db.get_token(token.id)

    async def is_at_valid(self, token_id: int, token: Optional[Token] = None) -> bool:
        """检查AT是否有效,如果无效或即将过期则自动刷新

        Returns:
            True if AT is valid or refreshed successfully
            False if AT cannot be refreshed
        """
        token_obj = token if token and token.id == token_id else await self.db.get_token(token_id)
        if not token_obj:
            return False

        valid_token = await self.ensure_valid_token(token_obj)
        return valid_token is not None


    async def _refresh_at_inner(self, token_id: int) -> bool:
        """Perform exactly one real AT refresh attempt."""
        refresh_lock = await self._get_token_lock(
            self._refresh_locks,
            self._refresh_lock_guard,
            token_id,
        )
        async with refresh_lock:
            token = await self.db.get_token(token_id)
            if not token:
                return False
            was_revoked_before = token.ban_reason == TOKEN_REASON_ST_REVOKED

            result = await self._do_refresh_at(token_id, token.st)
            if result:
                return True

            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: first AT refresh failed, trying ST refresh...")
            new_st = await self._try_refresh_st(token_id, token)
            if new_st:
                debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: ST refreshed, retrying AT refresh...")
                result = await self._do_refresh_at(token_id, new_st)
                if result:
                    return True

            # 仅在"本次确认 ST 被撤销"时禁用+告警；瞬时网络错误保留账号待重试，
            # 避免一次抖动就把健康账号下线（与每日保活保守策略一致）。
            await self._handle_refresh_failure(token_id, was_revoked_before)
            return False

    async def _handle_refresh_failure(self, token_id: int, was_revoked_before: bool) -> None:
        """刷新失败后的统一处置（on-use 与每日保活共用）：
        仅在"本次新确认 ST_REVOKED"时禁用+告警；否则视为瞬时错误，保留账号待下次重试。
        was_revoked_before 用于排除历史遗留的 ST_REVOKED 造成的误杀。"""
        refreshed = await self.db.get_token(token_id)
        newly_revoked = (
            refreshed is not None
            and refreshed.ban_reason == TOKEN_REASON_ST_REVOKED
            and not was_revoked_before
        )
        if newly_revoked:
            debug_logger.log_error(f"[AT_REFRESH] Token {token_id}: ST 已被撤销，禁用并告警")
            email = refreshed.email if refreshed else str(token_id)
            await self._alert(
                title="账号失效需重登",
                description=f"账号 {email} 的 Session Token 已被 Google 撤销/失效，需人工重登。",
                fields=[("账号", email, True), ("Token ID", str(token_id), True),
                        ("建议操作", "登录 labs.google/fx/tools/flow，在后台「添加账号」粘贴该号 cookies.txt", False)],
                severity="critical",
            )
            await self.disable_token(token_id, reason=TOKEN_REASON_ST_REVOKED)
        else:
            debug_logger.log_warning(f"[AT_REFRESH] Token {token_id}: 刷新失败(疑似瞬时)，保留账号待重试")

    async def _refresh_at(self, token_id: int) -> bool:
        """Coalesce concurrent AT refresh calls for the same token."""
        existing_task = self._refresh_futures.get(token_id)
        if existing_task:
            return await existing_task

        async def runner() -> bool:
            try:
                return await self._refresh_at_inner(token_id)
            finally:
                current = self._refresh_futures.get(token_id)
                if current is task:
                    self._refresh_futures.pop(token_id, None)

        task = asyncio.create_task(runner())
        self._refresh_futures[token_id] = task
        return await task

    async def _alert_if_credit_crossed(
        self,
        email: str,
        previous_credits: Optional[int],
        new_credits: int,
    ) -> None:
        floor = config.min_credits_to_select
        if previous_credits is None or previous_credits <= floor or new_credits > floor:
            return
        try:
            await self._alert(
                title="单账号额度耗尽",
                description=f"账号 {email} 剩余额度已降至 {new_credits}（阈值 {floor}），将不再被调度。",
                fields=[
                    ("账号", email, True),
                    ("剩余额度", str(new_credits), True),
                    ("建议操作", "为该账号充值或更换新号", False),
                ],
                severity="warning",
            )
        except Exception as alert_err:
            debug_logger.log_warning(f"[ALERT] 额度耗尽告警异常被忽略: {alert_err}")

    async def _do_refresh_at(self, token_id: int, st: str) -> bool:
        """Refresh and persist ST/AT only after identity and credits verification."""
        before = await self.db.get_token(token_id)
        if before is None:
            return False
        previous_credits = before.credits
        account_email = before.email
        try:
            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: 开始严格账号验证...")
            snapshot = await self.inspect_account(st)
            await self.db.apply_verified_account_snapshot(token_id, snapshot)
        except AccountIdentityError as exc:
            if exc.code == "session_rejected":
                await self.db.update_token(token_id, ban_reason=TOKEN_REASON_ST_REVOKED)
            elif exc.code == "grant_expired":
                await self.db.update_token(token_id, ban_reason=TOKEN_REASON_GRANT_EXPIRED)
            debug_logger.log_error(
                f"[AT_REFRESH] Token {token_id}: 验证失败 ({exc.code}) - {exc}"
            )
            return False
        except (KeyError, ValueError) as exc:
            debug_logger.log_error(f"[AT_REFRESH] Token {token_id}: 身份写入被拒绝 - {exc}")
            return False
        except Exception as exc:
            debug_logger.log_error(
                f"[AT_REFRESH] Token {token_id}: 刷新异常 - {type(exc).__name__}: {exc}"
            )
            return False

        if snapshot.st != st:
            debug_logger.log_info(f"[ST_ROTATE] Token {token_id}: ST 已滚动续期并原子写入")
        await self._alert_if_credit_crossed(
            account_email,
            previous_credits,
            snapshot.credits,
        )
        debug_logger.log_info(
            f"[AT_REFRESH] Token {token_id}: 身份及 AT 验证成功（余额: {snapshot.credits}）"
        )
        return True

    async def _try_refresh_st(self, token_id: int, token) -> Optional[str]:
        """尝试通过浏览器刷新 Session Token

        使用常驻 tab 获取新的 __Secure-next-auth.session-token

        Args:
            token_id: Token ID
            token: Token 对象

        Returns:
            新的 ST 字符串，如果失败返回 None
        """
        from ..core.config import config

        if not config.st_browser_refresh_enabled:
            debug_logger.log_info(
                f"[ST_REFRESH] Token {token_id}: 浏览器 ST 刷新已禁用 "
                f"(st_browser_refresh_enabled=false，多账号下会写错号)，跳过"
            )
            return None

        try:
            # 仅在 personal 模式下支持 ST 自动刷新
            if config.captcha_method != "personal":
                debug_logger.log_info(f"[ST_REFRESH] 非 personal 模式，跳过 ST 自动刷新")
                return None

            if not token.current_project_id:
                debug_logger.log_warning(f"[ST_REFRESH] Token {token_id} 没有 project_id，无法刷新 ST")
                return None

            debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: 尝试通过浏览器刷新 ST...")

            from .browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(self.db)

            refresh_timeout_seconds = 45.0
            try:
                new_st = await asyncio.wait_for(
                    service.refresh_session_token(token.current_project_id),
                    timeout=refresh_timeout_seconds,
                )
            except asyncio.TimeoutError:
                debug_logger.log_error(
                    f"[ST_REFRESH] Token {token_id}: 刷新 ST 超时 ({refresh_timeout_seconds:.0f}s)"
                )
                return None
            if new_st and new_st != token.st:
                try:
                    snapshot = await self.inspect_account(new_st)
                    await self.db.apply_verified_account_snapshot(token_id, snapshot)
                except (AccountIdentityError, KeyError, ValueError) as exc:
                    debug_logger.log_error(
                        f"[ST_REFRESH] Token {token_id}: 新 ST 身份验证失败 - {exc}"
                    )
                    return None
                debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: ST 已验证并原子更新")
                return snapshot.st
            elif new_st == token.st:
                debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: 获取到的 ST 与原 ST 相同，可能登录已失效")
                return None
            else:
                debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: 无法获取新 ST")
                return None

        except Exception as e:
            debug_logger.log_error(f"[ST_REFRESH] Token {token_id}: 刷新 ST 失败 - {str(e)}")
            return None

    async def _alert(self, title: str, description: str, fields=None, severity: str = "warning") -> None:
        """构造 AlertNotifier 投递告警；任何异常都不外抛，绝不影响主流程。"""
        try:
            from ..core.config import config
            from .alert_notifier import AlertNotifier
            await AlertNotifier(config.alert_webhook_url).send_alert(title, description, fields, severity)
        except Exception as e:
            debug_logger.log_warning(f"[ALERT] 发送异常被忽略: {e}")

    async def keepalive_rotate_st(self, token_id: int) -> bool:
        """每日保活：滚动 ST 续期。直接走 _do_refresh_at（它会捕获并回写 rotated ST、
        并仅在确认 401 时标记 ST_REVOKED），**不经过会“失败即 disable”的 _refresh_at_inner**。

        只有当刷新失败且确认是 ST 被撤销 (ban_reason=ST_REVOKED) 时才 disable+告警；
        瞬时网络错误保留账号，待下一轮重试。Returns True 表示该号仍健康。
        """
        token = await self.db.get_token(token_id)
        if not token or not token.is_active:
            return False
        # 快照刷新前的 ban_reason，交给共用的失败处置逻辑判定是否"本次新确认撤销"。
        was_revoked_before = token.ban_reason == TOKEN_REASON_ST_REVOKED
        ok = await self._do_refresh_at(token_id, token.st)
        if ok:
            return True
        await self._handle_refresh_failure(token_id, was_revoked_before)
        return False

    async def keepalive_sweep(self, inter_delay: float = 2.0) -> None:
        """对所有活跃 token 跑一次保活续期；单个号失败不影响其余。

        被后台保活任务调用。除续命外，也会顺带发现"已死但还挂着 active"的号
        （如 Google 令牌已失效、闲置从未被用时刷新过）→ _do_refresh_at 命中 401 →
        标记 ST_REVOKED → 禁用 → 发告警。

        inter_delay：账号之间间隔（秒），避免一串请求集中打 Google 触发风控；
        测试可传 0 跳过等待。
        """
        tokens = await self.db.get_active_tokens()
        debug_logger.log_info(f"[ST_KEEPALIVE] 开始保活扫描，活跃账号 {len(tokens)} 个")
        for idx, token in enumerate(tokens):
            try:
                await self.keepalive_rotate_st(token.id)
            except Exception as e:
                debug_logger.log_warning(f"[ST_KEEPALIVE] Token {token.id} 保活失败: {e}")
            if inter_delay > 0 and idx < len(tokens) - 1:
                await asyncio.sleep(inter_delay)

    async def ensure_project_pool(
        self,
        token_id: int,
        base_name: Optional[str] = None,
    ) -> List[Project]:
        """Idempotently provision the configured pool without rotating its pointer."""
        project_lock = await self._get_token_lock(
            self._project_locks,
            self._project_lock_guard,
            token_id,
        )
        async with project_lock:
            token = await self.db.get_token(token_id)
            if not token:
                raise ValueError("Token not found")
            return await provision_project_pool(
                self.db,
                self.flow_client,
                token,
                self._get_project_pool_size(),
                base_name=base_name,
            )

    async def ensure_project_exists(self, token_id: int) -> str:
        """Ensure the project pool exists and select its next project."""
        project_lock = await self._get_token_lock(
            self._project_locks,
            self._project_lock_guard,
            token_id,
        )
        async with project_lock:
            token = await self.db.get_token(token_id)
            if not token:
                raise ValueError("Token not found")
            try:
                projects = await provision_project_pool(
                    self.db,
                    self.flow_client,
                    token,
                    self._get_project_pool_size(),
                )
                selected_project = self._select_next_project(token, projects)
                await self.db.update_token(
                    token_id,
                    current_project_id=selected_project.project_id,
                    current_project_name=selected_project.project_name,
                )
                return selected_project.project_id
            except Exception as e:
                raise ValueError(f"Failed to prepare project pool: {str(e)}") from e

    async def record_usage(self, token_id: int, is_video: bool = False):
        """Record token usage"""
        await self.db.update_token(token_id, use_count=1, last_used_at=datetime.now())

        if is_video:
            await self.db.increment_token_stats(token_id, "video")
        else:
            await self.db.increment_token_stats(token_id, "image")

    async def record_error(self, token_id: int):
        """Record token error and auto-disable if threshold reached"""
        await self.db.increment_token_stats(token_id, "error")

        # Check if should auto-disable token (based on consecutive errors)
        stats = await self.db.get_token_stats(token_id)
        admin_config = await self.db.get_admin_config()

        if stats and stats.consecutive_error_count >= admin_config.error_ban_threshold:
            debug_logger.log_warning(
                f"[TOKEN_BAN] Token {token_id} consecutive error count ({stats.consecutive_error_count}) "
                f"reached threshold ({admin_config.error_ban_threshold}), auto-disabling"
            )
            await self.disable_token(token_id, reason=TOKEN_REASON_CONSECUTIVE_ERRORS)

    async def record_success(self, token_id: int):
        """Record successful request (reset consecutive error count)

        This method resets error_count to 0, which is used for auto-disable threshold checking.
        Note: today_error_count and historical statistics are NOT reset.
        """
        await self.db.reset_error_count(token_id)

    async def ban_token_for_429(self, token_id: int):
        """因429错误立即禁用token

        Args:
            token_id: Token ID
        """
        debug_logger.log_warning(f"[429_BAN] 禁用Token {token_id} (原因: 429 Rate Limit)")
        await self.db.update_token(
            token_id,
            is_active=False,
            ban_reason=TOKEN_REASON_429_RATE_LIMIT,
            banned_at=datetime.now(timezone.utc)
        )
        # 429 也会让可用账号变少：触发池告急检测（任何禁用都要查）
        await self._check_pool_low()

    async def auto_unban_429_tokens(self):
        """自动解禁因429被禁用的token

        规则:
        - 距离禁用时间12小时后自动解禁
        - 仅解禁未过期的token
        - 仅解禁因429被禁用的token
        """
        all_tokens = await self.db.get_all_tokens()
        now = datetime.now(timezone.utc)

        for token in all_tokens:
            # 跳过非429禁用的token
            if token.ban_reason != TOKEN_REASON_429_RATE_LIMIT:
                continue

            # 跳过未禁用的token
            if token.is_active:
                continue

            # 跳过没有禁用时间的token
            if not token.banned_at:
                continue

            # 检查token是否已过期
            if token.at_expires:
                # 确保时区一致
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires

                # 如果已过期，跳过
                if at_expires_aware <= now:
                    debug_logger.log_info(f"[AUTO_UNBAN] Token {token.id} 已过期，跳过解禁")
                    continue

            # 确保banned_at时区一致
            if token.banned_at.tzinfo is None:
                banned_at_aware = token.banned_at.replace(tzinfo=timezone.utc)
            else:
                banned_at_aware = token.banned_at

            # 检查是否已过12小时
            time_since_ban = now - banned_at_aware
            if time_since_ban.total_seconds() >= 12 * 3600:  # 12小时
                debug_logger.log_info(
                    f"[AUTO_UNBAN] 解禁Token {token.id} (禁用时间: {banned_at_aware}, "
                    f"已过 {time_since_ban.total_seconds() / 3600:.1f} 小时)"
                )
                await self.db.update_token(token.id, is_active=True)
                await self.db.clear_token_ban(token.id)
                # 重置错误计数
                await self.db.reset_error_count(token.id)
                # 池恢复后复位池告急标志（解禁绕过了 enable_token）
                await self._check_pool_low()

    # ========== 余额刷新 ==========

    async def refresh_credits(self, token_id: int) -> int:
        """刷新Token余额

        Returns:
            credits
        """
        token = await self.db.get_token(token_id)
        if not token:
            return 0

        # 确保AT有效
        token = await self.ensure_valid_token(token)
        if not token:
            return 0

        try:
            result = await self.flow_client.get_credits(token.at)
            credits = result.get("credits", 0)
            user_paygate_tier = result.get("userPaygateTier")

            # 更新数据库
            await self.db.update_token(
                token_id,
                credits=credits,
                user_paygate_tier=user_paygate_tier,
            )

            return credits
        except Exception as e:
            debug_logger.log_error(f"Failed to refresh credits for token {token_id}: {str(e)}")
            return 0
