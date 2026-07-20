"""Admin lifecycle + profile-validation API tests without app lifespan or real Chrome.

The onboarding job-CRUD routes (config/jobs/start/finalize/cancel/recover) used
to be tested here too, but that HTTP surface is now permanently disabled (410
Gone, see ``tests/test_admin_onboarding_disabled.py``) after the 2810-line
``OnboardingService`` state machine caused a production incident. Those tests
were removed as obsolete rather than updated, since they asserted behavior
(job creation, job resumption, service-error-to-HTTP-status mapping) that can
no longer happen through the HTTP layer at all.

``validate-profile`` and ``lifecycle`` are NOT part of the disabled state
machine and keep their original coverage here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.models import ProfileValidationResult, Token, TokenLifecycle
from src.services.onboarding import OnboardingServiceError


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
ADMIN_TOKEN = "admin-test-session"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class FakeOnboardingService:
    def __init__(self):
        self.profile_validation = ProfileValidationResult(
            email="ruby@example.com",
            tier="PAYGATE_TIER_ONE",
            credits=850,
            expiry=NOW + timedelta(hours=1),
            project_count=4,
            profile_ready=True,
        )
        self.calls: list[tuple[str, object]] = []
        self.error: OnboardingServiceError | None = None

    async def validate_profile(self, token_id):
        self.calls.append(("validate_profile", token_id))
        if self.error is not None:
            raise self.error
        return self.profile_validation


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
    onboarding = FakeOnboardingService()
    database = FakeDatabase()
    app = FastAPI()
    app.include_router(admin.router)
    admin.set_dependencies(
        SimpleNamespace(),
        SimpleNamespace(),
        database,
        None,
        onboarding,
    )
    admin.active_admin_tokens.add(ADMIN_TOKEN)
    try:
        with TestClient(app) as client:
            yield client, onboarding, database
    finally:
        admin.active_admin_tokens.discard(ADMIN_TOKEN)
        admin.set_dependencies(None, None, None, None, None)


def test_profile_validation_requires_admin_and_returns_only_safe_read_only_result(api_context):
    client, onboarding, database = api_context
    token_before = database.token.model_copy(deep=True)
    lifecycle_before = database.lifecycle.model_copy(deep=True)

    unauthenticated = client.post("/api/tokens/23/validate-profile")
    response = client.post(
        "/api/tokens/23/validate-profile",
        headers=AUTH_HEADERS,
    )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "success": True,
        "profile": {
            "email": "ruby@example.com",
            "tier": "PAYGATE_TIER_ONE",
            "credits": 850,
            "expiry": "2026-07-19T13:00:00Z",
            "project_count": 4,
            "profile_ready": True,
        },
    }
    assert onboarding.calls == [("validate_profile", 23)]
    assert database.token == token_before
    assert database.lifecycle == lifecycle_before
    for forbidden in ("must-not-leak", '"st"', '"at"', "profile_path", "browser_pid"):
        assert forbidden not in response.text


def test_profile_validation_identity_failure_is_safe_and_does_not_fall_back_to_token_st(api_context):
    client, onboarding, database = api_context
    onboarding.error = OnboardingServiceError("profile_identity_mismatch")

    response = client.post(
        "/api/tokens/23/validate-profile",
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "profile_identity_mismatch",
        "message": "The retained profile identity does not match its account binding.",
    }
    assert onboarding.calls == [("validate_profile", 23)]
    assert database.token.st.startswith("eyJ")
    assert "must-not-leak" not in response.text


def test_lifecycle_endpoint_changes_keepalive_only_and_returns_no_credentials(api_context):
    client, _onboarding, database = api_context

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
    client, _onboarding, database = api_context

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
    client, _onboarding, database = api_context

    response = client.put(
        "/api/tokens/23/lifecycle",
        headers=AUTH_HEADERS,
        json={},
    )

    assert response.status_code == 422
    assert database.desired_updates == []


def test_lifecycle_endpoint_returns_404_for_missing_token(api_context):
    client, _onboarding, _database = api_context

    response = client.put(
        "/api/tokens/999/lifecycle",
        headers=AUTH_HEADERS,
        json={"keepalive_enabled": True},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Token not found"
