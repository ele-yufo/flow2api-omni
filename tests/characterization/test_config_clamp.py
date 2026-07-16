"""Characterization: lock config clamp/default behavior before P1 Settings refactor."""
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
