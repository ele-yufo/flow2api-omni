"""Characterization: lock handle_generation entry validation (unknown model -> 400).

Protects the top-level generation contract: unsupported models yield a 400 error and stop,
without touching tokens/flow. Uses __new__ to bypass the dependency-wiring constructor.
"""
import asyncio
import json


def _collect(agen):
    async def run():
        out = []
        async for chunk in agen:
            out.append(chunk)
        return out
    return asyncio.run(run())


def _bare_handler():
    from src.services.generation_handler import GenerationHandler
    gh = GenerationHandler.__new__(GenerationHandler)
    gh.flow_client = object()  # no clear_request_fingerprint -> skipped
    return gh


def test_unknown_model_non_stream_yields_400():
    gh = _bare_handler()
    chunks = _collect(gh.handle_generation("no-such-model", "hello", stream=False))
    assert len(chunks) == 1
    payload = json.loads(chunks[0])
    assert payload["error"]["status_code"] == 400
    assert "不支持的模型" in payload["error"]["message"]


def test_unknown_model_stream_yields_400():
    gh = _bare_handler()
    chunks = _collect(gh.handle_generation("no-such-model", "hello", stream=True))
    # stream path still ends with the error response (last chunk parseable as error JSON)
    assert any("不支持的模型" in c for c in chunks)


def test_known_model_passes_validation():
    """Known model must NOT short-circuit at validation (it proceeds past the model check).

    We stop it right after by giving a flow_client whose next-used attr blows up, proving
    validation passed (different failure than the 400 model error)."""
    gh = _bare_handler()
    # A known image model exists in MODEL_CONFIG; validation should pass. We only assert the
    # 400-unsupported-model path is NOT taken (any downstream error differs).
    from src.services.generation_handler import MODEL_CONFIG
    known = next(k for k, v in MODEL_CONFIG.items() if v["type"] == "image")
    # collect until first error/exception; we only check it's not the "unsupported model" 400
    try:
        chunks = _collect(gh.handle_generation(known, "hi", stream=False))
        joined = " ".join(chunks)
    except Exception as e:
        joined = str(e)
    assert "不支持的模型" not in joined
