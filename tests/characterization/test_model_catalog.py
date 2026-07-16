"""Characterization: lock the full model catalog before P2 data migration."""
from tests.conftest import assert_golden


def test_model_config_snapshot():
    from src.services.generation_handler import MODEL_CONFIG

    snapshot = {k: v for k, v in MODEL_CONFIG.items()}
    assert_golden("model_catalog", snapshot)


def test_openai_model_list_snapshot():
    from src.api.routes import _get_openai_model_catalog

    assert_golden("openai_model_list", _get_openai_model_catalog())


def test_gemini_model_catalog_snapshot():
    from src.api.routes import _get_gemini_model_catalog

    assert_golden("gemini_model_catalog", _get_gemini_model_catalog())
