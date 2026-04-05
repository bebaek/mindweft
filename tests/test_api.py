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
from app.mcp import MCPServerInfo
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

    create_response = client.post("/threads", headers=OTHER_TENANT_HEADERS)

    assert create_response.status_code == 403
    assert create_response.json()["detail"] == "Tenant 'tenant-2' has no execution configuration"


def test_create_thread_can_select_skill_and_skill_narrows_runtime_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "tools": {"allowed_local_tools": ["echo", "calculator"]},
                    "skills": {
                        "items": [
                            {
                                "name": "math",
                                "system_prompt": "Prefer exact arithmetic.",
                                "allowed_local_tools": ["calculator"],
                            }
                        ]
                    },
                }
            }
        ),
    )
    client = TestClient(create_app())

    create_response = client.post("/threads", json={"skill_name": "math"}, headers=AUTH_HEADERS)
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from restricted skill"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "Mock reply: /tool echo hello from restricted skill"}


def test_create_thread_uses_default_skill_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "tools": {"allowed_local_tools": ["echo", "calculator"]},
                    "skills": {
                        "default_skill": "math",
                        "items": [
                            {
                                "name": "math",
                                "system_prompt": "Prefer exact arithmetic.",
                                "allowed_local_tools": ["calculator"],
                            }
                        ],
                    },
                }
            }
        ),
    )
    client = TestClient(create_app())

    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from default skill"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "Mock reply: /tool echo hello from default skill"}


def test_create_thread_rejects_unknown_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "tools": {"allowed_local_tools": ["echo"]},
                    "skills": {
                        "items": [
                            {
                                "name": "support",
                                "system_prompt": "Answer concisely.",
                                "allowed_local_tools": ["echo"],
                            }
                        ]
                    },
                }
            }
        ),
    )
    client = TestClient(create_app())

    response = client.post("/threads", json={"skill_name": "missing"}, headers=AUTH_HEADERS)

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown skill 'missing' for tenant 'tenant-1'"


def test_admin_api_requires_admin_access(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store")
    )

    response = client.get("/admin/tenants", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"


def test_admin_api_validates_tenant_execution_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeMCPClient:
        def __init__(self, config, transport=None, timeout=15.0) -> None:
            _ = transport
            _ = timeout
            self._config = config

        async def list_tools(self) -> list[object]:
            return [type("Spec", (), {"name": "demo.echo"})()]

        def server_info(self) -> MCPServerInfo:
            return MCPServerInfo(
                name=self._config.name,
                url=self._config.url,
                protocol_version=self._config.protocol_version,
                session_id="session-123",
                server_name="demo-server",
                server_version="1.2.3",
            )

    monkeypatch.setattr("app.execution.MCPHTTPClient", FakeMCPClient)
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store")
    )

    response = client.post(
        "/admin/tenants/tenant-1/execution-config/validate",
        json={
            "config": {
                "llm": {
                    "provider": "openai-compatible",
                    "base_url": "https://example.com/v1",
                    "model": "gpt-test",
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
        },
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "config_shape": {"ok": True, "errors": []},
        "llm": {
            "ok": True,
            "provider": "openai-compatible",
            "model": "gpt-test",
            "base_url": "https://example.com/v1",
            "errors": [],
        },
        "tools": {
            "ok": True,
            "errors": [],
            "local_tools": ["echo"],
            "unknown_local_tools": [],
            "mcp_servers": [
                {
                    "name": "demo",
                    "url": "https://example.com/mcp",
                    "ok": True,
                    "error": None,
                    "tool_count": 1,
                    "protocol_version": "2025-11-25",
                    "session": True,
                    "server_name": "demo-server",
                    "server_version": "1.2.3",
                }
            ],
        },
    }

    get_response = client.get(
        "/admin/tenants/tenant-1/execution-config",
        headers=ADMIN_HEADERS,
    )
    assert get_response.status_code == 404


def test_admin_api_validation_reports_tool_policy_and_mcp_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FailingMCPClient:
        def __init__(self, config, transport=None, timeout=15.0) -> None:
            _ = transport
            _ = timeout
            self._config = config

        async def list_tools(self) -> list[object]:
            raise HTTPException(
                status_code=502,
                detail=f"MCP server '{self._config.name}' request failed: boom",
            )

    monkeypatch.setattr("app.execution.MCPHTTPClient", FailingMCPClient)
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store")
    )

    response = client.post(
        "/admin/tenants/tenant-1/execution-config/validate",
        json={
            "config": {
                "llm": {
                    "provider": "openai",
                    "model": "gpt-test",
                },
                "tools": {
                    "allowed_local_tools": ["echo", "does_not_exist"],
                    "mcp_servers": [
                        {
                            "name": "demo",
                            "url": "https://example.com/mcp",
                            "headers": {},
                        }
                    ],
                },
            }
        },
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["config_shape"]["ok"] is False
    assert body["config_shape"]["errors"] == [
        "Tenant 'tenant-1' allowed_local_tools references unknown local tools: does_not_exist"
    ]
    assert body["llm"] == {
        "ok": False,
        "provider": "openai",
        "model": "gpt-test",
        "base_url": "https://api.openai.com/v1",
        "errors": ["Tenant LLM provider 'openai' requires api_key"],
    }
    assert body["tools"]["ok"] is False
    assert body["tools"]["unknown_local_tools"] == ["does_not_exist"]
    assert body["tools"]["errors"] == ["MCP server 'demo' request failed: boom"]
    assert body["tools"]["mcp_servers"][0]["ok"] is False
    assert body["tools"]["mcp_servers"][0]["error"] == "MCP server 'demo' request failed: boom"


def test_admin_api_can_manage_tenant_execution_config_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store")
    )
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
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store")
    )

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


