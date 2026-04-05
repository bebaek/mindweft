import json
from datetime import datetime, timedelta, timezone

from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from app import auth as auth_module
from app.llm import LLMAdapter, MockLLMAdapter
from app.main import create_app
from app.models import LLMResponse, Message, MessageRole, ToolCall
from app.tools import build_local_tool_registry

AUTH_HEADERS = {
    "X-Minigent-User-Id": "user-1",
    "X-Minigent-Tenant-Id": "tenant-1",
}

OTHER_TENANT_HEADERS = {
    "X-Minigent-User-Id": "user-2",
    "X-Minigent-Tenant-Id": "tenant-2",
}
ADMIN_HEADERS = {
    "X-Minigent-User-Id": "admin-user",
    "X-Minigent-Tenant-Id": "admin-tenant",
    "X-Minigent-Admin": "true",
}

TOKEN_HEADERS = {"Authorization": "Bearer token-1"}
OTHER_TOKEN_HEADERS = {"Authorization": "Bearer token-2"}


def _jwt_claims(*, issuer: str = "https://issuer.example", audience: str = "minigent-api") -> dict[str, object]:
    return {
        "sub": "jwt-user",
        "tenant_id": "jwt-tenant",
        "is_admin": True,
        "iss": issuer,
        "aud": audience,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }


def test_thread_lifecycle_endpoints() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    config_response = client.get("/config")
    assert config_response.status_code == 200
    assert config_response.json()["llm"]["provider"] == "mock"

    create_response = client.post("/threads", headers=AUTH_HEADERS)
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]

    add_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello"},
        headers=AUTH_HEADERS,
    )
    assert add_response.status_code == 200
    assert add_response.json()["role"] == MessageRole.USER
    assert add_response.json()["created_by"] == "user-1"

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "Mock reply: hello"}

    messages_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert [message["role"] for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]

    delete_response = client.delete(f"/threads/{thread_id}", headers=AUTH_HEADERS)
    assert delete_response.status_code == 204

    missing_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert missing_response.status_code == 404


def test_run_endpoint_handles_tool_call_flow() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from api"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert run_response.json() == {"reply": 'Tool result: {"echo": "hello from api"}'}

    messages = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS).json()
    assert [message["role"] for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]


def test_run_endpoint_returns_reply_when_tool_fails() -> None:
    class ToolFailingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
            if messages and messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content=f"Tool result: {messages[-1].content}")
            return LLMResponse(
                tool_call=ToolCall(
                    id="call-fetch",
                    name="fetch_url",
                    arguments={"url": "https://example.com/missing"},
                )
            )

        def describe(self) -> dict[str, object]:
            return {
                "provider": "test",
                "model": None,
                "base_url": None,
                "headers": [],
                "adapter": "ToolFailingLLM",
            }

    class FailingRegistry:
        def specs(self) -> list[object]:
            return []

        def mcp_servers(self) -> list[dict[str, object]]:
            return []

        async def execute(self, name: str, arguments: dict[str, object]) -> object:
            raise HTTPException(status_code=502, detail="fetch_url failed with status 404")

    client = TestClient(create_app(llm_adapter=ToolFailingLLM(), tool_registry=FailingRegistry()))  # type: ignore[arg-type]
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "is austin airport open now"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert run_response.json() == {
        "reply": (
            'Tool result: {"error": {"tool_name": "fetch_url", "status_code": 502, '
            '"detail": "fetch_url failed with status 404"}}'
        )
    }


def test_thread_endpoints_require_authenticated_principal() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.post("/threads")

    assert response.status_code == 401
    assert "Missing authenticated principal" in response.json()["detail"]


def test_thread_endpoints_hide_cross_tenant_access() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    response = client.get(f"/threads/{thread_id}/messages", headers=OTHER_TENANT_HEADERS)

    assert response.status_code == 404


