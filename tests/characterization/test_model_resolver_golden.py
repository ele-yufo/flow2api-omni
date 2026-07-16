"""Characterization: lock resolve_model_name input→output across all branches."""
from tests.conftest import assert_golden


class _Req:
    """Minimal stand-in exposing generationConfig like ChatCompletionRequest."""
    def __init__(self, generation_config=None):
        self.generationConfig = generation_config
        self.generation_config = generation_config


def _resolve(model, gen_cfg=None):
    from src.core.model_resolver import resolve_model_name
    from src.services.generation_handler import MODEL_CONFIG

    req = _Req(gen_cfg) if gen_cfg is not None else None
    return resolve_model_name(model=model, request=req, model_config=MODEL_CONFIG)


def test_resolver_golden_matrix(subtests):
    cases = [
        # passthrough: already-full MODEL_CONFIG keys return as-is
        ("img_full_key", "gemini-3.0-pro-image-square-2k", None),
        ("omni_full_key", "gemini_omni_t2v_4s", None),
        ("unknown_model", "this-model-does-not-exist", None),
        # assembly branch (what P2 refactors): short base + generationConfig → full key
        (
            "img_assemble_square_2k",
            "gemini-3.0-pro-image",
            {"imageConfig": {"aspectRatio": "1:1", "imageSize": "2K"}},
        ),
        (
            "img_assemble_default_aspect",
            "gemini-3.1-flash-image",
            {},
        ),
        (
            "video_assemble_portrait",
            "veo_3_1_t2v_fast",
            {"imageConfig": {"aspectRatio": "9:16"}},
        ),
        (
            "video_assemble_default_landscape",
            "veo_3_1_t2v_fast",
            {},
        ),
    ]
    results = {}
    for name, model, gen in cases:
        with subtests.test(case=name):
            results[name] = _resolve(model, gen)
    assert_golden("model_resolver_cases", results)
