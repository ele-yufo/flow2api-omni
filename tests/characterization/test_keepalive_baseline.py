"""Characterization tests for the deployed browser keepalive baseline."""

import inspect


def _config_with(raw: dict):
    from src.core.config import Config

    cfg = Config.__new__(Config)
    cfg._config = raw
    cfg._admin_username = None
    cfg._admin_password = None
    return cfg


def test_keepalive_host_config_baseline():
    cfg = _config_with({
        "keepalive": {
            "browser_enabled": True,
            "browser_interval_seconds": "1200",
            "browser_token_ids": "23, 22,invalid",
            "browser_profile_base": "/opt/flow2api-profiles",
            "browser_proxy": "http://127.0.0.1:7890",
            "browser_display": ":10",
            "browser_settle_seconds": "8",
        }
    })

    assert cfg.keepalive_browser_enabled is True
    assert cfg.keepalive_browser_interval_seconds == 1200
    assert cfg.keepalive_browser_token_ids == [23, 22]
    assert cfg.keepalive_browser_profile_base == "/opt/flow2api-profiles"
    assert cfg.keepalive_browser_proxy == "http://127.0.0.1:7890"
    assert cfg.keepalive_browser_display == ":10"
    assert cfg.keepalive_browser_settle_seconds == 8.0


def test_keepalive_browser_is_headed_by_default():
    from src.services.keepalive.refresher import launch_keepalive_browser

    headless = inspect.signature(launch_keepalive_browser).parameters["headless"]
    assert headless.default is False


def test_keepalive_entrypoint_supports_one_shot_validation():
    from pathlib import Path

    source = (Path(__file__).parents[2] / "scripts" / "keepalive_browser.py").read_text(
        encoding="utf-8"
    )
    assert '"--once"' in source
    assert '"--token-id"' in source
    assert '"--preflight"' in source
    assert "KeepaliveSupervisor" in source
    assert "ManagedAccountRunner" in source