def test_thread_endpoints_accept_bearer_token_auth(monkeypatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_AUTH_MODE",
        "static-tokens",
    )
    monkeypatch.setenv(
        "MINIGENT_AUTH_TOKENS",
        (
            '{"token-1":{"user_id":"user-1","tenant_id":"tenant-1"},'
            '"token-2":{"user_id":"user-2","tenant_id":"tenant-2"}}'
        ),
    )
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    create_response = client.post("/threads", headers=TOKEN_HEADERS)
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]

    add_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello with token"},
        headers=TOKEN_HEADERS,
    )
    assert add_response.status_code == 200
    assert add_response.json()["created_by"] == "user-1"

    cross_tenant = client.get(f"/threads/{thread_id}/messages", headers=OTHER_TOKEN_HEADERS)
    assert cross_tenant.status_code == 404


def test_thread_endpoints_require_bearer_token_when_tokens_are_configured(monkeypatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_AUTH_MODE",
        "static-tokens",
    )
    monkeypatch.setenv(
        "MINIGENT_AUTH_TOKENS",
        '{"token-1":{"user_id":"user-1","tenant_id":"tenant-1"}}',
    )
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.post("/threads", headers=AUTH_HEADERS)

    assert response.status_code == 401
    assert "Missing bearer token" in response.json()["detail"]


def test_thread_endpoints_reject_invalid_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_AUTH_MODE",
        "static-tokens",
    )
    monkeypatch.setenv(
        "MINIGENT_AUTH_TOKENS",
        '{"token-1":{"user_id":"user-1","tenant_id":"tenant-1"}}',
    )
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.post("/threads", headers={"Authorization": "Bearer bad-token"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bearer token"


def test_thread_endpoints_accept_hs256_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_secret = "test-secret-0123456789abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("MINIGENT_AUTH_MODE", "jwt")
    monkeypatch.setenv("MINIGENT_JWT_ALGORITHMS", '["HS256"]')
    monkeypatch.setenv("MINIGENT_JWT_SHARED_SECRET", shared_secret)
    monkeypatch.setenv("MINIGENT_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("MINIGENT_JWT_AUDIENCE", "minigent-api")

    token = jwt.encode(_jwt_claims(), shared_secret, algorithm="HS256")
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    create_response = client.post("/threads", headers={"Authorization": f"Bearer {token}"})

    assert create_response.status_code == 200


def test_thread_endpoints_reject_jwt_with_wrong_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_secret = "test-secret-0123456789abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("MINIGENT_AUTH_MODE", "jwt")
    monkeypatch.setenv("MINIGENT_JWT_ALGORITHMS", '["HS256"]')
    monkeypatch.setenv("MINIGENT_JWT_SHARED_SECRET", shared_secret)
    monkeypatch.setenv("MINIGENT_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("MINIGENT_JWT_AUDIENCE", "minigent-api")

    token = jwt.encode(
        _jwt_claims(issuer="https://other-issuer.example"),
        shared_secret,
        algorithm="HS256",
    )
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.post("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert "Invalid JWT" in response.json()["detail"]


def test_thread_endpoints_accept_rs256_jwt_via_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = "test-key"

    async def fake_fetch_jwks_document(url: str) -> dict[str, object]:
        assert url == "https://issuer.example/.well-known/jwks.json"
        return {"keys": [jwk]}

    monkeypatch.setenv("MINIGENT_AUTH_MODE", "jwt")
    monkeypatch.setenv("MINIGENT_JWT_ALGORITHMS", '["RS256"]')
    monkeypatch.setenv("MINIGENT_JWT_JWKS_URL", "https://issuer.example/.well-known/jwks.json")
    monkeypatch.setenv("MINIGENT_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setenv("MINIGENT_JWT_AUDIENCE", "minigent-api")
    monkeypatch.setattr(auth_module, "_fetch_jwks_document", fake_fetch_jwks_document)
    auth_module._JWKS_CACHE.clear()

    token = jwt.encode(
        _jwt_claims(),
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    create_response = client.post("/threads", headers={"Authorization": f"Bearer {token}"})

    assert create_response.status_code == 200


def test_tenant_execution_config_limits_tools_per_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "tools": {"allowed_local_tools": ["echo"]},
                },
                "tenant-2": {
                    "llm": {"provider": "mock"},
                    "tools": {"allowed_local_tools": ["current_time"]},
                },
            }
        ),
    )
    client = TestClient(create_app())

    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from tenant one"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": 'Tool result: {"echo": "hello from tenant one"}'}

    other_thread_id = client.post("/threads", headers=OTHER_TENANT_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{other_thread_id}/messages",
        json={"content": "/tool echo hello from tenant two"},
        headers=OTHER_TENANT_HEADERS,
    )

    other_run = client.post(f"/threads/{other_thread_id}/run", headers=OTHER_TENANT_HEADERS)

    assert other_run.status_code == 200
    assert other_run.json() == {"reply": "Mock reply: /tool echo hello from tenant two"}


def test_tenant_execution_config_rejects_missing_tenant_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "tools": {"allowed_local_tools": ["echo"]},
                }
            }
        ),
    )
    client = TestClient(create_app())

    thread_id = client.post("/threads", headers=OTHER_TENANT_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello"},
        headers=OTHER_TENANT_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=OTHER_TENANT_HEADERS)

    assert run_response.status_code == 403
    assert run_response.json()["detail"] == "Tenant 'tenant-2' has no execution configuration"


