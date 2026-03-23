import asyncio
import json

from src.api import admin as admin_module
from src.api import routes
from src.core.auth import AuthManager, verify_api_key_flexible


def build_openai_completion(content: str) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "flow2api",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )


def test_openai_route_resolves_alias_and_returns_non_stream_result(client, fake_handler):
    fake_handler.non_stream_chunks = [build_openai_completion("![Generated Image](https://example.com/out.png)")]

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.0-pro-image",
            "messages": [{"role": "user", "content": "draw a sunset"}],
            "generationConfig": {
                "imageConfig": {
                    "aspectRatio": "16:9",
                    "imageSize": "2K",
                }
            },
        },
    )

    assert response.status_code == 200
    assert fake_handler.calls[0]["model"] == "gemini-3.0-pro-image-landscape-2k"
    assert response.json()["choices"][0]["message"]["content"].startswith("![Generated Image]")


def test_openai_route_returns_handler_error_status(client, fake_handler):
    fake_handler.non_stream_chunks = [
        json.dumps(
            {
                "error": {
                    "message": "没有可用的Token进行图片生成",
                    "status_code": 503,
                }
            }
        )
    ]

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.0-pro-image",
            "messages": [{"role": "user", "content": "draw a tree"}],
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "没有可用的Token进行图片生成"


def test_flexible_auth_accepts_x_goog_api_key(monkeypatch):
    monkeypatch.setattr(AuthManager, "verify_api_key", staticmethod(lambda api_key: api_key == "secret"))

    assert asyncio.run(
        verify_api_key_flexible(
            credentials=None,
            x_goog_api_key="secret",
            key=None,
        )
    ) == "secret"


def test_admin_remote_browser_helper_uses_asyncsession(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"success": true, "token": "abc"}'

        def json(self):
            return {"success": True, "token": "abc"}

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

    monkeypatch.setattr(admin_module, "AsyncSession", FakeSession)

    status_code, payload, response_text = asyncio.run(
        admin_module._sync_json_http_request(
            method="POST",
            url="https://example.com/api/v1/custom-score",
            headers={"Authorization": "Bearer token"},
            payload={"website_url": "https://example.com"},
            timeout=15,
        )
    )

    assert status_code == 200
    assert payload == {"success": True, "token": "abc"}
    assert response_text == '{"success": true, "token": "abc"}'
    assert calls == [
        {
            "method": "POST",
            "url": "https://example.com/api/v1/custom-score",
            "kwargs": {
                "headers": {
                    "Authorization": "Bearer token",
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                },
                "timeout": 15,
                "impersonate": "chrome120",
                "json": {"website_url": "https://example.com"},
            },
        }
    ]
