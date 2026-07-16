"""Characterization: lock pooled project naming."""
from datetime import datetime
from tests.conftest import assert_golden


def test_project_naming_golden():
    from src.services.tokens.project_naming import normalize_project_name_base, build_project_name

    fixed = datetime(2026, 3, 5, 14, 30)
    out = {
        "plain": normalize_project_name_base("My Project"),
        "strip_pool_suffix": normalize_project_name_base("My Project P3"),  # -> base
        "not_pool_suffix": normalize_project_name_base("My Project Phase"),  # keep
        "empty_fallback": normalize_project_name_base("", now=fixed),
        "build": build_project_name(2, "My Project"),
        "build_from_pooled": build_project_name(5, "My Project P1"),  # base re-extracted
    }
    assert out["strip_pool_suffix"] == "My Project"
    assert out["not_pool_suffix"] == "My Project Phase"
    assert out["build"] == "My Project P2"
    assert out["build_from_pooled"] == "My Project P5"
    assert_golden("project_naming", out)
