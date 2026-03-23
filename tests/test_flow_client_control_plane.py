import asyncio

from src.services import flow_client as flow_client_module
from src.services.flow_client import FlowClient


def test_control_plane_timeout_is_capped():
    client = FlowClient(None)

    client.timeout = 120
    assert client._get_control_plane_timeout() == 10

    client.timeout = 8
    assert client._get_control_plane_timeout() == 8

    client.timeout = 3
    assert client._get_control_plane_timeout() == 5


def test_control_plane_calls_use_short_timeouts(monkeypatch):
    client = FlowClient(None)
    client.timeout = 120
    calls = []

    async def fake_make_request(**kwargs):
        calls.append({
            "url": kwargs["url"],
            "timeout": kwargs.get("timeout"),
        })
        url = kwargs["url"]
        if url.endswith("/auth/session"):
            return {"access_token": "at", "user": {"email": "tester@example.com"}}
        if url.endswith("/trpc/project.createProject"):
            return {"result": {"data": {"json": {"result": {"projectId": "project-123"}}}}}
        if url.endswith("/credits"):
            return {"credits": 1000, "userPaygateTier": "PAYGATE_TIER_ONE"}
        return {}

    monkeypatch.setattr(client, "_make_request", fake_make_request)

    async def run():
        await client.st_to_at("st")
        await client.create_project("st", "demo")
        await client.delete_project("st", "project-123")
        await client.get_credits("at")

    asyncio.run(run())

    assert [call["timeout"] for call in calls] == [10, 15, 10, 10]


def test_remote_browser_http_helper_uses_asyncsession(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'

        def json(self):
            return {"ok": True}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, **kwargs):
            calls.append({
                "method": method,
                "url": url,
                "kwargs": kwargs,
            })
            return FakeResponse()

    monkeypatch.setattr(flow_client_module, "AsyncSession", FakeSession)

    status_code, payload, response_text = asyncio.run(
        FlowClient._sync_json_http_request(
            method="POST",
            url="https://example.com/api/v1/solve",
            headers={"Authorization": "Bearer token"},
            payload={"project_id": "project-123"},
            timeout=12,
        )
    )

    assert status_code == 200
    assert payload == {"ok": True}
    assert response_text == '{"ok": true}'
    assert calls == [
        {
            "method": "POST",
            "url": "https://example.com/api/v1/solve",
            "kwargs": {
                "headers": {
                    "Authorization": "Bearer token",
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                },
                "timeout": 12,
                "impersonate": "chrome120",
                "json": {"project_id": "project-123"},
            },
        }
    ]
