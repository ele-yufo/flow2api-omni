"""Onboarding state-machine admin routes are disabled (410 Gone).

The 2810-line ``OnboardingService`` state machine caused a production incident
(forced re-logins, a destroyed valid session, the wrong XRDP Chrome window
operated). It is replaced by ``scripts/tokens.py onboard`` + the
``TokenLifecycleRepository.publish_verified_account`` tunnel. Its HTTP surface
must stay registered (so misdirected clients get 410, not a confusing 404)
but must never execute the old state machine again.

``validate-profile``, ``lifecycle``, and ``export`` are NOT part of the
disabled state machine and must keep working normally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.models import ProfileValidationResult, Token, TokenLifecycle


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
ADMIN_TOKEN = "admin-onboarding-disabled-test"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class FakeOnboardingService:
    """A service whose methods must never be called once routes are disabled."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.profile_validation = ProfileValidationResult(
            email="ruby@example.com",
            tier="PAYGATE_TIER_ONE",
            credits=850,
            expiry=NOW + timedelta(hours=1),
            project_count=4,
            profile_ready=True,
        )

    def get_safe_config(self):
        self.calls.append(("config", None))
        return {"display": ":42"}

    async def create_job(self, **kwargs):
        self.calls.append(("create", kwargs))
        raise AssertionError("disabled onboarding route must not reach the service")

    async def list(self, **filters):
        self.calls.append(("list", filters))
        raise AssertionError("disabled onboarding route must not reach the service")

    async def get(self, job_id):
        self.calls.append(("get", job_id))
        raise AssertionError("disabled onboarding route must not reach the service")

    async def start_job(self, job_id):
        self.calls.append(("start", job_id))
        raise AssertionError("disabled onboarding route must not reach the service")

    async def finalize(self, job_id):
        self.calls.append(("finalize", job_id))
        raise AssertionError("disabled onboarding route must not reach the service")

    async def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        raise AssertionError("disabled onboarding route must not reach the service")

    async def recover_incomplete(self):
        self.calls.append(("recover", None))
        raise AssertionError("disabled onboarding route must not reach the service")

    async def validate_profile(self, token_id):
        self.calls.append(("validate_profile", token_id))
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
def admin_context():
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


DISABLED_ROUTES = [
    ("get", "/api/onboarding/config", None),
    ("post", "/api/onboarding/jobs", {"conflict_policy": "reject"}),
    ("get", "/api/onboarding/jobs", None),
    ("get", "/api/onboarding/jobs/job-1", None),
    ("post", "/api/onboarding/jobs/job-1/start", None),
    ("post", "/api/onboarding/jobs/job-1/finalize", None),
    ("post", "/api/onboarding/jobs/job-1/cancel", None),
    ("post", "/api/onboarding/recover", None),
]


@pytest.mark.parametrize(("method", "path", "json_body"), DISABLED_ROUTES)
def test_onboarding_state_machine_routes_return_410(admin_context, method, path, json_body):
    client, onboarding, _database = admin_context

    kwargs = {"headers": AUTH_HEADERS}
    if json_body is not None:
        kwargs["json"] = json_body
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 410
    assert response.json()["detail"] == {
        "code": "onboarding_deprecated",
        "message": (
            "onboarding state machine is deprecated; "
            "use 'scripts/tokens.py onboard' instead"
        ),
    }
    # The disabled state machine must never actually run.
    assert onboarding.calls == []


@pytest.mark.parametrize(("method", "path", "json_body"), DISABLED_ROUTES)
def test_onboarding_state_machine_routes_still_require_admin_auth(
    admin_context, method, path, json_body
):
    client, _onboarding, _database = admin_context

    kwargs = {}
    if json_body is not None:
        kwargs["json"] = json_body
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401


def test_validate_profile_route_is_unaffected_by_onboarding_disable(admin_context):
    client, onboarding, _database = admin_context

    response = client.post("/api/tokens/23/validate-profile", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "profile": {
            "email": "ruby@example.com",
            "tier": "PAYGATE_TIER_ONE",
            "credits": 850,
            "expiry": "2026-07-20T13:00:00Z",
            "project_count": 4,
            "profile_ready": True,
        },
    }
    assert onboarding.calls == [("validate_profile", 23)]


def test_lifecycle_put_route_is_unaffected_by_onboarding_disable(admin_context):
    client, _onboarding, database = admin_context

    response = client.put(
        "/api/tokens/23/lifecycle",
        headers=AUTH_HEADERS,
        json={"keepalive_enabled": True, "runtime_mode": "persistent"},
    )

    assert response.status_code == 200
    assert database.desired_updates == [
        (23, {"keepalive_enabled": True, "runtime_mode": "persistent"})
    ]
    payload = response.json()["account"]
    assert payload["keepalive_enabled"] is True
    assert payload["runtime_mode"] == "persistent"


def test_lifecycle_put_route_rejects_warm_runtime_mode(admin_context):
    """``runtime_mode: warm`` is rejected at request validation (422), never reaches the DB.

    A ``warm`` one-shot destroyed a valid Google session in a prior production
    incident by tearing down the resident Chrome and re-navigating, rotating
    the session cookie into an unauthorized state. The admin API must not
    offer any path back to that mode.
    """
    client, _onboarding, database = admin_context

    response = client.put(
        "/api/tokens/23/lifecycle",
        headers=AUTH_HEADERS,
        json={"runtime_mode": "warm"},
    )

    assert response.status_code == 422
    assert database.desired_updates == []


def test_export_route_is_unaffected_by_onboarding_disable(admin_context):
    client, _onboarding, _database = admin_context

    response = client.post("/api/tokens/23/export", headers=AUTH_HEADERS)

    assert response.status_code == 200
    payload = response.json()["token"]
    assert payload["id"] == 23
    assert payload["st"].startswith("eyJ")
