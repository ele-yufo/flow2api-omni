"""Database storage layer for Flow2API"""
import aiosqlite
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
from ..shared.db import SqliteEngine
from .repositories.token_stats_repository import TokenStatsRepository
from .repositories.request_log_repository import RequestLogRepository
from .repositories.project_repository import ProjectRepository
from .repositories.task_repository import TaskRepository
from .repositories.config_repository import ConfigRepository
from .repositories.token_repository import TokenRepository
from .models import Token, TokenStats, Task, RequestLog, AdminConfig, ProxyConfig, GenerationConfig, CacheConfig, Project, CaptchaConfig, PluginConfig, CallLogicConfig


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
        self._tokens = TokenRepository(self)

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """Ensure all config tables have their default rows

        Args:
            db: Database connection
            config_dict: Configuration dictionary from setting.toml (optional)
                        If None, use default values instead of reading from TOML.
        """
        # Ensure admin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM admin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            admin_username = "admin"
            admin_password = "admin"
            api_key = "han1234"
            error_ban_threshold = 3

            if config_dict:
                global_config = config_dict.get("global", {})
                admin_username = global_config.get("admin_username", "admin")
                admin_password = global_config.get("admin_password", "admin")
                api_key = global_config.get("api_key", "han1234")

                admin_config = config_dict.get("admin", {})
                error_ban_threshold = admin_config.get("error_ban_threshold", 3)

            await db.execute("""
                INSERT INTO admin_config (id, username, password, api_key, error_ban_threshold)
                VALUES (1, ?, ?, ?, ?)
            """, (admin_username, admin_password, api_key, error_ban_threshold))

        # Ensure proxy_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM proxy_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            proxy_enabled = False
            proxy_url = None
            media_proxy_enabled = False
            media_proxy_url = None

            if config_dict:
                proxy_config = config_dict.get("proxy", {})
                proxy_enabled = proxy_config.get("proxy_enabled", False)
                proxy_url = proxy_config.get("proxy_url", "")
                proxy_url = proxy_url if proxy_url else None
                media_proxy_enabled = proxy_config.get(
                    "media_proxy_enabled",
                    proxy_config.get("image_io_proxy_enabled", False)
                )
                media_proxy_url = proxy_config.get(
                    "media_proxy_url",
                    proxy_config.get("image_io_proxy_url", "")
                )
                media_proxy_url = media_proxy_url if media_proxy_url else None

            await db.execute("""
                INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                VALUES (1, ?, ?, ?, ?)
            """, (proxy_enabled, proxy_url, media_proxy_enabled, media_proxy_url))

        # Ensure generation_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM generation_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            image_timeout = 300
            video_timeout = 1500

            if config_dict:
                generation_config = config_dict.get("generation", {})
                image_timeout = generation_config.get("image_timeout", 300)
                video_timeout = generation_config.get("video_timeout", 1500)

            await db.execute("""
                INSERT INTO generation_config (id, image_timeout, video_timeout)
                VALUES (1, ?, ?)
            """, (image_timeout, video_timeout))

        # Ensure call_logic_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM call_logic_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            call_mode = "default"
            polling_mode_enabled = False

            if config_dict:
                call_logic_config = config_dict.get("call_logic", {})
                call_mode = call_logic_config.get("call_mode", "default")
                if call_mode not in ("default", "polling"):
                    polling_mode_enabled = call_logic_config.get("polling_mode_enabled", False)
                    call_mode = "polling" if polling_mode_enabled else "default"
                else:
                    polling_mode_enabled = call_mode == "polling"

            await db.execute("""
                INSERT INTO call_logic_config (id, call_mode, polling_mode_enabled)
                VALUES (1, ?, ?)
            """, (call_mode, polling_mode_enabled))

        # Ensure cache_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM cache_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            cache_enabled = False
            cache_timeout = 7200
            cache_base_url = None

            if config_dict:
                cache_config = config_dict.get("cache", {})
                cache_enabled = cache_config.get("enabled", False)
                cache_timeout = cache_config.get("timeout", 7200)
                cache_base_url = cache_config.get("base_url", "")
                # Convert empty string to None
                cache_base_url = cache_base_url if cache_base_url else None

            await db.execute("""
                INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                VALUES (1, ?, ?, ?)
            """, (cache_enabled, cache_timeout, cache_base_url))

        # Ensure debug_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM debug_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            debug_enabled = False
            log_requests = True
            log_responses = True
            mask_token = True

            if config_dict:
                debug_config = config_dict.get("debug", {})
                debug_enabled = debug_config.get("enabled", False)
                log_requests = debug_config.get("log_requests", True)
                log_responses = debug_config.get("log_responses", True)
                mask_token = debug_config.get("mask_token", True)

            await db.execute("""
                INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                VALUES (1, ?, ?, ?, ?)
            """, (debug_enabled, log_requests, log_responses, mask_token))

        # Ensure captcha_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM captcha_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            captcha_method = "personal"
            yescaptcha_api_key = ""
            yescaptcha_base_url = "https://api.yescaptcha.com"
            remote_browser_base_url = ""
            remote_browser_api_key = ""
            remote_browser_timeout = 60
            browser_count = 1
            personal_project_pool_size = 4
            personal_max_resident_tabs = 5
            personal_idle_tab_ttl_seconds = 600

            if config_dict:
                captcha_config = config_dict.get("captcha", {})
                captcha_method = captcha_config.get("captcha_method", "personal")
                yescaptcha_api_key = captcha_config.get("yescaptcha_api_key", "")
                yescaptcha_base_url = captcha_config.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                remote_browser_base_url = captcha_config.get("remote_browser_base_url", "")
                remote_browser_api_key = captcha_config.get("remote_browser_api_key", "")
                remote_browser_timeout = captcha_config.get("remote_browser_timeout", 60)
                browser_count = captcha_config.get("browser_count", 1)
                personal_project_pool_size = captcha_config.get("personal_project_pool_size", 4)
                personal_max_resident_tabs = captcha_config.get("personal_max_resident_tabs", 5)
                personal_idle_tab_ttl_seconds = captcha_config.get("personal_idle_tab_ttl_seconds", 600)
            try:
                remote_browser_timeout = max(5, int(remote_browser_timeout))
            except Exception:
                remote_browser_timeout = 60
            try:
                browser_count = max(1, int(browser_count))
            except Exception:
                browser_count = 1
            try:
                personal_project_pool_size = max(1, min(50, int(personal_project_pool_size)))
            except Exception:
                personal_project_pool_size = 4
            try:
                personal_max_resident_tabs = max(1, min(50, int(personal_max_resident_tabs)))
            except Exception:
                personal_max_resident_tabs = 5
            try:
                personal_idle_tab_ttl_seconds = max(60, int(personal_idle_tab_ttl_seconds))
            except Exception:
                personal_idle_tab_ttl_seconds = 600

            await db.execute("""
                INSERT INTO captcha_config (
                    id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                    remote_browser_base_url, remote_browser_api_key, remote_browser_timeout,
                    browser_count, personal_project_pool_size,
                    personal_max_resident_tabs, personal_idle_tab_ttl_seconds
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                captcha_method,
                yescaptcha_api_key,
                yescaptcha_base_url,
                remote_browser_base_url,
                remote_browser_api_key,
                remote_browser_timeout,
                browser_count,
                personal_project_pool_size,
                personal_max_resident_tabs,
                personal_idle_tab_ttl_seconds,
            ))

        # Ensure plugin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM plugin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            await db.execute("""
                INSERT INTO plugin_config (id, connection_token, auto_enable_on_update)
                VALUES (1, '', 1)
            """)

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
