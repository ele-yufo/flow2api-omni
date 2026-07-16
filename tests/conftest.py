"""Shared fixtures + golden-file characterization helper for P0 safety net."""
import json
import os
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"
PROD_DB = (Path(__file__).parent.parent / "data" / "flow.db").resolve()


def assert_golden(name: str, actual) -> None:
    """Compare `actual` against tests/golden/<name>.json.

    REGEN_GOLDEN=1 writes the golden (first-time capture). Otherwise strict compare.
    Serialization is canonical (sorted keys) so dict ordering never causes churn.
    """
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{name}.json"
    payload = json.dumps(actual, sort_keys=True, ensure_ascii=False, indent=2)
    if os.environ.get("REGEN_GOLDEN") == "1" or not path.exists():
        path.write_text(payload, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    assert payload == expected, (
        f"Golden mismatch for {name!r}. "
        f"If this change is intentional, rerun with REGEN_GOLDEN=1 and review the diff."
    )


@pytest.fixture
def temp_db_path(tmp_path) -> str:
    return str(tmp_path / "test_flow.db")


@pytest.fixture
def openai_chat_request() -> dict:
    return {
        "model": "gemini-3.1-flash-image-landscape",
        "messages": [{"role": "user", "content": "a red apple on a wooden table"}],
        "stream": True,
    }


@pytest.fixture
def gemini_generate_request() -> dict:
    return {
        "contents": [
            {"role": "user", "parts": [{"text": "a red apple on a wooden table"}]}
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"},
        },
    }


@pytest.fixture(autouse=True)
def _forbid_prod_db(monkeypatch):
    """Fail loudly if any test tries to open the live production DB."""
    import sqlite3

    real_connect = sqlite3.connect

    def guarded(database, *args, **kwargs):
        try:
            if Path(str(database)).resolve() == PROD_DB:
                raise AssertionError(
                    f"Test attempted to open production DB {PROD_DB}. Use temp_db_path."
                )
        except (OSError, ValueError):
            pass
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded)
    yield
