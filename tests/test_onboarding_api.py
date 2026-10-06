"""Admin lifecycle API tests without app lifespan or real Chrome.

The onboarding state-machine routes (config/jobs/start/finalize/cancel/
recover, plus ``validate-profile`` which ran on the same deleted service)
are permanently disabled with 410 Gone — that contract is pinned in
``tests/test_admin_onboarding_disabled.py``. This file keeps the original
coverage of the still-live ``lifecycle`` desired-state endpoint.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.models import Token, TokenLifecycle


ADMIN_TOKEN = "admin-test-session"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class FakeDatabase:
    def __init__(self):
        self.token = Token(
            id=23,
            st="eyJ" + "s" * 1100,
            at="must-not-leak",
            email="ruby@example.com",
            is_active=False,
            ban_reason="manual_disabled",
        )
        self.lifecycle = TokenLifecycle(
            token_id=23,
            keepalive_enabled=False,
            runtime_mode="warm",
            profile_state="ready",
            verified_email="ruby@example.com",
        )
        self.desired_updates: list[tuple[int, dict]] = []

    async def get_token(self, token_id):
        return self.token if token_id == self.token.id else None

    async def get_token_lifecycle(self, token_id):
        return self.lifecycle if token_id == self.lifecycle.token_id else None

    async def set_token_desired_state(self, token_id, **fields):
        if token_id != self.lifecycle.token_id:
            raise KeyError(token_id)
        self.desired_updates.append((token_id, dict(fields)))
        updates = {}
        if fields.get("keepalive_enabled") is not None:
            updates["keepalive_enabled"] = fields["keepalive_enabled"]
        if fields.get("runtime_mode") is not None:
            updates["runtime_mode"] = fields["runtime_mode"]
        self.lifecycle = self.lifecycle.model_copy(update=updates)


@pytest.fixture
def api_context():
    database = FakeDatabase()
    app = FastAPI()
    app.include_router(admin.router)
    admin.set_dependencies(
        SimpleNamespace(),
        SimpleNamespace(),
        database,
        None,
    )
    admin.active_admin_tokens.add(ADMIN_TOKEN)
    try:
        with TestClient(app) as client:
            yield client, database
    finally:
        admin.active_admin_tokens.discard(ADMIN_TOKEN)
        admin.set_dependencies(None, None, None, None)


def test_lifecycle_endpoint_changes_keepalive_only_and_returns_no_credentials(api_context):
    client, database = api_context

    response = client.put(
        "/api/tokens/23/lifecycle",
        headers=AUTH_HEADERS,
        json={"keepalive_enabled": True, "runtime_mode": "persistent"},
    )

    assert response.status_code == 200
    assert database.desired_updates == [
        (23, {"keepalive_enabled": True, "runtime_mode": "persistent"})
    ]
    assert database.token.is_active is False
    assert database.token.ban_reason == "manual_disabled"
    payload = response.json()["account"]
    assert payload["email"] == "ruby@example.com"
    assert payload["is_active"] is False
    assert payload["ban_reason"] == "manual_disabled"
    assert payload["keepalive_enabled"] is True
    assert payload["runtime_mode"] == "persistent"
    assert "st" not in payload
    assert "at" not in payload
    assert "must-not-leak" not in response.text


@pytest.mark.parametrize(
    ("request_body", "expected_update", "expected_enabled", "expected_mode"),
    [
        ({"keepalive_enabled": True}, {"keepalive_enabled": True}, True, "warm"),
        ({"runtime_mode": "persistent"}, {"runtime_mode": "persistent"}, False, "persistent"),
    ],
)
def test_lifecycle_endpoint_supports_atomic_partial_updates(
    api_context,
    request_body,
    expected_update,
    expected_enabled,
    expected_mode,
):
    client, database = api_context

    response = client.put(
        "/api/tokens/23/lifecycle",
        headers=AUTH_HEADERS,
        json=request_body,
    )

    assert response.status_code == 200
    assert database.desired_updates == [(23, expected_update)]
    account = response.json()["account"]
    assert account["keepalive_enabled"] is expected_enabled
    assert account["runtime_mode"] == expected_mode


def test_lifecycle_endpoint_rejects_empty_update(api_context):
    client, database = api_context

    response = client.put(
        "/api/tokens/23/lifecycle",
        headers=AUTH_HEADERS,
        json={},
    )

    assert response.status_code == 422
    assert database.desired_updates == []


def test_lifecycle_endpoint_returns_404_for_missing_token(api_context):
    client, _database = api_context

    response = client.put(
        "/api/tokens/999/lifecycle",
        headers=AUTH_HEADERS,
        json={"keepalive_enabled": True},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Token not found"
