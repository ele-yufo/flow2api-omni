"""Characterization: lock the OpenAI/Gemini model-list *response envelope*.

Complements test_model_catalog (which locks catalog CONTENT). Here we lock the
wrapper structure + per-item field schema that downstream consumers parse.

We call the async route handlers directly (passing api_key ourselves) instead of
spinning up TestClient — that avoids triggering the app lifespan (which would open
the DB and start browser/background tasks). The handlers are plain async funcs, so
this exercises the real envelope-building code deterministically and offline.
"""
import asyncio

from tests.conftest import assert_golden


def _envelope_schema(payload: dict, items_key: str) -> dict:
    """Reduce a list response to its structural fingerprint (no bulky descriptions)."""
    items = payload.get(items_key, [])
    first = items[0] if items else {}
    return {
        "top_level_keys": sorted(payload.keys()),
        "items_key": items_key,
        "count": len(items),
        "item_keys": sorted(first.keys()),
        "sample_first_no_desc": {
            k: v for k, v in sorted(first.items()) if k != "description"
        },
    }


def test_openai_models_envelope():
    from src.api.routes import list_models

    payload = asyncio.run(list_models(api_key="test"))
    assert payload["object"] == "list"
    assert_golden("openai_models_envelope", _envelope_schema(payload, "data"))


def test_gemini_models_envelope():
    from src.api.routes import list_gemini_models

    payload = asyncio.run(list_gemini_models(api_key="test"))
    assert "models" in payload
    assert_golden("gemini_models_envelope", _envelope_schema(payload, "models"))