def test_admin_api_requires_admin_access(tmp_path: Path) -> None:
    client = TestClient(create_app(admin_store=_sqlite_store(tmp_path)))

    response = client.get("/admin/tenants", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"


def test_admin_api_can_manage_tenant_execution_config_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(admin_store=_sqlite_store(tmp_path)))
    payload = {
        "config": {
            "llm": {
                "provider": "mock",
                "api_key": "secret-key",
            },
            "tools": {
                "allowed_local_tools": ["echo"],
                "mcp_servers": [
                    {
                        "name": "demo",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer token"},
                    }
                ],
            },
        }
    }

    put_response = client.put(
        "/admin/tenants/tenant-1/execution-config",
        json=payload,
        headers=ADMIN_HEADERS,
    )

    assert put_response.status_code == 200
    assert put_response.json()["config"]["llm"]["api_key"] == "<redacted>"
    assert put_response.json()["config"]["llm"]["has_api_key"] is True
    assert (
        put_response.json()["config"]["tools"]["mcp_servers"][0]["headers"]["Authorization"]
        == "<redacted>"
    )

    list_response = client.get("/admin/tenants", headers=ADMIN_HEADERS)
    assert list_response.status_code == 200
    assert list_response.json()["tenants"] == ["tenant-1"]

    get_response = client.get(
        "/admin/tenants/tenant-1/execution-config",
        headers=ADMIN_HEADERS,
    )
    assert get_response.status_code == 200
    assert get_response.json()["config"]["llm"]["api_key"] == "<redacted>"


def test_admin_api_updates_runtime_after_config_change(tmp_path: Path) -> None:
    client = TestClient(create_app(admin_store=_sqlite_store(tmp_path)))

    first_config = {
        "config": {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["echo"]},
        }
    }
    second_config = {
        "config": {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["current_time"]},
        }
    }

    client.put(
        "/admin/tenants/tenant-1/execution-config",
        json=first_config,
        headers=ADMIN_HEADERS,
    )

    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello before update"},
        headers=AUTH_HEADERS,
    )
    first_run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert first_run.status_code == 200
    assert first_run.json() == {"reply": 'Tool result: {"echo": "hello before update"}'}

    client.put(
        "/admin/tenants/tenant-1/execution-config",
        json=second_config,
        headers=ADMIN_HEADERS,
    )

    next_thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{next_thread_id}/messages",
        json={"content": "/tool echo hello after update"},
        headers=AUTH_HEADERS,
    )
    second_run = client.post(f"/threads/{next_thread_id}/run", headers=AUTH_HEADERS)
    assert second_run.status_code == 200
    assert second_run.json() == {"reply": "Mock reply: /tool echo hello after update"}

    delete_response = client.delete(
        "/admin/tenants/tenant-1/execution-config",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 204


def _sqlite_store(tmp_path: Path):
    from app.admin_store import SQLiteTenantConfigStore

    return SQLiteTenantConfigStore(str(tmp_path / "tenant-configs.db"))
