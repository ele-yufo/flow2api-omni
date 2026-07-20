"""Offline packaging contracts for browser account keepalive."""

from pathlib import Path

import tomli


PROJECT_ROOT = Path(__file__).parents[1]
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
SETTING_EXAMPLE = PROJECT_ROOT / "config" / "setting_example.toml"


def test_runtime_dependencies_pin_validated_browser_versions():
    requirements = {
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "browser-cookie3==0.20.1" in requirements
    assert "nodriver==0.48.1" in requirements
    assert not any(line.startswith("nodriver>=") for line in requirements)


def test_example_config_documents_complete_keepalive_host_policy():
    source = SETTING_EXAMPLE.read_text(encoding="utf-8")
    config = tomli.loads(source)
    keepalive = config["keepalive"]

    assert keepalive == {
        "browser_enabled": False,
        "browser_token_ids": "",
        "browser_interval_seconds": 1200,
        "browser_initial_delay_seconds": 120,
        "browser_retired_interval_seconds": 43200,
        "browser_reconcile_interval_seconds": 15,
        "browser_max_concurrent_refreshes": 1,
        "browser_max_concurrent_launches": 1,
        "browser_retry_base_seconds": 60,
        "browser_retry_max_seconds": 1800,
        "browser_human_retry_seconds": 21600,
        "browser_profile_base": "/opt/flow2api-profiles",
        "browser_proxy": "http://127.0.0.1:7890",
        "browser_display": ":10",
        "browser_settle_seconds": 8.0,
        "onboarding_display": ":11",
        "onboarding_session_ttl_seconds": 1800,
    }
    assert "BROWSER_EXECUTABLE_PATH" in source
    assert "token_lifecycle" in source
    assert "migration" in source.casefold() or "迁移" in source
