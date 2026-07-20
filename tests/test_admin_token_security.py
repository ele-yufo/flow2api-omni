"""Admin token-list sanitization and explicit secret-export API tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.models import Token


ADMIN_TOKEN = "admin-token-security-test"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
LONG_ST = "eyJ" + "s" * 1100
ACCESS_TOKEN = "access-token-must-only-appear-in-export"


class FakeDatabase:
    def __init__(self):
        self.token = Token(
            id=23,
            st=LONG_ST,
            at=ACCESS_TOKEN,
            at_expires=NOW,
            email="ruby@example.com",
            name="Ruby",
            remark="baseline",
            is_active=False,
            credits=900,
            user_paygate_tier="PAYGATE_TIER_ONE",
            ban_reason="manual_disabled",
        )

    async def get_all_tokens_with_stats(self):
        return [
            {
                **self.token.model_dump(),
                "image_count": 3,
                "video_count": 4,
                "error_count": 1,
                "membership_confirmed_status": "active",
                "membership_candidate": "paid",
                "membership_candidate_count": 1,
                "keepalive_enabled": 1,
                "runtime_mode": "persistent",
                "profile_state": "ready",
                "verified_email": "ruby@example.com",
                "last_keepalive_success_at": NOW,
                "last_keepalive_status": "success",
                "next_due_at": NOW,
                "last_failure_at": None,
                "last_failure_code": None,
                "last_observed_tier": "PAYGATE_TIER_ONE",
                "last_observed_at": NOW,
                "retired_at": None,
                "restored_at": None,
            }
        ]

    async def get_token(self, token_id):
        return self.token if token_id == self.token.id else None

    async def get_plugin_config(self):
        return SimpleNamespace(
            connection_token="plugin-connection-token",
            auto_enable_on_update=False,
        )


@pytest.fixture
def client():
    database = FakeDatabase()
    app = FastAPI()
    app.include_router(admin.router)
    admin.set_dependencies(None, None, database, None, None)
    admin.active_admin_tokens.add(ADMIN_TOKEN)
    try:
        with TestClient(app, base_url="https://flow.example.com") as test_client:
            yield test_client
    finally:
        admin.active_admin_tokens.discard(ADMIN_TOKEN)
        admin.set_dependencies(None, None, None, None, None)


def test_ordinary_token_list_omits_credentials_and_includes_lifecycle(client):
    response = client.get("/api/tokens", headers=AUTH_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    account = payload[0]
    assert account["id"] == 23
    assert account["email"] == "ruby@example.com"
    assert account["has_st"] is True
    assert account["has_at"] is True
    assert account["keepalive_enabled"] is True
    assert account["runtime_mode"] == "persistent"
    assert account["profile_state"] == "ready"
    assert account["membership_confirmed_status"] == "active"
    assert account["ban_reason"] == "manual_disabled"
    assert "st" not in account
    assert "at" not in account
    assert "token" not in account
    assert LONG_ST not in response.text
    assert ACCESS_TOKEN not in response.text


def test_secret_export_requires_admin_authentication(client):
    response = client.post("/api/tokens/23/export")

    assert response.status_code == 401


def test_explicit_secret_export_is_no_store_and_contains_only_requested_account(client):
    response = client.post("/api/tokens/23/export", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()["token"]
    assert payload == {
        "id": 23,
        "email": "ruby@example.com",
        "st": LONG_ST,
        "at": ACCESS_TOKEN,
        "at_expires": NOW.isoformat(),
    }


def test_secret_export_returns_404_for_missing_account(client):
    response = client.post("/api/tokens/999/export", headers=AUTH_HEADERS)

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == "Token not found"


def test_plugin_connection_url_preserves_public_https_scheme(client):
    response = client.get("/api/plugin/config", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["config"]["connection_url"] == (
        "https://flow.example.com/api/plugin/update-token"
    )
