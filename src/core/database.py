"""Database storage layer for Flow2API"""
import aiosqlite
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
from ..shared.db import SqliteEngine
from .schema_defaults import ensure_config_rows
from .repositories.token_stats_repository import TokenStatsRepository
from .repositories.request_log_repository import RequestLogRepository
from .repositories.project_repository import ProjectRepository
from .repositories.task_repository import TaskRepository
from .repositories.config_repository import ConfigRepository
from .repositories.onboarding_job_repository import OnboardingJobRepository
from .repositories.token_lifecycle_repository import TokenLifecycleRepository
from .repositories.token_repository import TokenRepository
from .models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, GenerationConfig, CacheConfig, Project, CaptchaConfig, PluginConfig, CallLogicConfig, OnboardingJob
from .token_states import AccountLifecycleState


class Database(SqliteEngine):
    """SQLite database manager (flow2api schema on top of the shared SqliteEngine).

    Connection/lock/pragma/schema-probe plumbing lives in SqliteEngine; this class
    owns the flow2api tables, migrations, and CRUD.
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            # Store database in data directory
            data_dir = Path(__file__).parent.parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "flow.db")
        super().__init__(db_path)
        # 按实体拆分的仓储(共享本引擎的连接层),Database 逐步收敛为组合根。
        self._token_stats = TokenStatsRepository(self)
        self._request_logs = RequestLogRepository(self)
        self._projects = ProjectRepository(self)
        self._tasks = TaskRepository(self)
        self._config = ConfigRepository(self)
        self._token_lifecycle = TokenLifecycleRepository(self)
        self._onboarding_jobs = OnboardingJobRepository(self)
        self._tokens = TokenRepository(self, self._token_lifecycle)

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """委托 core.schema_defaults。"""
        await ensure_config_rows(db, config_dict)

    async def _create_onboarding_jobs_table(self, db, table_name: str):
        """Create the approved credential-free onboarding schema."""
        if table_name not in ("onboarding_jobs", "onboarding_jobs_migrating"):
            raise ValueError(f"unsupported onboarding table name: {table_name}")
        await db.execute(f"""
            CREATE TABLE {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                target_token_id INTEGER,
                resolved_token_id INTEGER,
                phase TEXT NOT NULL DEFAULT 'created',
                state TEXT NOT NULL DEFAULT 'pending',
                browser_pid INTEGER,
                browser_start_ticks INTEGER,
                discovered_email TEXT,
                discovered_tier TEXT,
                discovered_credits INTEGER,
                discovered_at_expires TIMESTAMP,
                project_count INTEGER,
                profile_ready BOOLEAN,
                conflict_status TEXT,
                conflict_policy TEXT NOT NULL DEFAULT 'reject',
                requested_business_enabled BOOLEAN NOT NULL DEFAULT 0,
                requested_keepalive_enabled BOOLEAN NOT NULL DEFAULT 0,
                requested_runtime_mode TEXT NOT NULL DEFAULT 'warm'
                    CHECK (requested_runtime_mode IN ('persistent', 'warm')),
                error_code TEXT,
                error_message TEXT,
                expires_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                cancelled_at TIMESTAMP,
                FOREIGN KEY (target_token_id) REFERENCES tokens(id) ON DELETE SET NULL,
                FOREIGN KEY (resolved_token_id) REFERENCES tokens(id) ON DELETE SET NULL
            )
        """)

    async def _rebuild_unsafe_onboarding_jobs(self, db, columns):
        """Remove provisional unsafe fields while preserving approved metadata."""
        await db.execute("DROP TABLE IF EXISTS onboarding_jobs_migrating")
        await self._create_onboarding_jobs_table(db, "onboarding_jobs_migrating")

        def source(column, fallback="NULL"):
            return column if column in columns else fallback

        target_source = source("target_token_id", source("token_id"))
        discovered_email_source = source("discovered_email", source("verified_email"))
        select_values = [
            source("id"),
            source("job_id"),
            target_source,
            source("resolved_token_id"),
            source("phase", "'created'"),
            source("state", "'pending'"),
            source("browser_pid"),
            source("browser_start_ticks"),
            discovered_email_source,
            source("discovered_tier"),
            source("discovered_credits"),
            source("discovered_at_expires"),
            source("project_count"),
            source("profile_ready"),
            source("conflict_status"),
            source("conflict_policy", "'reject'"),
            source("requested_business_enabled", "0"),
            source("requested_keepalive_enabled", "0"),
            source("requested_runtime_mode", "'warm'"),
            source("error_code"),
            source("error_message"),
            source("expires_at"),
            source("created_at", "CURRENT_TIMESTAMP"),
            source("updated_at", "CURRENT_TIMESTAMP"),
            source("started_at"),
            source("completed_at"),
            source("cancelled_at"),
        ]
        target_columns = (
            "id, job_id, target_token_id, resolved_token_id, phase, state, "
            "browser_pid, browser_start_ticks, discovered_email, discovered_tier, "
            "discovered_credits, discovered_at_expires, project_count, profile_ready, "
            "conflict_status, conflict_policy, requested_business_enabled, "
            "requested_keepalive_enabled, "
            "requested_runtime_mode, error_code, error_message, expires_at, "
            "created_at, updated_at, started_at, completed_at, cancelled_at"
        )
        await db.execute(
            f"INSERT INTO onboarding_jobs_migrating ({target_columns}) "
            f"SELECT {', '.join(select_values)} FROM onboarding_jobs"
        )
        await db.execute("DROP TABLE onboarding_jobs")
        await db.execute(
            "ALTER TABLE onboarding_jobs_migrating RENAME TO onboarding_jobs"
        )

    async def _ensure_onboarding_jobs_table(self, db):
        """Create or migrate onboarding jobs without retaining unsafe columns."""
        if not await self._table_exists(db, "onboarding_jobs"):
            await self._create_onboarding_jobs_table(db, "onboarding_jobs")
            return

        cursor = await db.execute("PRAGMA table_info(onboarding_jobs)")
        columns = {row[1] for row in await cursor.fetchall()}
        forbidden = {
            "st", "at", "cookie", "cookies", "access_token", "session_token",
            "token_id", "profile_path", "path", "command",
        }
        if columns & forbidden:
            await self._rebuild_unsafe_onboarding_jobs(db, columns)
            cursor = await db.execute("PRAGMA table_info(onboarding_jobs)")
            columns = {row[1] for row in await cursor.fetchall()}

        columns_to_add = [
            ("target_token_id", "INTEGER REFERENCES tokens(id) ON DELETE SET NULL"),
            ("resolved_token_id", "INTEGER REFERENCES tokens(id) ON DELETE SET NULL"),
            ("phase", "TEXT NOT NULL DEFAULT 'created'"),
            ("state", "TEXT NOT NULL DEFAULT 'pending'"),
            ("browser_pid", "INTEGER"),
            ("browser_start_ticks", "INTEGER"),
            ("discovered_email", "TEXT"),
            ("discovered_tier", "TEXT"),
            ("discovered_credits", "INTEGER"),
            ("discovered_at_expires", "TIMESTAMP"),
            ("project_count", "INTEGER"),
            ("profile_ready", "BOOLEAN"),
            ("conflict_status", "TEXT"),
            ("conflict_policy", "TEXT NOT NULL DEFAULT 'reject'"),
            ("requested_business_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
            ("requested_keepalive_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
            ("requested_runtime_mode", "TEXT NOT NULL DEFAULT 'warm'"),
            ("error_code", "TEXT"),
            ("error_message", "TEXT"),
            ("expires_at", "TIMESTAMP"),
            ("created_at", "TIMESTAMP"),
            ("updated_at", "TIMESTAMP"),
            ("started_at", "TIMESTAMP"),
            ("completed_at", "TIMESTAMP"),
            ("cancelled_at", "TIMESTAMP"),
        ]
        for column_name, column_type in columns_to_add:
            if column_name not in columns:
                await db.execute(
                    f"ALTER TABLE onboarding_jobs ADD COLUMN {column_name} {column_type}"
                )

    async def _ensure_lifecycle_tables(self, db):
        """Create and additively migrate lifecycle/onboarding persistence."""
        await db.execute("""
            CREATE TABLE IF NOT EXISTS token_lifecycle (
                token_id INTEGER PRIMARY KEY,
                membership_confirmed_status TEXT NOT NULL DEFAULT 'active'
                    CHECK (membership_confirmed_status IN ('active', 'retired')),
                membership_candidate TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (membership_candidate IN ('unknown', 'free', 'paid')),
                membership_candidate_count INTEGER NOT NULL DEFAULT 0
                    CHECK (membership_candidate_count IN (0, 1)),
                keepalive_enabled BOOLEAN NOT NULL DEFAULT 0,
                runtime_mode TEXT NOT NULL DEFAULT 'warm'
                    CHECK (runtime_mode IN ('persistent', 'warm')),
                profile_state TEXT NOT NULL DEFAULT 'unprovisioned',
                verified_email TEXT,
                last_keepalive_at TIMESTAMP,
                last_keepalive_success_at TIMESTAMP,
                last_keepalive_status TEXT,
                last_keepalive_error TEXT,
                keepalive_failure_count INTEGER NOT NULL DEFAULT 0,
                next_due_at TIMESTAMP,
                last_failure_at TIMESTAMP,
                last_failure_code TEXT,
                last_failure_detail TEXT,
                last_observed_tier TEXT,
                last_observed_at TIMESTAMP,
                retired_at TIMESTAMP,
                restored_at TIMESTAMP,
                last_alert_code TEXT,
                last_alert_at TIMESTAMP,
                alert_episode INTEGER NOT NULL DEFAULT 0,
                alerted BOOLEAN NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (token_id) REFERENCES tokens(id) ON DELETE CASCADE
            )
        """)
        lifecycle_columns = [
            ("next_due_at", "TIMESTAMP"),
            ("last_failure_at", "TIMESTAMP"),
            ("last_failure_code", "TEXT"),
            ("last_failure_detail", "TEXT"),
            ("last_observed_tier", "TEXT"),
            ("last_observed_at", "TIMESTAMP"),
            ("retired_at", "TIMESTAMP"),
            ("restored_at", "TIMESTAMP"),
            ("last_alert_code", "TEXT"),
            ("last_alert_at", "TIMESTAMP"),
            ("alert_episode", "INTEGER NOT NULL DEFAULT 0"),
            ("alerted", "BOOLEAN NOT NULL DEFAULT 0"),
        ]
        for column_name, column_type in lifecycle_columns:
            if not await self._column_exists(db, "token_lifecycle", column_name):
                await db.execute(
                    f"ALTER TABLE token_lifecycle ADD COLUMN {column_name} {column_type}"
                )

        await self._ensure_onboarding_jobs_table(db)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_lifecycle_keepalive_enabled "
            "ON token_lifecycle(keepalive_enabled, token_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_onboarding_jobs_target_created_at "
            "ON onboarding_jobs(target_token_id, created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_onboarding_jobs_resolved_created_at "
            "ON onboarding_jobs(resolved_token_id, created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_onboarding_jobs_state_created_at "
            "ON onboarding_jobs(state, created_at DESC)"
        )

    async def check_and_migrate_db(self, config_dict: dict = None):
        """Check database integrity and perform migrations if needed

        This method is called during upgrade mode to:
        1. Create missing tables (if they don't exist)
        2. Add missing columns to existing tables
        3. Ensure all config tables have default rows

        Args:
            config_dict: Configuration dictionary from setting.toml (optional)
                        Used only to initialize missing config rows with default values.
                        Existing config rows will NOT be overwritten.
        """
        async with self._connect(write=True) as db:
            print("Checking database integrity and performing migrations...")
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")

            # ========== Step 1: Create missing tables ==========
            # Check and create cache_config table if missing
            if not await self._table_exists(db, "cache_config"):
                print("  ✓ Creating missing table: cache_config")
                await db.execute("""
                    CREATE TABLE cache_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        cache_enabled BOOLEAN DEFAULT 0,
                        cache_timeout INTEGER DEFAULT 7200,
                        cache_base_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create proxy_config table if missing
            if not await self._table_exists(db, "proxy_config"):
                print("  ✓ Creating missing table: proxy_config")
                await db.execute("""
                    CREATE TABLE proxy_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        enabled BOOLEAN DEFAULT 0,
                        proxy_url TEXT,
                        media_proxy_enabled BOOLEAN DEFAULT 0,
                        media_proxy_url TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create call_logic_config table if missing
            if not await self._table_exists(db, "call_logic_config"):
                print("  Creating missing table: call_logic_config")
                await db.execute("""
                    CREATE TABLE call_logic_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        call_mode TEXT DEFAULT 'default',
                        polling_mode_enabled BOOLEAN DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create captcha_config table if missing
            if not await self._table_exists(db, "captcha_config"):
                print("  ✓ Creating missing table: captcha_config")
                await db.execute("""
                    CREATE TABLE captcha_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        captcha_method TEXT DEFAULT 'personal',
                        yescaptcha_api_key TEXT DEFAULT '',
                        yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                        capmonster_api_key TEXT DEFAULT '',
                        capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                        ezcaptcha_api_key TEXT DEFAULT '',
                        ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                        capsolver_api_key TEXT DEFAULT '',
                        capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                        remote_browser_base_url TEXT DEFAULT '',
                        remote_browser_api_key TEXT DEFAULT '',
                        remote_browser_timeout INTEGER DEFAULT 60,
                        website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                        page_action TEXT DEFAULT 'IMAGE_GENERATION',
                        browser_proxy_enabled BOOLEAN DEFAULT 0,
                        browser_proxy_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create plugin_config table if missing
            if not await self._table_exists(db, "plugin_config"):
                print("  ✓ Creating missing table: plugin_config")
                await db.execute("""
                    CREATE TABLE plugin_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        connection_token TEXT DEFAULT '',
                        auto_enable_on_update BOOLEAN DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            await self._ensure_lifecycle_tables(db)

            # ========== Step 2: Add missing columns to existing tables ==========
            # Check and add missing columns to tokens table
            if await self._table_exists(db, "tokens"):
                columns_to_add = [
                    ("at", "TEXT"),  # Access Token
                    ("at_expires", "TIMESTAMP"),  # AT expiration time
                    ("credits", "INTEGER DEFAULT 0"),  # Balance
                    ("user_paygate_tier", "TEXT"),  # User tier
                    ("current_project_id", "TEXT"),  # Current project UUID
                    ("current_project_name", "TEXT"),  # Project name
                    ("image_enabled", "BOOLEAN DEFAULT 1"),
                    ("video_enabled", "BOOLEAN DEFAULT 1"),
                    ("image_concurrency", "INTEGER DEFAULT -1"),
                    ("video_concurrency", "INTEGER DEFAULT -1"),
                    ("captcha_proxy_url", "TEXT"),  # token级打码代理
                    ("ban_reason", "TEXT"),  # 禁用原因
                    ("banned_at", "TIMESTAMP"),  # 禁用时间
                ]

                for col_name, col_type in columns_to_add:
                    if not await self._column_exists(db, "tokens", col_name):
                        try:
                            await db.execute(f"ALTER TABLE tokens ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to tokens table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to admin_config table
            if await self._table_exists(db, "admin_config"):
                if not await self._column_exists(db, "admin_config", "error_ban_threshold"):
                    try:
                        await db.execute("ALTER TABLE admin_config ADD COLUMN error_ban_threshold INTEGER DEFAULT 3")
                        print("  ✓ Added column 'error_ban_threshold' to admin_config table")
                    except Exception as e:
                        print(f"  ✗ Failed to add column 'error_ban_threshold': {e}")

            # Check and add missing columns to proxy_config table
            if await self._table_exists(db, "proxy_config"):
                proxy_columns_to_add = [
                    ("media_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("media_proxy_url", "TEXT"),
                ]

                for col_name, col_type in proxy_columns_to_add:
                    if not await self._column_exists(db, "proxy_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE proxy_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to proxy_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("browser_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("browser_proxy_url", "TEXT"),
                    ("capmonster_api_key", "TEXT DEFAULT ''"),
                    ("capmonster_base_url", "TEXT DEFAULT 'https://api.capmonster.cloud'"),
                    ("ezcaptcha_api_key", "TEXT DEFAULT ''"),
                    ("ezcaptcha_base_url", "TEXT DEFAULT 'https://api.ez-captcha.com'"),
                    ("capsolver_api_key", "TEXT DEFAULT ''"),
                    ("capsolver_base_url", "TEXT DEFAULT 'https://api.capsolver.com'"),
                    ("browser_count", "INTEGER DEFAULT 1"),
                    ("remote_browser_base_url", "TEXT DEFAULT ''"),
                    ("remote_browser_api_key", "TEXT DEFAULT ''"),
                    ("remote_browser_timeout", "INTEGER DEFAULT 60"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to token_stats table
            if await self._table_exists(db, "token_stats"):
                stats_columns_to_add = [
                    ("today_image_count", "INTEGER DEFAULT 0"),
                    ("today_video_count", "INTEGER DEFAULT 0"),
                    ("today_error_count", "INTEGER DEFAULT 0"),
                    ("today_date", "DATE"),
                    ("consecutive_error_count", "INTEGER DEFAULT 0"),  # 🆕 连续错误计数
                ]

                for col_name, col_type in stats_columns_to_add:
                    if not await self._column_exists(db, "token_stats", col_name):
                        try:
                            await db.execute(f"ALTER TABLE token_stats ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to token_stats table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to plugin_config table
            if await self._table_exists(db, "plugin_config"):
                plugin_columns_to_add = [
                    ("auto_enable_on_update", "BOOLEAN DEFAULT 1"),  # 默认开启
                ]

                for col_name, col_type in plugin_columns_to_add:
                    if not await self._column_exists(db, "plugin_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE plugin_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to plugin_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("personal_project_pool_size", "INTEGER DEFAULT 4"),
                    ("personal_max_resident_tabs", "INTEGER DEFAULT 5"),
                    ("personal_idle_tab_ttl_seconds", "INTEGER DEFAULT 600"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            legacy_ids = (config_dict or {}).get("keepalive", {}).get("browser_token_ids")
            await self._token_lifecycle.backfill_legacy(db, legacy_ids)

            # ========== Step 3: Ensure all config tables have default rows ==========
            # Note: This will NOT overwrite existing config rows
            # It only ensures missing rows are created with default values from setting.toml
            await self._ensure_config_rows(db, config_dict=config_dict)

            await db.commit()
            print("Database migration check completed.")

    async def init_db(self):
        """Initialize database tables"""
        async with self._connect(write=True) as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            # Tokens table (Flow2API版本)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    st TEXT UNIQUE NOT NULL,
                    at TEXT,
                    at_expires TIMESTAMP,
                    email TEXT NOT NULL,
                    name TEXT,
                    remark TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    credits INTEGER DEFAULT 0,
                    user_paygate_tier TEXT,
                    current_project_id TEXT,
                    current_project_name TEXT,
                    image_enabled BOOLEAN DEFAULT 1,
                    video_enabled BOOLEAN DEFAULT 1,
                    image_concurrency INTEGER DEFAULT -1,
                    video_concurrency INTEGER DEFAULT -1,
                    captcha_proxy_url TEXT,
                    ban_reason TEXT,
                    banned_at TIMESTAMP
                )
            """)

            await self._ensure_lifecycle_tables(db)

            # Projects table (新增)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    project_name TEXT NOT NULL,
                    tool_name TEXT DEFAULT 'PINHOLE',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Token stats table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Tasks table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    progress INTEGER DEFAULT 0,
                    result_urls TEXT,
                    error_message TEXT,
                    scene_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Request logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    status_text TEXT DEFAULT '',
                    progress INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Admin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT DEFAULT 'admin',
                    password TEXT DEFAULT 'admin',
                    api_key TEXT DEFAULT 'han1234',
                    error_ban_threshold INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Proxy config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proxy_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    proxy_url TEXT,
                    media_proxy_enabled BOOLEAN DEFAULT 0,
                    media_proxy_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Generation config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS generation_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    image_timeout INTEGER DEFAULT 300,
                    video_timeout INTEGER DEFAULT 1500,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Call logic config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS call_logic_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    call_mode TEXT DEFAULT 'default',
                    polling_mode_enabled BOOLEAN DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Cache config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    cache_enabled BOOLEAN DEFAULT 0,
                    cache_timeout INTEGER DEFAULT 7200,
                    cache_base_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Debug config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS debug_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    log_requests BOOLEAN DEFAULT 1,
                    log_responses BOOLEAN DEFAULT 1,
                    mask_token BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Captcha config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS captcha_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    captcha_method TEXT DEFAULT 'personal',
                    yescaptcha_api_key TEXT DEFAULT '',
                    yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                    capmonster_api_key TEXT DEFAULT '',
                    capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                    ezcaptcha_api_key TEXT DEFAULT '',
                    ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                    capsolver_api_key TEXT DEFAULT '',
                    capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                    remote_browser_base_url TEXT DEFAULT '',
                    remote_browser_api_key TEXT DEFAULT '',
                    remote_browser_timeout INTEGER DEFAULT 60,
                    website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                    page_action TEXT DEFAULT 'IMAGE_GENERATION',

                    browser_proxy_enabled BOOLEAN DEFAULT 0,
                    browser_proxy_url TEXT,
                    browser_count INTEGER DEFAULT 1,
                    personal_project_pool_size INTEGER DEFAULT 4,
                    personal_max_resident_tabs INTEGER DEFAULT 5,
                    personal_idle_tab_ttl_seconds INTEGER DEFAULT 600,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Plugin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plugin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    connection_token TEXT DEFAULT '',
                    auto_enable_on_update BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON tasks(task_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_st ON tokens(st)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_project_id ON projects(project_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_email ON tokens(email)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_is_active_last_used_at ON tokens(is_active, last_used_at)")

            # Migrate request_logs table if needed
            await self._migrate_request_logs(db)

            # Request logs query indexes (列表按 created_at 排序 / token 过滤)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_token_id_created_at ON request_logs(token_id, created_at DESC)")

            # Token stats lookup index
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_stats_token_id ON token_stats(token_id)")

            await db.commit()

    async def _migrate_request_logs(self, db):
        """Migrate request_logs table from old schema to new schema"""
        try:
            has_model = await self._column_exists(db, "request_logs", "model")
            has_operation = await self._column_exists(db, "request_logs", "operation")

            if has_model and not has_operation:
                print("?? ?????request_logs???,????...")
                await db.execute("ALTER TABLE request_logs RENAME TO request_logs_old")
                await db.execute("""
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id INTEGER,
                        operation TEXT NOT NULL,
                        request_body TEXT,
                        response_body TEXT,
                        status_code INTEGER NOT NULL,
                        duration FLOAT NOT NULL,
                        status_text TEXT DEFAULT '',
                        progress INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (token_id) REFERENCES tokens(id)
                    )
                """)
                await db.execute("""
                    INSERT INTO request_logs (token_id, operation, request_body, status_code, duration, status_text, progress, created_at, updated_at)
                    SELECT
                        token_id,
                        model as operation,
                        json_object('model', model, 'prompt', substr(prompt, 1, 100)) as request_body,
                        CASE
                            WHEN status = 'completed' THEN 200
                            WHEN status = 'failed' THEN 500
                            ELSE 102
                        END as status_code,
                        response_time as duration,
                        CASE
                            WHEN status = 'completed' THEN 'completed'
                            WHEN status = 'failed' THEN 'failed'
                            ELSE 'processing'
                        END as status_text,
                        CASE
                            WHEN status = 'completed' THEN 100
                            WHEN status = 'failed' THEN 0
                            ELSE 0
                        END as progress,
                        created_at,
                        created_at
                    FROM request_logs_old
                """)
                await db.execute("DROP TABLE request_logs_old")
                print("? request_logs?????")

            if not await self._column_exists(db, "request_logs", "status_text"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN status_text TEXT DEFAULT ''")
            if not await self._column_exists(db, "request_logs", "progress"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN progress INTEGER DEFAULT 0")
            if not await self._column_exists(db, "request_logs", "updated_at"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN updated_at TIMESTAMP")
            await db.execute("UPDATE request_logs SET updated_at = created_at WHERE updated_at IS NULL")
        except Exception as e:
            print(f"?? request_logs?????: {e}")
            # Continue even if migration fails

    # Token operations
    async def add_token(self, token):
        """委托 TokenRepository。"""
        return await self._tokens.add_token(token=token)

    async def get_token(self, token_id):
        """委托 TokenRepository。"""
        return await self._tokens.get_token(token_id=token_id)

    async def get_token_by_st(self, st):
        """委托 TokenRepository。"""
        return await self._tokens.get_token_by_st(st=st)

    async def get_token_by_email(self, email):
        """委托 TokenRepository。"""
        return await self._tokens.get_token_by_email(email=email)

    async def get_all_tokens(self):
        """委托 TokenRepository。"""
        return await self._tokens.get_all_tokens()

    async def get_all_tokens_with_stats(self):
        """委托 TokenRepository。"""
        return await self._tokens.get_all_tokens_with_stats()

    async def get_dashboard_stats(self):
        """委托 TokenRepository。"""
        return await self._tokens.get_dashboard_stats()

    async def get_system_info_stats(self):
        """委托 TokenRepository。"""
        return await self._tokens.get_system_info_stats()

    async def get_active_tokens(self):
        """委托 TokenRepository。"""
        return await self._tokens.get_active_tokens()

    async def update_token(self, token_id, **kwargs):
        """委托 TokenRepository。"""
        await self._tokens.update_token(token_id=token_id, **kwargs)

    async def clear_token_ban(self, token_id):
        """委托 TokenRepository。"""
        await self._tokens.clear_token_ban(token_id=token_id)

    async def delete_token(self, token_id):
        """委托 TokenRepository。"""
        await self._tokens.delete_token(token_id=token_id)

    # Token lifecycle and keepalive operations
    async def get_token_lifecycle(self, token_id: int):
        """Return lifecycle persistence for one token."""
        return await self._token_lifecycle.get(token_id)

    async def apply_verified_account_snapshot(
        self,
        token_id: int,
        snapshot,
        *,
        observed_at: datetime = None,
        next_due_at: datetime = None,
        allow_auth_reactivate: bool = True,
    ):
        """Atomically apply identity-verified credentials and lifecycle policy."""
        return await self._token_lifecycle.apply_verified_snapshot(
            token_id,
            snapshot,
            observed_at=observed_at,
            next_due_at=next_due_at,
            allow_auth_reactivate=allow_auth_reactivate,
        )

    async def list_enabled_token_lifecycles(self):
        """List lifecycle rows whose keepalive flag is enabled."""
        return await self._token_lifecycle.list_enabled()

    async def list_keepalive_enabled_tokens(self):
        """Return enabled token+lifecycle joins, including business-disabled tokens."""
        return await self._token_lifecycle.list_enabled_tokens()

    async def set_token_desired_state(
        self,
        token_id: int,
        keepalive_enabled: bool = None,
        runtime_mode: str = None,
        profile_state: str = None,
    ):
        """Set desired keepalive/runtime state independently of token is_active."""
        await self._token_lifecycle.set_desired_state(
            token_id,
            keepalive_enabled=keepalive_enabled,
            runtime_mode=runtime_mode,
            profile_state=profile_state,
        )

    async def finalize_onboarding_account_state(
        self,
        token_id: int,
        *,
        keepalive_enabled: bool,
        runtime_mode: str,
        enable_business_if_pending: bool,
        completed_at: datetime = None,
    ):
        """Atomically publish onboarding lifecycle state and its owned business ban."""
        await self._token_lifecycle.finalize_onboarding_state(
            token_id,
            keepalive_enabled=keepalive_enabled,
            runtime_mode=runtime_mode,
            enable_business_if_pending=enable_business_if_pending,
            completed_at=completed_at,
        )

    async def set_token_verified_email(self, token_id: int, verified_email: str = None):
        """Set or explicitly clear the verified browser-profile email."""
        await self._token_lifecycle.set_verified_email(token_id, verified_email)

    async def update_token_membership_state(
        self,
        token_id: int,
        state: AccountLifecycleState,
        *,
        observed_tier: str = None,
        observed_at: datetime = None,
    ):
        """Persist canonical membership state and observation telemetry."""
        await self._token_lifecycle.update_membership_state(
            token_id,
            state,
            observed_tier=observed_tier,
            observed_at=observed_at,
        )

    async def update_token_lifecycle_alert(
        self,
        token_id: int,
        *,
        alert_code: str = None,
        alerted_at: datetime = None,
    ):
        """Set or explicitly clear the latest lifecycle alert marker."""
        await self._token_lifecycle.update_alert(
            token_id,
            alert_code=alert_code,
            alerted_at=alerted_at,
        )

    async def update_token_alert_state(
        self,
        token_id: int,
        *,
        alert_code: str = None,
        episode: int,
        alerted: bool,
        alerted_at: datetime = None,
    ):
        """Persist complete alert episode state for restart-safe deduplication."""
        await self._token_lifecycle.update_alert_state(
            token_id,
            alert_code=alert_code,
            episode=episode,
            alerted=alerted,
            alerted_at=alerted_at,
        )

    async def update_token_keepalive_telemetry(
        self,
        token_id: int,
        *,
        status: str,
        error: str = None,
        error_code: str = None,
        attempted_at: datetime = None,
        next_due_at: datetime = None,
    ):
        """Persist keepalive outcome, failure detail, and optional next due time."""
        kwargs = {
            "status": status,
            "error": error,
            "error_code": error_code,
            "attempted_at": attempted_at,
        }
        if next_due_at is not None:
            kwargs["next_due_at"] = next_due_at
        await self._token_lifecycle.update_keepalive_telemetry(token_id, **kwargs)

    async def clear_token_keepalive_error(self, token_id: int):
        """Explicitly clear a token's keepalive telemetry error."""
        await self._token_lifecycle.clear_keepalive_error(token_id)

    # Onboarding job operations
    async def create_onboarding_job(self, job: OnboardingJob = None, **kwargs):
        """Create a safe onboarding job with no ST, AT, or cookie fields."""
        if job is not None and kwargs:
            raise TypeError("pass either an OnboardingJob or keyword fields, not both")
        record = job if job is not None else OnboardingJob(**kwargs)
        return await self._onboarding_jobs.create(record)

    async def claim_onboarding_job(self, job_id: str) -> bool:
        """Atomically claim the singleton onboarding browser slot."""
        return await self._onboarding_jobs.claim_start(job_id)

    async def claim_failed_onboarding_job_resume(
        self,
        job_id: str,
        *,
        expected_phase: str,
        expected_error_code: str = None,
        expected_pid: int = None,
        expected_start_ticks: int = None,
        expected_expires_at: datetime = None,
        refreshed_expires_at: datetime,
    ) -> bool:
        """Atomically reclaim one failed job and refresh its resume deadline."""
        return await self._onboarding_jobs.claim_failed_resume(
            job_id,
            expected_phase=expected_phase,
            expected_error_code=expected_error_code,
            expected_pid=expected_pid,
            expected_start_ticks=expected_start_ticks,
            expected_expires_at=expected_expires_at,
            refreshed_expires_at=refreshed_expires_at,
        )

    async def get_onboarding_job(self, job_id):
        """Get an onboarding job by public or integer ID."""
        return await self._onboarding_jobs.get(job_id)

    async def list_onboarding_jobs(
        self,
        *,
        target_token_id: int = None,
        resolved_token_id: int = None,
        state: str = None,
        phase: str = None,
    ):
        """List onboarding jobs with approved resumability filters."""
        return await self._onboarding_jobs.list(
            target_token_id=target_token_id,
            resolved_token_id=resolved_token_id,
            state=state,
            phase=phase,
        )

    async def update_onboarding_job(self, job_id, **fields):
        """Update allowlisted resumable onboarding metadata."""
        await self._onboarding_jobs.update(job_id, **fields)

    async def replace_onboarding_browser_identity(
        self,
        job_id,
        *,
        expected_pid,
        expected_start_ticks,
        browser_pid,
        browser_start_ticks,
    ) -> bool:
        """Compare-and-swap replace one complete onboarding browser identity."""
        return await self._onboarding_jobs.replace_browser_identity(
            job_id,
            expected_pid=expected_pid,
            expected_start_ticks=expected_start_ticks,
            browser_pid=browser_pid,
            browser_start_ticks=browser_start_ticks,
        )

    async def clear_onboarding_browser_identity(
        self,
        job_id,
        *,
        expected_pid,
        expected_start_ticks,
    ) -> bool:
        """Compare-and-swap clear one onboarding browser process generation."""
        return await self._onboarding_jobs.clear_browser_identity(
            job_id,
            expected_pid=expected_pid,
            expected_start_ticks=expected_start_ticks,
        )

    async def transition_onboarding_job_state(
        self,
        job_id,
        *,
        expected_state: str,
        expected_phase: str,
        state: str,
        phase: str,
        clear_error: bool = False,
    ) -> bool:
        """Conditionally transition one unchanged onboarding job state and phase."""
        return await self._onboarding_jobs.update_state(
            job_id,
            state,
            phase=phase,
            clear_error=clear_error,
            expected_state=expected_state,
            expected_phase=expected_phase,
        )

    async def update_onboarding_job_state(
        self,
        job_id,
        state: str,
        *,
        phase: str = None,
        error_code: str = None,
        error_message: str = None,
        clear_error: bool = False,
    ):
        """Update onboarding workflow state, phase, timestamps, and safe errors."""
        kwargs = {"phase": phase, "clear_error": clear_error}
        if error_code is not None:
            kwargs["error_code"] = error_code
        if error_message is not None:
            kwargs["error_message"] = error_message
        await self._onboarding_jobs.update_state(job_id, state, **kwargs)

    async def delete_onboarding_job(self, job_id):
        """Delete an onboarding job."""
        await self._onboarding_jobs.delete(job_id)

    # Project operations
    async def add_project(self, project: Project) -> int:
        """委托 ProjectRepository。"""
        return await self._projects.add_project(project)

    async def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """委托 ProjectRepository。"""
        return await self._projects.get_project_by_id(project_id)

    async def get_projects_by_token(self, token_id: int) -> List[Project]:
        """委托 ProjectRepository。"""
        return await self._projects.get_projects_by_token(token_id)

    async def delete_project(self, project_id: str):
        """委托 ProjectRepository。"""
        await self._projects.delete_project(project_id)

    # Task operations
    async def create_task(self, task: Task) -> int:
        """委托 TaskRepository。"""
        return await self._tasks.create_task(task)

    async def get_task(self, task_id: str) -> Optional[Task]:
        """委托 TaskRepository。"""
        return await self._tasks.get_task(task_id)

    async def update_task(self, task_id: str, **kwargs):
        """委托 TaskRepository。"""
        await self._tasks.update_task(task_id, **kwargs)

    # Token stats operations (kept for compatibility, now delegates to specific methods)
    async def increment_token_stats(self, token_id: int, stat_type: str):
        """委托 TokenStatsRepository。"""
        await self._token_stats.increment_token_stats(token_id, stat_type)

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """委托 TokenStatsRepository。"""
        return await self._token_stats.get_token_stats(token_id)

    async def increment_image_count(self, token_id: int):
        """委托 TokenStatsRepository。"""
        await self._token_stats.increment_image_count(token_id)

    async def increment_video_count(self, token_id: int):
        """委托 TokenStatsRepository。"""
        await self._token_stats.increment_video_count(token_id)

    async def increment_error_count(self, token_id: int):
        """委托 TokenStatsRepository。"""
        await self._token_stats.increment_error_count(token_id)

    async def reset_error_count(self, token_id: int):
        """委托 TokenStatsRepository。"""
        await self._token_stats.reset_error_count(token_id)

    # Config operations
    async def get_admin_config(self):
        """委托 ConfigRepository。"""
        return await self._config.get_admin_config()

    async def update_admin_config(self, **kwargs):
        """委托 ConfigRepository。"""
        await self._config.update_admin_config(**kwargs)

    async def get_proxy_config(self):
        """委托 ConfigRepository。"""
        return await self._config.get_proxy_config()

    async def update_proxy_config(self, enabled, proxy_url=None, media_proxy_enabled=None, media_proxy_url=None):
        """委托 ConfigRepository。"""
        await self._config.update_proxy_config(enabled=enabled, proxy_url=proxy_url, media_proxy_enabled=media_proxy_enabled, media_proxy_url=media_proxy_url)

    async def get_generation_config(self):
        """委托 ConfigRepository。"""
        return await self._config.get_generation_config()

    async def update_generation_config(self, image_timeout, video_timeout):
        """委托 ConfigRepository。"""
        await self._config.update_generation_config(image_timeout=image_timeout, video_timeout=video_timeout)

    async def get_call_logic_config(self):
        """委托 ConfigRepository。"""
        return await self._config.get_call_logic_config()

    async def update_call_logic_config(self, call_mode):
        """委托 ConfigRepository。"""
        await self._config.update_call_logic_config(call_mode=call_mode)

    # Request log operations
    async def add_request_log(self, log: RequestLog) -> int:
        """委托 RequestLogRepository。"""
        return await self._request_logs.add_request_log(log)

    async def update_request_log(self, log_id: int, **kwargs):
        """委托 RequestLogRepository。"""
        await self._request_logs.update_request_log(log_id, **kwargs)

    async def get_logs(self, limit: int = 100, token_id: Optional[int] = None, include_payload: bool = False):
        """委托 RequestLogRepository。"""
        return await self._request_logs.get_logs(limit=limit, token_id=token_id, include_payload=include_payload)

    async def get_log_detail(self, log_id: int) -> Optional[Dict[str, Any]]:
        """委托 RequestLogRepository。"""
        return await self._request_logs.get_log_detail(log_id)

    async def clear_all_logs(self):
        """委托 RequestLogRepository。"""
        await self._request_logs.clear_all_logs()

    async def init_config_from_toml(self, config_dict: dict, is_first_startup: bool = True):
        """
        Initialize database configuration from setting.toml

        Args:
            config_dict: Configuration dictionary from setting.toml
            is_first_startup: If True, initialize all config rows from setting.toml.
                            If False (upgrade mode), only ensure missing config rows exist with default values.
        """
        async with self._connect(write=True) as db:
            if is_first_startup:
                # First startup: Initialize all config tables with values from setting.toml
                await self._ensure_config_rows(db, config_dict)
            else:
                # Upgrade mode: Only ensure missing config rows exist (with default values, not from TOML)
                await self._ensure_config_rows(db, config_dict=None)

            await db.commit()

    async def reload_config_to_memory(self):
        """
        Reload all configuration from database to in-memory Config instance.
        This should be called after any configuration update to ensure hot-reload.

        Includes:
        - Admin config (username, password, api_key)
        - Cache config (enabled, timeout, base_url)
        - Generation config (image_timeout, video_timeout)
        - Proxy config will be handled by ProxyManager
        """
        from .config import config

        # Reload admin config
        admin_config = await self.get_admin_config()
        if admin_config:
            config.set_admin_username_from_db(admin_config.username)
            config.set_admin_password_from_db(admin_config.password)
            config.api_key = admin_config.api_key

        # Reload cache config
        cache_config = await self.get_cache_config()
        if cache_config:
            config.set_cache_enabled(cache_config.cache_enabled)
            config.set_cache_timeout(cache_config.cache_timeout)
            config.set_cache_base_url(cache_config.cache_base_url or "")

        # Reload generation config
        generation_config = await self.get_generation_config()
        if generation_config:
            config.set_image_timeout(generation_config.image_timeout)
            config.set_video_timeout(generation_config.video_timeout)

        # Reload call logic config
        call_logic_config = await self.get_call_logic_config()
        if call_logic_config:
            config.set_call_logic_mode(call_logic_config.call_mode)

        # Reload debug config
        debug_config = await self.get_debug_config()
        if debug_config:
            config.set_debug_enabled(debug_config.enabled)

        # Reload captcha config
        captcha_config = await self.get_captcha_config()
        if captcha_config:
            config.set_captcha_method(captcha_config.captcha_method)
            config.set_yescaptcha_api_key(captcha_config.yescaptcha_api_key)
            config.set_yescaptcha_base_url(captcha_config.yescaptcha_base_url)
            config.set_capmonster_api_key(captcha_config.capmonster_api_key)
            config.set_capmonster_base_url(captcha_config.capmonster_base_url)
            config.set_ezcaptcha_api_key(captcha_config.ezcaptcha_api_key)
            config.set_ezcaptcha_base_url(captcha_config.ezcaptcha_base_url)
            config.set_capsolver_api_key(captcha_config.capsolver_api_key)
            config.set_capsolver_base_url(captcha_config.capsolver_base_url)
            config.set_remote_browser_base_url(captcha_config.remote_browser_base_url)
            config.set_remote_browser_api_key(captcha_config.remote_browser_api_key)
            config.set_remote_browser_timeout(captcha_config.remote_browser_timeout)
            config.set_personal_project_pool_size(captcha_config.personal_project_pool_size)
            config.set_personal_max_resident_tabs(captcha_config.personal_max_resident_tabs)
            config.set_personal_idle_tab_ttl_seconds(captcha_config.personal_idle_tab_ttl_seconds)

    # Cache config operations
    async def get_cache_config(self):
        """委托 ConfigRepository。"""
        return await self._config.get_cache_config()

    async def update_cache_config(self, enabled=None, timeout=None, base_url=None):
        """委托 ConfigRepository。"""
        await self._config.update_cache_config(enabled=enabled, timeout=timeout, base_url=base_url)

    async def get_debug_config(self):
        """委托 ConfigRepository。"""
        return await self._config.get_debug_config()

    async def update_debug_config(self, enabled=None, log_requests=None, log_responses=None, mask_token=None):
        """委托 ConfigRepository。"""
        await self._config.update_debug_config(enabled=enabled, log_requests=log_requests, log_responses=log_responses, mask_token=mask_token)

    async def get_captcha_config(self):
        """委托 ConfigRepository。"""
        return await self._config.get_captcha_config()

    async def update_captcha_config(self, captcha_method=None, yescaptcha_api_key=None, yescaptcha_base_url=None, capmonster_api_key=None, capmonster_base_url=None, ezcaptcha_api_key=None, ezcaptcha_base_url=None, capsolver_api_key=None, capsolver_base_url=None, remote_browser_base_url=None, remote_browser_api_key=None, remote_browser_timeout=None, browser_proxy_enabled=None, browser_proxy_url=None, browser_count=None, personal_project_pool_size=None, personal_max_resident_tabs=None, personal_idle_tab_ttl_seconds=None):
        """委托 ConfigRepository。"""
        await self._config.update_captcha_config(captcha_method=captcha_method, yescaptcha_api_key=yescaptcha_api_key, yescaptcha_base_url=yescaptcha_base_url, capmonster_api_key=capmonster_api_key, capmonster_base_url=capmonster_base_url, ezcaptcha_api_key=ezcaptcha_api_key, ezcaptcha_base_url=ezcaptcha_base_url, capsolver_api_key=capsolver_api_key, capsolver_base_url=capsolver_base_url, remote_browser_base_url=remote_browser_base_url, remote_browser_api_key=remote_browser_api_key, remote_browser_timeout=remote_browser_timeout, browser_proxy_enabled=browser_proxy_enabled, browser_proxy_url=browser_proxy_url, browser_count=browser_count, personal_project_pool_size=personal_project_pool_size, personal_max_resident_tabs=personal_max_resident_tabs, personal_idle_tab_ttl_seconds=personal_idle_tab_ttl_seconds)

    async def get_plugin_config(self):
        """委托 ConfigRepository。"""
        return await self._config.get_plugin_config()

    async def update_plugin_config(self, connection_token, auto_enable_on_update=True):
        """委托 ConfigRepository。"""
        await self._config.update_plugin_config(connection_token=connection_token, auto_enable_on_update=auto_enable_on_update)
