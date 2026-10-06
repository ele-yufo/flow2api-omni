"""Onboarding-family admin routes are disabled (410 Gone).

The 2810-line ``OnboardingService`` state machine caused a production incident
(forced re-logins, a destroyed valid session, the wrong XRDP Chrome window
operated) and has been deleted outright. Onboarding now runs through
``scripts/tokens.py onboard`` + the
``TokenLifecycleRepository.publish_verified_account`` tunnel. The old HTTP
surface must stay registered (so misdirected clients get 410, not a confusing
404) but there is no backing service left to reach.

``validate-profile`` ran on the deleted service and is disabled with the rest;
``lifecycle`` and ``export`` are NOT part of the disabled surface and must
keep working normally.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.models import Token, TokenLifecycle


ADMIN_TOKEN = "admin-onboarding-disabled-test"
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
def admin_context():
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


DISABLED_ROUTES = [
    ("get", "/api/onboarding/config", None),
    ("post", "/api/onboarding/jobs", {"conflict_policy": "reject"}),
    ("get", "/api/onboarding/jobs", None),
    ("get", "/api/onboarding/jobs/job-1", None),
    ("post", "/api/onboarding/jobs/job-1/start", None),
    ("post", "/api/onboarding/jobs/job-1/finalize", None),
    ("post", "/api/onboarding/jobs/job-1/cancel", None),
    ("post", "/api/onboarding/recover", None),
    ("post", "/api/tokens/23/validate-profile", None),
]


@pytest.mark.parametrize(("method", "path", "json_body"), DISABLED_ROUTES)
def test_onboarding_state_machine_routes_return_410(admin_context, method, path, json_body):
    client, _database = admin_context

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


@pytest.mark.parametrize(("method", "path", "json_body"), DISABLED_ROUTES)
def test_onboarding_state_machine_routes_still_require_admin_auth(
    admin_context, method, path, json_body
):
    client, _database = admin_context

    kwargs = {}
    if json_body is not None:
        kwargs["json"] = json_body
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401


def test_lifecycle_put_route_is_unaffected_by_onboarding_disable(admin_context):
    client, database = admin_context

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
    incident by tearing down the resident Chrome and re-navigating, rotating the
    session cookie into an unauthorized state. The admin API must not
    offer any path back to that mode.
    """
    client, database = admin_context

    response = client.put(
        "/api/tokens/23/lifecycle",
        headers=AUTH_HEADERS,
        json={"runtime_mode": "warm"},
    )

    assert response.status_code == 422
    assert database.desired_updates == []


def test_export_route_is_unaffected_by_onboarding_disable(admin_context):
    client, _database = admin_context

    response = client.post("/api/tokens/23/export", headers=AUTH_HEADERS)

    assert response.status_code == 200
    payload = response.json()["token"]
    assert payload["id"] == 23
    assert payload["st"].startswith("eyJ")
