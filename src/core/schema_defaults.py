"""Config-table default rows setup (flow2api schema defaults).

Extracted from Database (0 self; param-driven: operates on the passed db connection).
Seeds default rows for all 8 single-row config tables on first init. Behavior covered by
tests/characterization/test_db_config.py (via init_config_from_toml).
"""


async def ensure_config_rows(db, config_dict: dict = None):
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
