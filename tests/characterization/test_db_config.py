"""Characterization: lock config-table CRUD (admin/cache/captcha covering kwargs/params/clamp)."""
import asyncio

from tests.conftest import assert_golden

_VOL = {"updated_at", "created_at", "id"}


def _scrub(m):
    if m is None:
        return None
    d = m.model_dump() if hasattr(m, "model_dump") else dict(m)
    return {k: v for k, v in sorted(d.items()) if k not in _VOL}


def test_config_crud_golden(temp_db_path):
    from src.core.database import Database

    async def run():
        db = Database(db_path=temp_db_path)
        await db.init_db()
        await db.init_config_from_toml({}, is_first_startup=True)  # 建 config 默认行

        # admin: **kwargs update
        await db.update_admin_config(api_key="KEY123")
        admin = await db.get_admin_config()

        # generation: positional params
        await db.update_generation_config(image_timeout=99, video_timeout=88)
        gen = await db.get_generation_config()

        # cache: partial params + empty-string→None
        await db.update_cache_config(enabled=True, timeout=1234)
        cache1 = await db.get_cache_config()
        await db.update_cache_config(base_url="")  # empty -> None
        cache2 = await db.get_cache_config()

        # call_logic: normalization
        await db.update_call_logic_config("polling")
        cl = await db.get_call_logic_config()

        # captcha: many params + clamps (idle ttl floor 60, pool clamp 1-50)
        await db.update_captcha_config(captcha_method="personal",
                                       personal_idle_tab_ttl_seconds=5,  # -> floored to 60
                                       personal_project_pool_size=999)   # -> clamped 50
        cap = await db.get_captcha_config()

        # proxy / debug / plugin (auto-generated delegations)
        await db.update_proxy_config(enabled=True, proxy_url="http://x:1",
                                     media_proxy_enabled=True, media_proxy_url="http://y:2")
        proxy = await db.get_proxy_config()
        await db.update_debug_config(enabled=True, mask_token=False)
        debug = await db.get_debug_config()
        await db.update_plugin_config(connection_token="TK", auto_enable_on_update=False)
        plugin = await db.get_plugin_config()

        return {
            "admin": _scrub(admin),
            "generation": _scrub(gen),
            "cache_after_enable": _scrub(cache1),
            "cache_base_url_is_none": cache2.cache_base_url is None,
            "call_logic": _scrub(cl),
            "captcha": _scrub(cap),
            "proxy": _scrub(proxy),
            "debug": _scrub(debug),
            "plugin": _scrub(plugin),
        }

    out = asyncio.run(run())
    assert out["admin"]["api_key"] == "KEY123"
    assert out["generation"]["image_timeout"] == 99
    assert out["cache_after_enable"]["cache_timeout"] == 1234
    assert out["cache_base_url_is_none"] is True
    assert out["call_logic"]["call_mode"] == "polling"
    assert out["captcha"]["personal_idle_tab_ttl_seconds"] == 60   # clamp floor
    assert out["captcha"]["personal_project_pool_size"] == 50       # clamp ceil
    assert out["captcha"]["captcha_method"] == "personal"
    assert out["proxy"]["proxy_url"] == "http://x:1"
    assert out["debug"]["mask_token"] is False
    assert out["plugin"]["connection_token"] == "TK"
    assert_golden("db_config", out)
