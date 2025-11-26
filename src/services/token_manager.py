"""Token manager for Flow2API with AT auto-refresh"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from ..core.database import Database
from ..core.models import Token, Project
from ..core.logger import debug_logger
from .flow_client import FlowClient
from .proxy_manager import ProxyManager


class TokenManager:
    """Token lifecycle manager with AT auto-refresh"""

    def __init__(self, db: Database, flow_client: FlowClient):
        self.db = db
        self.flow_client = flow_client
        self._lock = asyncio.Lock()

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
        await self.db.delete_token(token_id)

    async def enable_token(self, token_id: int):
        """Enable a token and reset error count"""
        # Enable the token
        await self.db.update_token(token_id, is_active=True)
        # Reset error count when enabling (only reset total error_count, keep today_error_count)
        await self.db.reset_error_count(token_id)

    async def disable_token(self, token_id: int):
        """Disable a token"""
        await self.db.update_token(token_id, is_active=False)

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
        video_concurrency: int = -1
    ) -> Token:
        """Add a new token

        Args:
            st: Session Token (必需)
            project_id: 项目ID (可选,如果提供则直接使用,不创建新项目)
            project_name: 项目名称 (可选,如果不提供则自动生成)
            remark: 备注
            image_enabled: 是否启用图片生成
            video_enabled: 是否启用视频生成
            image_concurrency: 图片并发限制
            video_concurrency: 视频并发限制

        Returns:
            Token object
        """
        # Step 1: 检查ST是否已存在
        existing_token = await self.db.get_token_by_st(st)
        if existing_token:
            raise ValueError(f"Token 已存在（邮箱: {existing_token.email}）")

        # Step 2: 使用ST转换AT
        debug_logger.log_info(f"[ADD_TOKEN] Converting ST to AT...")
        try:
            result = await self.flow_client.st_to_at(st)
            at = result["access_token"]
            expires = result.get("expires")
            user_info = result.get("user", {})
            email = user_info.get("email", "")
            name = user_info.get("name", email.split("@")[0] if email else "")

            # 解析过期时间
            at_expires = None
            if expires:
                try:
                    at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                except:
                    pass

        except Exception as e:
            raise ValueError(f"ST转AT失败: {str(e)}")

        # Step 3: 查询余额
        try:
            credits_result = await self.flow_client.get_credits(at)
            credits = credits_result.get("credits", 0)
            user_paygate_tier = credits_result.get("userPaygateTier")
        except:
            credits = 0
            user_paygate_tier = None

        # Step 4: 处理Project ID和名称
        if project_id:
            # 用户提供了project_id,直接使用
            debug_logger.log_info(f"[ADD_TOKEN] Using provided project_id: {project_id}")
            if not project_name:
                # 如果没有提供project_name,生成一个
                now = datetime.now()
                project_name = now.strftime("%b %d - %H:%M")
        else:
            # 用户没有提供project_id,需要创建新项目
            if not project_name:
                # 自动生成项目名称
                now = datetime.now()
                project_name = now.strftime("%b %d - %H:%M")

            try:
                project_id = await self.flow_client.create_project(st, project_name)
                debug_logger.log_info(f"[ADD_TOKEN] Created new project: {project_name} (ID: {project_id})")
            except Exception as e:
                raise ValueError(f"创建项目失败: {str(e)}")

        # Step 5: 创建Token对象
        token = Token(
            st=st,
            at=at,
            at_expires=at_expires,
            email=email,
            name=name,
            remark=remark,
            is_active=True,
            credits=credits,
            user_paygate_tier=user_paygate_tier,
            current_project_id=project_id,
            current_project_name=project_name,
            image_enabled=image_enabled,
            video_enabled=video_enabled,
            image_concurrency=image_concurrency,
            video_concurrency=video_concurrency
        )

        # Step 6: 保存到数据库
        token_id = await self.db.add_token(token)
        token.id = token_id

        # Step 7: 保存Project到数据库
        project = Project(
            project_id=project_id,
            token_id=token_id,
            project_name=project_name,
            tool_name="PINHOLE"
        )
        await self.db.add_project(project)

        debug_logger.log_info(f"[ADD_TOKEN] Token added successfully (ID: {token_id}, Email: {email})")
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
        video_concurrency: Optional[int] = None
    ):
        """Update token (支持修改project_id和project_name)"""
        update_fields = {}

        if st is not None:
            update_fields["st"] = st
        if at is not None:
            update_fields["at"] = at
        if at_expires is not None:
            update_fields["at_expires"] = at_expires
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

        if update_fields:
            await self.db.update_token(token_id, **update_fields)

    # ========== AT自动刷新逻辑 (核心) ==========

    async def is_at_valid(self, token_id: int) -> bool:
        """检查AT是否有效,如果无效或即将过期则自动刷新

        Returns:
            True if AT is valid or refreshed successfully
            False if AT cannot be refreshed
        """
        token = await self.db.get_token(token_id)
        if not token:
            return False

        # 如果AT不存在,需要刷新
        if not token.at:
            debug_logger.log_info(f"[AT_CHECK] Token {token_id}: AT不存在,需要刷新")
            return await self._refresh_at(token_id)

        # 如果没有过期时间,假设需要刷新
        if not token.at_expires:
            debug_logger.log_info(f"[AT_CHECK] Token {token_id}: AT过期时间未知,尝试刷新")
            return await self._refresh_at(token_id)

        # 检查是否即将过期 (提前1小时刷新)
        now = datetime.now(timezone.utc)
        # 确保at_expires也是timezone-aware
        if token.at_expires.tzinfo is None:
            at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
        else:
            at_expires_aware = token.at_expires

        time_until_expiry = at_expires_aware - now

        if time_until_expiry.total_seconds() < 3600:  # 1 hour (3600 seconds)
            debug_logger.log_info(f"[AT_CHECK] Token {token_id}: AT即将过期 (剩余 {time_until_expiry.total_seconds():.0f} 秒),需要刷新")
            return await self._refresh_at(token_id)

        # AT有效
        return True

    async def _refresh_at(self, token_id: int) -> bool:
        """内部方法: 刷新AT

        Returns:
            True if refresh successful, False otherwise
        """
        async with self._lock:
            token = await self.db.get_token(token_id)
            if not token:
                return False

            try:
                debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: 开始刷新AT...")

                # 使用ST转AT
                result = await self.flow_client.st_to_at(token.st)
                new_at = result["access_token"]
                expires = result.get("expires")

                # 解析过期时间
                new_at_expires = None
                if expires:
                    try:
                        new_at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                    except:
                        pass

                # 更新数据库
                await self.db.update_token(
                    token_id,
                    at=new_at,
                    at_expires=new_at_expires
                )

                debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: AT刷新成功")
                debug_logger.log_info(f"  - 新过期时间: {new_at_expires}")

                # 同时刷新credits
                try:
                    credits_result = await self.flow_client.get_credits(new_at)
                    await self.db.update_token(
                        token_id,
                        credits=credits_result.get("credits", 0)
                    )
                except:
                    pass

                return True

            except Exception as e:
                debug_logger.log_error(f"[AT_REFRESH] Token {token_id}: AT刷新失败 - {str(e)}")
                # 刷新失败,禁用Token
                await self.disable_token(token_id)
                return False

    async def ensure_project_exists(self, token_id: int) -> str:
        """确保Token有可用的Project

        Returns:
            project_id
        """
        token = await self.db.get_token(token_id)
        if not token:
            raise ValueError("Token not found")

        # 如果已有project_id,直接返回
        if token.current_project_id:
            return token.current_project_id

        # 创建新Project
        now = datetime.now()
        project_name = now.strftime("%b %d - %H:%M")

        try:
            project_id = await self.flow_client.create_project(token.st, project_name)
            debug_logger.log_info(f"[PROJECT] Created project for token {token_id}: {project_name}")

            # 更新Token
            await self.db.update_token(
                token_id,
                current_project_id=project_id,
                current_project_name=project_name
            )

            # 保存Project到数据库
            project = Project(
                project_id=project_id,
                token_id=token_id,
                project_name=project_name
            )
            await self.db.add_project(project)

            return project_id

        except Exception as e:
            raise ValueError(f"Failed to create project: {str(e)}")

    # ========== Token使用统计 ==========

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
            await self.disable_token(token_id)

    async def record_success(self, token_id: int):
        """Record successful request (reset consecutive error count)

        This method resets error_count to 0, which is used for auto-disable threshold checking.
        Note: today_error_count and historical statistics are NOT reset.
        """
        await self.db.reset_error_count(token_id)

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
        if not await self.is_at_valid(token_id):
            return 0

        # 重新获取token (AT可能已刷新)
        token = await self.db.get_token(token_id)

        try:
            result = await self.flow_client.get_credits(token.at)
            credits = result.get("credits", 0)

            # 更新数据库
            await self.db.update_token(token_id, credits=credits)

            return credits
        except Exception as e:
            debug_logger.log_error(f"Failed to refresh credits for token {token_id}: {str(e)}")
            return 0