def test_admin_store_encrypts_secrets_at_rest(tmp_path: Path) -> None:
    db_path = tmp_path / "tenant-configs.db"
    client = TestClient(
        create_app(
            admin_store=_sqlite_store(tmp_path, encryption_key="test-admin-encryption-key"),
            tenant_config_source="store",
        )
    )

    response = client.put(
        "/admin/tenants/tenant-1/execution-config",
        json={
            "config": {
                "llm": {"provider": "mock", "api_key": "super-secret"},
                "tools": {
                    "mcp_servers": [
                        {
                            "name": "demo",
                            "url": "https://example.com/mcp",
                            "headers": {"Authorization": "Bearer secret-token"},
                        }
                    ]
                },
            }
        },
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200

    import sqlite3

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT config_json FROM tenant_execution_configs WHERE tenant_id = ?",
            ("tenant-1",),
        ).fetchone()

    assert row is not None
    stored_json = str(row[0])
    assert "super-secret" not in stored_json
    assert "secret-token" not in stored_json

    get_response = client.get(
        "/admin/tenants/tenant-1/execution-config",
        headers=ADMIN_HEADERS,
    )
    assert get_response.status_code == 200
    assert get_response.json()["config"]["llm"]["api_key"] == "<redacted>"


def test_store_with_defaults_uses_store_default_before_failing(
    tmp_path: Path,
) -> None:
    client = TestClient(
        create_app(
            admin_store=_sqlite_store(tmp_path),
            tenant_config_source="store-with-defaults",
        )
    )
    default_config = {
        "config": {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["echo"]},
        }
    }
    client.put(
        "/admin/tenants/*/execution-config",
        json=default_config,
        headers=ADMIN_HEADERS,
    )

    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from default"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": 'Tool result: {"echo": "hello from default"}'}


def test_store_mode_fails_closed_without_tenant_config(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store")
    )

    create_response = client.post("/threads", headers=AUTH_HEADERS)

    assert create_response.status_code == 403
    assert create_response.json()["detail"] == "Tenant 'tenant-1' has no execution configuration"


def test_store_mode_requires_encryption_key_when_using_env_admin_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MINIGENT_ADMIN_DB_PATH", str(tmp_path / "tenant-configs.db"))
    monkeypatch.delenv("MINIGENT_ADMIN_ENCRYPTION_KEY", raising=False)

    with pytest.raises(RuntimeError, match="MINIGENT_ADMIN_ENCRYPTION_KEY"):
        create_app(tenant_config_source="store")


def test_store_with_defaults_requires_encryption_key_when_using_env_admin_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MINIGENT_ADMIN_DB_PATH", str(tmp_path / "tenant-configs.db"))
    monkeypatch.delenv("MINIGENT_ADMIN_ENCRYPTION_KEY", raising=False)

    with pytest.raises(RuntimeError, match="MINIGENT_ADMIN_ENCRYPTION_KEY"):
        create_app(tenant_config_source="store-with-defaults")


def _sqlite_store(tmp_path: Path, *, encryption_key: str | None = None):
    from app.admin_store import SQLiteTenantConfigStore

    return SQLiteTenantConfigStore(
        str(tmp_path / "tenant-configs.db"),
        encryption_key=encryption_key,
    )
