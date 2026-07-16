"""Characterization: lock env truthy parsing (headed-allow flags)."""
import os
from tests.conftest import assert_golden


def test_is_truthy_env_golden(monkeypatch):
    from src.services.captcha.environment import is_truthy_env

    cases = {"1": True, "true": True, "TRUE": True, "yes": True, "on": True,
             "0": False, "false": False, "": False, "off": False, "  true  ": True}
    out = {}
    for val, _ in cases.items():
        monkeypatch.setenv("_TEST_FLAG", val)
        out[repr(val)] = is_truthy_env("_TEST_FLAG")
    monkeypatch.delenv("_TEST_FLAG", raising=False)
    out["missing"] = is_truthy_env("_TEST_FLAG")
    assert out["'true'"] is True and out["'0'"] is False and out["missing"] is False
    assert_golden("captcha_environment", out)
