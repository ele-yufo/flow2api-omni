"""Characterization: lock config clamp/default behavior before P1 Settings refactor."""
import pytest

from tests.conftest import assert_golden


def _config_with(raw: dict):
    from src.core.config import Config

    cfg = Config.__new__(Config)          # 跳过 __init__ 的文件读取
    cfg._config = raw
    cfg._admin_username = None
    cfg._admin_password = None
    return cfg


def test_config_clamp_golden():
    # 各种坏/边界输入,锁兜底行为
    variants = {
        "empty": {},
        "bad_types": {"flow": {"timeout": "abc", "max_retries": -5}},
        "extreme": {"flow": {"timeout": 1, "max_retries": 999}},
    }
    out = {}
    for name, raw in variants.items():
        cfg = _config_with(raw)
        out[name] = {
            "flow_timeout": cfg.flow_timeout,
            "flow_max_retries": cfg.flow_max_retries,
            "min_credits_to_select": cfg.min_credits_to_select,
        }
    assert_golden("config_clamp", out)


def test_keepalive_runtime_config_defaults_and_clamps():
    defaults = _config_with({})
    assert defaults.keepalive_browser_initial_delay_seconds == 120
    assert defaults.keepalive_browser_retired_interval_seconds == 43200
    assert defaults.keepalive_browser_reconcile_interval_seconds == 15
    assert defaults.keepalive_browser_max_concurrent_refreshes == 1
    assert defaults.keepalive_browser_max_concurrent_launches == 1
    assert defaults.keepalive_browser_retry_base_seconds == 60
    assert defaults.keepalive_browser_retry_max_seconds == 1800
    assert defaults.keepalive_browser_human_retry_seconds == 21600
    assert defaults.keepalive_onboarding_display == ":11"
    assert defaults.keepalive_onboarding_session_ttl_seconds == 1800

    clamped = _config_with({
        "keepalive": {
            "browser_initial_delay_seconds": -1,
            "browser_retired_interval_seconds": 10,
            "browser_reconcile_interval_seconds": 0,
            "browser_max_concurrent_refreshes": 0,
            "browser_max_concurrent_launches": 999,
            "browser_retry_base_seconds": 0,
            "browser_retry_max_seconds": 1,
            "browser_human_retry_seconds": 20,
            "onboarding_display": "  ",
            "onboarding_session_ttl_seconds": 20,
        }
    })
    assert clamped.keepalive_browser_initial_delay_seconds == 0
    assert clamped.keepalive_browser_retired_interval_seconds == 3600
    assert clamped.keepalive_browser_reconcile_interval_seconds == 5
    assert clamped.keepalive_browser_max_concurrent_refreshes == 1
    assert clamped.keepalive_browser_max_concurrent_launches == 10
    assert clamped.keepalive_browser_retry_base_seconds == 10
    assert clamped.keepalive_browser_retry_max_seconds == 30
    assert clamped.keepalive_browser_human_retry_seconds == 300
    assert clamped.keepalive_onboarding_display == ":11"
    assert clamped.keepalive_onboarding_session_ttl_seconds == 300


def test_cors_allowed_origins_are_explicit_normalized_and_environment_overridable(monkeypatch):
    defaults = _config_with({})
    assert defaults.server_cors_allowed_origins == []

    configured = _config_with({
        "server": {
            "cors_allowed_origins": [
                " https://admin.example.com/ ",
                "chrome-extension://abcdefghijklmnop",
                "https://admin.example.com",
                "",
            ],
        },
    })
    assert configured.server_cors_allowed_origins == [
        "https://admin.example.com",
        "chrome-extension://abcdefghijklmnop",
    ]

    monkeypatch.setenv(
        "FLOW2API_CORS_ALLOWED_ORIGINS",
        "https://console.example.com, chrome-extension://extensionid ",
    )
    assert configured.server_cors_allowed_origins == [
        "https://console.example.com",
        "chrome-extension://extensionid",
    ]


@pytest.mark.parametrize(
    "origin",
    ["*", "https://admin.example.com/manage", "admin.example.com"],
)
def test_cors_allowed_origins_reject_unsafe_or_non_origin_values(monkeypatch, origin):
    monkeypatch.delenv("FLOW2API_CORS_ALLOWED_ORIGINS", raising=False)
    configured = _config_with({"server": {"cors_allowed_origins": [origin]}})

    with pytest.raises(ValueError, match="CORS origin"):
        configured.server_cors_allowed_origins
