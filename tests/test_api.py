import asyncio
import json
import logging
import sqlite3
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from app import auth as auth_module
from app import execution as execution_module
from app import store as store_module
from app.admin_api import (
    AdminStoreSettings,
    admin_encryption_key_from_env,
    admin_store_path_from_env,
    admin_store_settings_from_env,
)
from app.agent_backends import PeerBackendSettings, _sanitize_peer_task_event
from app.config import load_environment
from app.execution import (
    InMemoryTenantExecutionResolver,
    build_execution_resolver_from_env,
    interpolate_tenant_execution_env_placeholders,
    parse_tenant_execution_config,
)
from app.llm import LLMAdapter, MockLLMAdapter, OpenAICompatibleAdapter
from app.main import DEFAULT_IMAGE_INPUT_MAX_BYTES, ImageInputSettings, create_app
from app.mcp import MCPServerInfo
from app.mcp_broker import MINIGENT_MCP_BROKER_TOKEN_ENV, MINIGENT_MCP_BROKER_URL_ENV
from app.models import (
    AuditRecord,
    LLMResponse,
    Message,
    MessageRole,
    Principal,
    TenantStatus,
    TenantUser,
    TenantUserRole,
    TenantUserStatus,
    ThreadStatus,
    ToolCall,
    ToolSpec,
)
from app.peer_agents import PeerAgentRegistry, parse_peer_agent_configs
from app.runtime import AgentRuntime
from app.store import InMemoryThreadStore, SQLiteThreadStore
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


def test_peer_backend_settings_from_env_mapping_uses_defaults() -> None:
    settings = PeerBackendSettings.from_env({})

    assert settings.mcp_broker_base_url == "http://127.0.0.1:8000"
    assert settings.safe_tool_arg_fields == {
        "read": ("path", "limit", "offset"),
        "grep": ("pattern", "path", "glob", "limit"),
        "find": ("pattern", "path", "limit"),
        "ls": ("path", "limit"),
    }


def test_peer_backend_settings_from_env_mapping_parses_values() -> None:
    settings = PeerBackendSettings.from_env(
        {
            "MINIGENT_MCP_BROKER_BASE_URL": "http://127.0.0.1:9000/",
            "MINIGENT_PEER_TOOL_ARG_ALLOWLIST": '{"read":["path"]}',
        }
    )

    assert settings.mcp_broker_base_url == "http://127.0.0.1:9000"
    assert settings.safe_tool_arg_fields == {"read": ("path",)}


def test_admin_store_settings_from_env_mapping_uses_defaults() -> None:
    assert AdminStoreSettings.from_env({}) == AdminStoreSettings(
        db_path=None,
        encryption_key=None,
    )


def test_admin_store_settings_from_env_mapping_parses_values() -> None:
    assert AdminStoreSettings.from_env(
        {
            "MINIGENT_ADMIN_DB_PATH": " .data/admin.db ",
            "MINIGENT_ADMIN_ENCRYPTION_KEY": " secret-key ",
        }
    ) == AdminStoreSettings(
        db_path=".data/admin.db",
        encryption_key="secret-key",
    )


def test_admin_store_settings_from_env_mapping_treats_blank_as_none() -> None:
    assert AdminStoreSettings.from_env(
        {
            "MINIGENT_ADMIN_DB_PATH": " ",
            "MINIGENT_ADMIN_ENCRYPTION_KEY": "\t",
        }
    ) == AdminStoreSettings(
        db_path=None,
        encryption_key=None,
    )


def test_admin_store_settings_from_env_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_ADMIN_DB_PATH", ".data/admin.db")
    monkeypatch.setenv("MINIGENT_ADMIN_ENCRYPTION_KEY", "secret-key")

    assert admin_store_settings_from_env() == AdminStoreSettings(
        db_path=".data/admin.db",
        encryption_key="secret-key",
    )
    assert admin_store_path_from_env() == ".data/admin.db"
    assert admin_encryption_key_from_env() == "secret-key"


def _jwt_claims(
    *, issuer: str = "https://issuer.example", audience: str = "minigent-api"
) -> dict[str, object]:
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
        json={"content": "hello", "metadata": {"raw_user_prompt": "hello"}},
        headers=AUTH_HEADERS,
    )
    assert add_response.status_code == 200
    assert add_response.json()["role"] == MessageRole.USER
    assert add_response.json()["created_by"] == "user-1"
    assert add_response.json()["metadata"] == {"raw_user_prompt": "hello"}

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "Mock reply: hello"}

    messages_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert [message["role"] for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[0]["metadata"] == {"raw_user_prompt": "hello"}

    delete_response = client.delete(f"/threads/{thread_id}", headers=AUTH_HEADERS)
    assert delete_response.status_code == 204

    missing_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert missing_response.status_code == 404


def test_config_export_does_not_collect_coding_runner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_CODING_MCP_SERVER_SPECS",
        json.dumps([{"name": "web-fetch", "command": ["uvx", "mcp-server-fetch"]}]),
    )
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.get("/config?export=true")

    assert response.status_code == 200
    export = response.json()["unified_config_export"]
    assert "coding" not in export


def test_image_input_settings_from_env_mapping_uses_defaults() -> None:
    settings = ImageInputSettings.from_env({})

    assert settings == ImageInputSettings(
        enabled=False,
        max_bytes=DEFAULT_IMAGE_INPUT_MAX_BYTES,
        allowed_mime_types=frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
    )


def test_image_input_settings_from_env_mapping_parses_values() -> None:
    settings = ImageInputSettings.from_env(
        {
            "MINIGENT_IMAGE_INPUT_ENABLED": "yes",
            "MINIGENT_IMAGE_INPUT_MAX_BYTES": "1234",
            "MINIGENT_IMAGE_INPUT_ALLOWED_MIME_TYPES": "image/png, image/avif",
        }
    )

    assert settings == ImageInputSettings(
        enabled=True,
        max_bytes=1234,
        allowed_mime_types=frozenset({"image/png", "image/avif"}),
    )


def test_image_input_settings_from_env_mapping_rejects_invalid_values() -> None:
    with pytest.raises(RuntimeError) as exc_info:
        ImageInputSettings.from_env(
            {
                "MINIGENT_IMAGE_INPUT_ENABLED": "true",
                "MINIGENT_IMAGE_INPUT_MAX_BYTES": "0",
            }
        )

    assert str(exc_info.value) == "MINIGENT_IMAGE_INPUT_MAX_BYTES must be a positive integer"


def test_add_message_rejects_image_when_disabled() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    create_response = client.post("/threads", headers=AUTH_HEADERS)
    thread_id = create_response.json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={
            "content": "describe it",
            "parts": [
                {"type": "text", "text": "describe it"},
                {"type": "image", "mime_type": "image/png", "data": "aGk="},
            ],
        },
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "image input is disabled"


def test_add_message_accepts_image_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_IMAGE_INPUT_ENABLED", "true")
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    create_response = client.post("/threads", headers=AUTH_HEADERS)
    thread_id = create_response.json()["thread_id"]

    response = client.post(
        f"/threads/{thread_id}/messages",
        json={
            "content": "describe it",
            "parts": [
                {"type": "text", "text": "describe it"},
                {"type": "image", "mime_type": "image/png", "data": "aGk="},
            ],
        },
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["parts"][1]["mime_type"] == "image/png"


def test_sqlite_thread_store_persists_threads_and_messages(tmp_path: Path) -> None:
    db_path = tmp_path / "threads.db"
    first_client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=SQLiteThreadStore(db_path),
        )
    )
    thread_id = first_client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    first_client.post(
        f"/threads/{thread_id}/messages",
        json={
            "content": "hello before restart",
            "metadata": {"raw_user_prompt": "hello before restart"},
        },
        headers=AUTH_HEADERS,
    )
    run_response = first_client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200

    second_client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=SQLiteThreadStore(db_path),
        )
    )

    messages_response = second_client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)

    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert [message["role"] for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[0]["content"] == "hello before restart"
    assert messages[0]["metadata"] == {"raw_user_prompt": "hello before restart"}
    assert messages[1]["content"] == "Mock reply: hello before restart"


def test_sqlite_thread_store_persists_structured_audit_records(tmp_path: Path) -> None:
    db_path = tmp_path / "threads.db"
    store = SQLiteThreadStore(db_path)
    store.append_audit_record(
        AuditRecord(
            tenant_id="tenant-1",
            actor_user_id="admin-user",
            action="tenants.update",
            affected_count=1,
            resource_type="tenant",
            resource_id="tenant-1",
            old_values={"status": "active"},
            new_values={"status": "suspended"},
            metadata={"reason": "test"},
        )
    )

    reloaded = SQLiteThreadStore(db_path).list_audit_records("tenant-1")

    assert len(reloaded) == 1
    assert reloaded[0].resource_type == "tenant"
    assert reloaded[0].resource_id == "tenant-1"
    assert reloaded[0].old_values == {"status": "active"}
    assert reloaded[0].new_values == {"status": "suspended"}
    assert reloaded[0].metadata == {"reason": "test"}


def test_sqlite_thread_store_migrates_audit_record_columns(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "threads.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE audit_records (
                audit_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                payload TEXT NOT NULL
            );
            """
        )

    SQLiteThreadStore(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audit_records)")}
    assert {
        "resource_type",
        "resource_id",
        "old_values_json",
        "new_values_json",
        "metadata_json",
    }.issubset(columns)


def test_sqlite_thread_store_closes_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = store_module.sqlite3.connect
    close_count = 0
    connect_count = 0

    class TrackedConnection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal connect_count
            connect_count += 1
            self._connection = real_connect(*args, **kwargs)

        def __enter__(self) -> object:
            return self._connection.__enter__()

        def __exit__(self, *args: object) -> object:
            return self._connection.__exit__(*args)

        def close(self) -> None:
            nonlocal close_count
            close_count += 1
            self._connection.close()

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

    monkeypatch.setattr(store_module.sqlite3, "connect", TrackedConnection)

    thread_store = SQLiteThreadStore(tmp_path / "threads.db")
    thread = thread_store.create_thread("tenant-1")
    thread_store.append_message(
        "tenant-1", Message(role=MessageRole.USER, thread_id=thread.thread_id, content="hello")
    )
    thread_store.list_messages("tenant-1", thread.thread_id)
    thread_store.get_thread_context("tenant-1", thread.thread_id)

    assert close_count == connect_count


def test_web_client_static_files_are_served() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.get("/web/")

    assert response.status_code == 200
    assert "Minigent Web Client" in response.text
    assert "./app.js" in response.text


def test_thread_manual_compact_endpoint_summarizes_older_messages() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    for index in range(10):
        response = client.post(
            f"/threads/{thread_id}/messages",
            json={"content": f"message-{index}"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200

    compact_response = client.post(f"/threads/{thread_id}/compact", headers=AUTH_HEADERS)

    assert compact_response.status_code == 200
    compacted = compact_response.json()
    assert compacted["compacted_message_count"] == 2
    assert compacted["message_count"] == 8
    assert "User: message-0" in compacted["summary"]
    assert "User: message-1" in compacted["summary"]
    raw_context = client.get(f"/threads/{thread_id}/context/raw", headers=AUTH_HEADERS).json()
    assert raw_context["summary"] == compacted["summary"]
    assert [message["content"] for message in raw_context["messages"]] == [
        f"message-{index}" for index in range(2, 10)
    ]


def test_create_app_uses_runtime_max_iterations_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_MAX_ITERATIONS", "24")

    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())

    assert app.state.runtime._max_iterations == 24
    assert app.state.runtime_settings.max_iterations == 24


def test_app_startup_logs_available_internal_tools(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(allowed_tools=["echo", "current_time"]),
    )

    with caplog.at_level(logging.INFO, logger="app.main"):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    assert (
        "available_internal_tools tenant_id=* tools=['current_time', 'echo'] count=2" in caplog.text
    )


def test_peer_agent_endpoints_list_and_fetch_agent_card() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://codex-agent.test/agent-card"
        return httpx.Response(
            200,
            json={
                "name": "codex-coding-agent",
                "version": "0.1.0",
                "capabilities": ["repository analysis"],
                "side_effects": ["runs local commands"],
            },
        )

    registry = PeerAgentRegistry(
        parse_peer_agent_configs(
            [
                {
                    "name": "codex",
                    "base_url": "http://codex-agent.test",
                    "description": "Local coding-agent wrapper",
                }
            ]
        ),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            peer_agent_registry=registry,
        )
    )

    list_response = client.get("/peer-agents", headers=AUTH_HEADERS)
    assert list_response.status_code == 200
    assert list_response.json() == {
        "agents": [
            {
                "name": "codex",
                "base_url": "http://codex-agent.test",
                "description": "Local coding-agent wrapper",
                "agent_card_name": "codex-coding-agent",
                "version": "0.1.0",
                "capabilities": ["repository analysis"],
                "side_effects": ["runs local commands"],
                "links": {
                    "agent_card": "/peer-agents/codex/agent-card",
                    "tasks": "/peer-agents/codex/tasks",
                },
            }
        ]
    }

    card_response = client.get("/peer-agents/codex/agent-card", headers=AUTH_HEADERS)
    assert card_response.status_code == 200
    assert card_response.json() == {
        "name": "codex-coding-agent",
        "version": "0.1.0",
        "capabilities": ["repository analysis"],
        "side_effects": ["runs local commands"],
    }


def test_peer_agent_task_proxy_endpoints() -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, payload))
        if request.method == "POST" and request.url.path == "/tasks":
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if request.method == "GET" and request.url.path == "/tasks/task_123":
            return httpx.Response(200, json={"task_id": "task_123", "status": "completed"})
        return httpx.Response(404, json={"detail": "missing"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            peer_agent_registry=registry,
        )
    )

    create_response = client.post(
        "/peer-agents/codex/tasks",
        headers=AUTH_HEADERS,
        json={"cwd": "/workspace/project", "prompt": "summarize this repo"},
    )
    assert create_response.status_code == 200
    assert create_response.json() == {"task_id": "task_123", "status": "running"}

    task_response = client.get("/peer-agents/codex/tasks/task_123", headers=AUTH_HEADERS)
    assert task_response.status_code == 200
    assert task_response.json() == {"task_id": "task_123", "status": "completed"}

    assert requests == [
        (
            "POST",
            "/tasks",
            {"cwd": "/workspace/project", "prompt": "summarize this repo"},
        ),
        ("GET", "/tasks/task_123", None),
    ]


def test_peer_agent_cancel_task_proxy_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://codex-agent.test/tasks/task_123/cancel"
        return httpx.Response(200, json={"task_id": "task_123", "status": "canceled"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            peer_agent_registry=registry,
        )
    )

    cancel_response = client.post(
        "/peer-agents/codex/tasks/task_123/cancel",
        headers=AUTH_HEADERS,
    )

    assert cancel_response.status_code == 200
    assert cancel_response.json() == {"task_id": "task_123", "status": "canceled"}


def test_peer_agent_events_and_artifact_proxy_endpoints() -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.method == "GET" and str(request.url).endswith("/tasks/task_123/events?after=0"):
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "next_index": 2,
                    "events": [{"index": 1, "type": "message.completed"}],
                },
            )
        if request.method == "GET" and request.url.path == "/tasks/task_123/artifacts/final-output":
            return httpx.Response(200, text="final text", headers={"content-type": "text/plain"})
        return httpx.Response(404, json={"detail": "missing"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            peer_agent_registry=registry,
        )
    )

    events_response = client.get(
        "/peer-agents/codex/tasks/task_123/events?after=0",
        headers=AUTH_HEADERS,
    )
    assert events_response.status_code == 200
    assert events_response.json() == {
        "task_id": "task_123",
        "next_index": 2,
        "events": [{"index": 1, "type": "message.completed"}],
    }

    artifact_response = client.get(
        "/peer-agents/codex/tasks/task_123/artifacts/final-output",
        headers=AUTH_HEADERS,
    )
    assert artifact_response.status_code == 200
    assert artifact_response.text == "final text"
    assert artifact_response.headers["content-type"].startswith("text/plain")

    assert requests == [
        ("GET", "http://codex-agent.test/tasks/task_123/events?after=0"),
        ("GET", "http://codex-agent.test/tasks/task_123/artifacts/final-output"),
    ]


def test_run_stream_endpoint_emits_ndjson_events() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello stream"},
        headers=AUTH_HEADERS,
    )

    with client.stream(
        "POST", f"/threads/{thread_id}/run/stream", headers=AUTH_HEADERS
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert [event["type"] for event in events] == [
        "run.started",
        "llm.request",
        "assistant.message",
        "run.completed",
    ]
    assert all(event["thread_id"] == thread_id for event in events)
    assert events[0]["thread_context"]["estimated"] is True
    assert events[0]["thread_context"]["total_tokens"] > 0
    assert events[1]["iteration"] == 1
    assert events[1]["message_count"] >= 2
    assert events[1]["tool_count"] >= 1
    assert events[2]["content"] == "Mock reply: hello stream"
    assert events[3]["thread_context"]["total_tokens"] > events[0]["thread_context"]["total_tokens"]


def test_run_stream_endpoint_emits_llm_usage() -> None:
    class UsageLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            del messages, tools
            return LLMResponse(
                content="usage reply",
                usage={
                    "prompt_tokens": 100,
                    "input_tokens": 100,
                    "completion_tokens": 12,
                    "output_tokens": 12,
                    "total_tokens": 112,
                    "cache_read_tokens": 80,
                },
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "usage-test"}

    client = TestClient(
        create_app(llm_adapter=UsageLLM(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello usage"},
        headers=AUTH_HEADERS,
    )

    with client.stream(
        "POST", f"/threads/{thread_id}/run/stream", headers=AUTH_HEADERS
    ) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert [event["type"] for event in events] == [
        "run.started",
        "llm.request",
        "llm.response",
        "assistant.message",
        "run.completed",
    ]
    assert events[2]["usage"] == {
        "prompt_tokens": 100,
        "input_tokens": 100,
        "completion_tokens": 12,
        "output_tokens": 12,
        "total_tokens": 112,
        "cache_read_tokens": 80,
    }


def test_run_stream_endpoint_emits_tool_events() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from stream"},
        headers=AUTH_HEADERS,
    )

    with client.stream(
        "POST", f"/threads/{thread_id}/run/stream", headers=AUTH_HEADERS
    ) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert [event["type"] for event in events] == [
        "run.started",
        "llm.request",
        "tool.call",
        "tool.result",
        "llm.request",
        "assistant.message",
        "run.completed",
    ]
    assert events[2]["name"] == "echo"
    assert events[2]["arguments"] == {"text": "hello from stream"}
    assert events[3]["name"] == "echo"
    assert events[3]["is_error"] is False
    assert events[3]["result"] == {"echo": "hello from stream"}
    assert events[5]["content"] == 'Tool result: {"echo": "hello from stream"}'


def test_run_stream_endpoint_emits_peer_task_events() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if request.method == "GET" and request.url.path == "/tasks/task_123":
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "completed",
                    "final_output": "Pi result",
                },
            )
        if request.method == "GET" and request.url.path == "/tasks/task_123/events":
            after = request.url.params.get("after")
            if after is None:
                return httpx.Response(
                    200,
                    json={
                        "task_id": "task_123",
                        "next_index": 1,
                        "events": [{"index": 0, "type": "session_start"}],
                    },
                )
            if after == "0":
                return httpx.Response(
                    200,
                    json={
                        "task_id": "task_123",
                        "next_index": 2,
                        "events": [
                            {
                                "index": 1,
                                "type": "message",
                                "message": {"role": "assistant", "content": "sensitive draft"},
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={"task_id": "task_123", "next_index": 2, "events": []},
            )
        return httpx.Response(404, json={"detail": "missing"})

    config = parse_tenant_execution_config(
        "tenant-1",
        {
            "agent_backend": {
                "type": "peer_agent",
                "peer": "pi",
                "cwd": "/workspace/project",
                "poll_interval_seconds": 0.001,
                "mcp_broker_enabled": False,
            }
        },
    )
    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "pi", "base_url": "http://pi-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            execution_resolver=InMemoryTenantExecutionResolver({"tenant-1": config}),
            peer_agent_registry=registry,
        )
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "please inspect the repo"},
        headers=AUTH_HEADERS,
    )

    with client.stream(
        "POST", f"/threads/{thread_id}/run/stream", headers=AUTH_HEADERS
    ) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert [event["type"] for event in events] == [
        "run.started",
        "peer.task.created",
        "peer.task.event",
        "peer.task.poll",
        "peer.task.event",
        "peer.task.completed",
        "assistant.message",
        "run.completed",
    ]
    assert events[2]["peer"] == "pi"
    assert events[2]["event"] == {"index": 0, "type": "session_start"}
    assert events[4]["event"] == {"index": 1, "type": "message"}
    assert "message" not in events[4]["event"]
    assert "sensitive draft" not in "\n".join(json.dumps(event) for event in events)
    assert events[6]["content"] == "Pi result"


def test_run_stream_endpoint_emits_peer_task_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if request.method == "GET" and request.url.path == "/tasks/task_123":
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "completed",
                    "final_output": "Pi result",
                    "usage": {"input": 12, "output": 5, "totalTokens": 17},
                },
            )
        if request.method == "GET" and request.url.path == "/tasks/task_123/events":
            return httpx.Response(200, json={"task_id": "task_123", "next_index": 0, "events": []})
        return httpx.Response(404, json={"detail": "missing"})

    config = parse_tenant_execution_config(
        "tenant-1",
        {
            "agent_backend": {
                "type": "peer_agent",
                "peer": "pi",
                "cwd": "/workspace/project",
                "poll_interval_seconds": 0.001,
                "mcp_broker_enabled": False,
            }
        },
    )
    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "pi", "base_url": "http://pi-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            execution_resolver=InMemoryTenantExecutionResolver({"tenant-1": config}),
            peer_agent_registry=registry,
        )
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "please inspect the repo"},
        headers=AUTH_HEADERS,
    )

    with client.stream(
        "POST", f"/threads/{thread_id}/run/stream", headers=AUTH_HEADERS
    ) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines() if line]

    run_started = next(event for event in events if event["type"] == "run.started")
    assert run_started["thread_context"]["estimated"] is True
    assert run_started["thread_context"]["total_tokens"] > 0
    completed = next(event for event in events if event["type"] == "peer.task.completed")
    assert completed["usage"] == {
        "prompt_tokens": 12,
        "input_tokens": 12,
        "completion_tokens": 5,
        "output_tokens": 5,
        "total_tokens": 17,
    }


def test_peer_agent_backend_persists_peer_tool_events_in_raw_context() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/tasks":
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if request.method == "GET" and request.url.path == "/tasks/task_123":
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "completed",
                    "final_output": "Pi result",
                    "events_tail": [
                        {
                            "index": 0,
                            "type": "tool_execution_start",
                            "tool_name": "read",
                            "toolCallId": "call-read-1",
                            "arguments": {"path": "README.md", "limit": 20},
                        },
                        {
                            "index": 1,
                            "type": "tool_execution_end",
                            "tool_name": "read",
                            "toolCallId": "call-read-1",
                            "status": "completed",
                            "result": {"content": "# Minigent"},
                        },
                    ],
                },
            )
        return httpx.Response(404, json={"detail": "missing"})

    config = parse_tenant_execution_config(
        "tenant-1",
        {
            "agent_backend": {
                "type": "peer_agent",
                "peer": "pi",
                "cwd": "/workspace/project",
                "poll_interval_seconds": 0.001,
                "mcp_broker_enabled": False,
            }
        },
    )
    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "pi", "base_url": "http://pi-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            execution_resolver=InMemoryTenantExecutionResolver({"tenant-1": config}),
            peer_agent_registry=registry,
        )
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "read the README"},
        headers=AUTH_HEADERS,
    )

    response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert response.status_code == 200
    raw_context = client.get(f"/threads/{thread_id}/context/raw", headers=AUTH_HEADERS).json()
    assert raw_context["messages"][1]["tool_name"] == "read"
    assert raw_context["messages"][1]["tool_call_id"] == "call-read-1"
    assert raw_context["messages"][1]["tool_arguments"] == {"summary": 'path="README.md", limit=20'}
    assert raw_context["messages"][2]["role"] == "tool"
    assert raw_context["messages"][2]["tool_name"] == "read"
    assert raw_context["messages"][2]["tool_call_id"] == "call-read-1"
    assert "# Minigent" in raw_context["messages"][2]["content"]
    assert "[assistant tool_call]\nname: read\nid: call-read-1" in raw_context["rendered"]
    assert "[tool_result]\nname: read\nid: call-read-1" in raw_context["rendered"]


def test_peer_task_event_sanitizer_preserves_tool_details_without_messages() -> None:
    sanitized = _sanitize_peer_task_event(
        {
            "index": 4,
            "type": "tool_execution_update",
            "toolName": "temperature",
            "toolCallId": "call-1",
            "status": "completed",
            "partialResult": {"indoor": "72 F"},
            "result": {"indoor": "72 F", "outdoor": "84 F"},
            "isError": False,
            "debugPayload": {"source": "thermostat"},
            "message": {"role": "assistant", "content": "sensitive draft"},
            "assistantMessageEvent": {"partial": {"content": "sensitive thinking"}},
        }
    )

    assert sanitized == {
        "index": 4,
        "type": "tool_execution_update",
        "status": "completed",
        "tool_name": "temperature",
        "partialResult": {"indoor": "72 F"},
        "result": {"indoor": "72 F", "outdoor": "84 F"},
        "isError": False,
        "debugPayload": {"source": "thermostat"},
    }


def test_peer_task_event_sanitizer_strips_nested_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINIGENT_PEER_TOOL_ARG_ALLOWLIST", raising=False)

    sanitized = _sanitize_peer_task_event(
        {
            "index": 2,
            "type": "tool_execution_end",
            "toolCall": {"name": "current_time", "arguments": {"timezone": "America/Chicago"}},
            "resultPayload": {"time": "9:55 PM", "timezone": "CDT"},
            "messages": [{"role": "assistant", "content": "sensitive draft"}],
        }
    )

    assert sanitized == {
        "index": 2,
        "type": "tool_execution_end",
        "tool_name": "current_time",
        "toolCall": {"name": "current_time"},
        "resultPayload": {"time": "9:55 PM", "timezone": "CDT"},
    }


def test_peer_task_event_sanitizer_adds_allowlisted_args_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINIGENT_PEER_TOOL_ARG_ALLOWLIST", raising=False)

    sanitized = _sanitize_peer_task_event(
        {
            "type": "tool_execution_start",
            "tool_name": "read",
            "toolCallId": "call-1",
            "arguments": {
                "path": "README.md",
                "limit": 20,
                "token": "secret-token",
                "content": "private prompt",
            },
        }
    )

    assert sanitized == {
        "type": "tool_execution_start",
        "tool_name": "read",
        "args_summary": 'path="README.md", limit=20',
    }


def test_peer_task_event_sanitizer_redacts_allowlisted_sensitive_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINIGENT_PEER_TOOL_ARG_ALLOWLIST", raising=False)

    sanitized = _sanitize_peer_task_event(
        {
            "type": "tool_execution_start",
            "tool_name": "grep",
            "arguments": {
                "pattern": "https://example.com/?token=abc123",
                "path": ".",
                "glob": "*.py",
            },
        }
    )

    assert sanitized["args_summary"] == (
        'pattern="https://example.com/?token=%3Credacted%3E", path=".", glob="*.py"'
    )
    assert "arguments" not in sanitized


def test_peer_task_event_sanitizer_uses_configured_arg_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_PEER_TOOL_ARG_ALLOWLIST", '{"read":["path"]}')

    sanitized = _sanitize_peer_task_event(
        {
            "type": "tool_execution_start",
            "tool_name": "read",
            "arguments": {"path": "README.md", "limit": 20},
        }
    )

    assert sanitized == {
        "type": "tool_execution_start",
        "tool_name": "read",
        "args_summary": 'path="README.md"',
    }


def test_peer_task_event_sanitizer_can_disable_arg_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_PEER_TOOL_ARG_ALLOWLIST", "off")

    sanitized = _sanitize_peer_task_event(
        {
            "type": "tool_execution_start",
            "tool_name": "read",
            "arguments": {"path": "README.md", "limit": 20},
        }
    )

    assert sanitized == {"type": "tool_execution_start", "tool_name": "read"}


def test_peer_task_event_sanitizer_can_allow_all_args_for_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_PEER_TOOL_ARG_ALLOWLIST", "all")

    sanitized = _sanitize_peer_task_event(
        {
            "type": "tool_execution_start",
            "tool_name": "custom_tool",
            "arguments": {
                "path": "README.md",
                "limit": 20,
                "token": "secret-token",
            },
        }
    )

    assert sanitized == {
        "type": "tool_execution_start",
        "tool_name": "custom_tool",
        "args_summary": 'path="README.md", limit=20, token="<redacted>"',
    }


def test_run_stream_endpoint_emits_error_event() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    with client.stream("POST", "/threads/missing/run/stream", headers=AUTH_HEADERS) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert events == [
        {"thread_id": "missing", "type": "run.started"},
        {
            "thread_id": "missing",
            "type": "run.error",
            "status_code": 404,
            "detail": "Thread 'missing' not found",
        },
    ]


def test_cancel_thread_run_endpoint_resets_stale_running_thread() -> None:
    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    client = TestClient(app)
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    app.state.store.set_thread_status("tenant-1", thread_id, ThreadStatus.RUNNING)

    response = client.post(f"/threads/{thread_id}/run/cancel", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "thread_id": thread_id}
    assert app.state.store.get_thread("tenant-1", thread_id).status == ThreadStatus.IDLE


class BlockingLLMAdapter(LLMAdapter):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
        self.started.set()
        await self.release.wait()
        return LLMResponse(content="done")

    def describe(self) -> dict[str, object]:
        return {"provider": "blocking"}


def test_agent_runtime_cancellation_resets_thread_to_idle() -> None:
    async def scenario() -> None:
        store = InMemoryThreadStore()
        adapter = BlockingLLMAdapter()
        runtime = AgentRuntime(
            store=store,
            llm_adapter=adapter,
            tool_registry=build_local_tool_registry(),
        )
        principal = Principal(user_id="user-1", tenant_id="tenant-1")
        thread = store.create_thread(principal.tenant_id)
        store.append_message(
            principal.tenant_id,
            Message(thread_id=thread.thread_id, role=MessageRole.USER, content="hello"),
        )

        task = asyncio.create_task(runtime.run_thread(principal, thread.thread_id))
        await adapter.started.wait()
        assert (
            store.get_thread(principal.tenant_id, thread.thread_id).status == ThreadStatus.RUNNING
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert store.get_thread(principal.tenant_id, thread.thread_id).status == ThreadStatus.IDLE

    asyncio.run(scenario())


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

    raw_context = client.get(f"/threads/{thread_id}/context/raw", headers=AUTH_HEADERS).json()
    assert raw_context["usage"]["estimated"] is True
    assert raw_context["messages"][1]["tool_name"] == "echo"
    assert raw_context["messages"][1]["tool_arguments"] == {"text": "hello from api"}
    assert "[assistant tool_call]\nname: echo" in raw_context["rendered"]
    assert 'arguments: {"text": "hello from api"}' in raw_context["rendered"]
    assert "[tool_result]\nname: echo" in raw_context["rendered"]


def test_run_endpoint_can_use_peer_agent_backend() -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, payload))
        if request.method == "POST" and request.url.path == "/tasks":
            assert payload is not None
            assert payload["cwd"] == "/workspace/project"
            env = payload["env"]
            assert isinstance(env, dict)
            assert env[MINIGENT_MCP_BROKER_URL_ENV].startswith("http://127.0.0.1:8000/mcp/peer/")
            assert env[MINIGENT_MCP_BROKER_TOKEN_ENV]
            prompt = str(payload["prompt"])
            assert "You are running as the execution backend for a Minigent thread." in prompt
            assert "Minigent MCP broker:" in prompt
            assert "[user]\nplease inspect the repo" in prompt
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if request.method == "GET" and request.url.path == "/tasks/task_123":
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "completed",
                    "final_output": "OpenCode result",
                },
            )
        return httpx.Response(404, json={"detail": "missing"})

    config = parse_tenant_execution_config(
        "tenant-1",
        {
            "agent_backend": {
                "type": "peer_agent",
                "peer": "opencode",
                "cwd": "/workspace/project",
                "poll_interval_seconds": 0.001,
            }
        },
    )
    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "opencode", "base_url": "http://opencode.test"}]),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            execution_resolver=InMemoryTenantExecutionResolver({"tenant-1": config}),
            peer_agent_registry=registry,
        )
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "please inspect the repo"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "OpenCode result"}
    messages = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS).json()
    assert [message["role"] for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert messages[-1]["content"] == "OpenCode result"
    assert [request[:2] for request in requests] == [("POST", "/tasks"), ("GET", "/tasks/task_123")]


def test_peer_agent_backend_prompt_includes_tool_call_context() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.method == "POST" and request.url.path == "/tasks":
            assert payload is not None
            prompt = str(payload["prompt"])
            assert "[assistant tool_call]\nname: echo\nid: call-1" in prompt
            assert 'arguments: {"text": "hello from peer context"}' in prompt
            assert "[tool_result]\nname: echo\nid: call-1" in prompt
            assert '{"echo": "hello from peer context"}' in prompt
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if request.method == "GET" and request.url.path == "/tasks/task_123":
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "completed",
                    "final_output": "Peer result",
                },
            )
        return httpx.Response(404, json={"detail": "missing"})

    config = parse_tenant_execution_config(
        "tenant-1",
        {
            "agent_backend": {
                "type": "peer_agent",
                "peer": "opencode",
                "cwd": "/workspace/project",
                "poll_interval_seconds": 0.001,
                "mcp_broker_enabled": False,
            }
        },
    )
    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "opencode", "base_url": "http://opencode.test"}]),
        transport=httpx.MockTransport(handler),
    )
    store = InMemoryThreadStore()
    client = TestClient(
        create_app(
            execution_resolver=InMemoryTenantExecutionResolver({"tenant-1": config}),
            peer_agent_registry=registry,
            thread_store=store,
        )
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    store.append_message(
        "tenant-1",
        Message(thread_id=thread_id, role=MessageRole.USER, content="use the tool result"),
    )
    store.append_message(
        "tenant-1",
        Message(
            thread_id=thread_id,
            role=MessageRole.ASSISTANT,
            content="",
            tool_name="echo",
            tool_call_id="call-1",
            tool_arguments={"text": "hello from peer context"},
        ),
    )
    store.append_message(
        "tenant-1",
        Message(
            thread_id=thread_id,
            role=MessageRole.TOOL,
            content='{"echo": "hello from peer context"}',
            tool_name="echo",
            tool_call_id="call-1",
        ),
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "Peer result"}


def test_peer_agent_backend_cancellation_cancels_peer_task_and_resets_thread() -> None:
    async def scenario() -> None:
        requests: list[tuple[str, str]] = []
        task_polled = asyncio.Event()
        release_poll = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.method == "POST" and request.url.path == "/tasks":
                return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
            if request.method == "GET" and request.url.path == "/tasks/task_123":
                task_polled.set()
                await release_poll.wait()
                return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
            if request.method == "POST" and request.url.path == "/tasks/task_123/cancel":
                return httpx.Response(200, json={"task_id": "task_123", "status": "canceled"})
            return httpx.Response(404, json={"detail": "missing"})

        config = parse_tenant_execution_config(
            "tenant-1",
            {
                "agent_backend": {
                    "type": "peer_agent",
                    "peer": "opencode",
                    "cwd": "/workspace/project",
                    "poll_interval_seconds": 0.001,
                }
            },
        )
        registry = PeerAgentRegistry(
            parse_peer_agent_configs([{"name": "opencode", "base_url": "http://opencode.test"}]),
            transport=httpx.MockTransport(handler),
        )
        store = InMemoryThreadStore()
        app = create_app(
            execution_resolver=InMemoryTenantExecutionResolver({"tenant-1": config}),
            peer_agent_registry=registry,
            thread_store=store,
        )
        principal = Principal(user_id="user-1", tenant_id="tenant-1")
        thread = store.create_thread(principal.tenant_id)
        store.append_message(
            principal.tenant_id,
            Message(thread_id=thread.thread_id, role=MessageRole.USER, content="please inspect"),
        )

        task = asyncio.create_task(app.state.agent_backend.run_thread(principal, thread.thread_id))
        await task_polled.wait()
        assert (
            store.get_thread(principal.tenant_id, thread.thread_id).status == ThreadStatus.RUNNING
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release_poll.set()

        assert ("POST", "/tasks/task_123/cancel") in requests
        assert store.get_thread(principal.tenant_id, thread.thread_id).status == ThreadStatus.IDLE

    asyncio.run(scenario())


def test_run_endpoint_can_disable_peer_agent_mcp_broker() -> None:
    requests: list[dict[str, object] | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.method == "POST" and request.url.path == "/tasks":
            requests.append(payload)
            assert payload is not None
            assert "env" not in payload
            assert "Minigent MCP broker:" not in str(payload["prompt"])
            return httpx.Response(
                200,
                json={"task_id": "task_123", "status": "completed", "final_output": "ok"},
            )
        return httpx.Response(404, json={"detail": "missing"})

    config = parse_tenant_execution_config(
        "tenant-1",
        {
            "agent_backend": {
                "type": "peer_agent",
                "peer": "opencode",
                "cwd": "/workspace/project",
                "mcp_broker_enabled": False,
            }
        },
    )
    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "opencode", "base_url": "http://opencode.test"}]),
        transport=httpx.MockTransport(handler),
    )
    client = TestClient(
        create_app(
            execution_resolver=InMemoryTenantExecutionResolver({"tenant-1": config}),
            peer_agent_registry=registry,
        )
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "please inspect the repo"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "ok"}
    assert len(requests) == 1


def test_openrouter_adapter_requests_usage_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert payload["usage"] == {"include": True}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "usage reply"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread-1", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.usage == {
        "prompt_tokens": 10,
        "input_tokens": 10,
        "completion_tokens": 3,
        "output_tokens": 3,
        "total_tokens": 13,
    }


def test_openai_adapter_normalizes_prompt_cache_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert payload["model"] == "test-model"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "cached reply"}}],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 25,
                    "total_tokens": 1225,
                    "prompt_tokens_details": {"cached_tokens": 900},
                },
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread-1", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "cached reply"
    assert response.usage == {
        "prompt_tokens": 1200,
        "input_tokens": 1200,
        "completion_tokens": 25,
        "output_tokens": 25,
        "total_tokens": 1225,
        "cache_read_tokens": 900,
    }


def test_run_endpoint_azure_adapter_allows_second_turn_after_tool_completion() -> None:
    seen_payloads: list[dict[str, object]] = []
    responses = deque(
        [
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text":"hello from api"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": 'Tool result: {"echo": "hello from api"}'}}]},
            {"choices": [{"message": {"content": "Mock reply: continue"}}]},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        seen_payloads.append(payload)
        if len(seen_payloads) == 2:
            assert [message["role"] for message in payload["messages"]] == [
                "system",
                "user",
                "assistant",
                "tool",
            ]
        if len(seen_payloads) == 3:
            assert [message["role"] for message in payload["messages"]] == [
                "system",
                "user",
                "assistant",
                "user",
            ]
        return httpx.Response(200, json=responses.popleft())

    adapter = OpenAICompatibleAdapter(
        base_url="https://example-resource.openai.azure.com/openai/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    client = TestClient(create_app(llm_adapter=adapter, tool_registry=build_local_tool_registry()))
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "weather today"},
        headers=AUTH_HEADERS,
    )

    first_run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert first_run.status_code == 200
    assert first_run.json() == {"reply": 'Tool result: {"echo": "hello from api"}'}

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "continue"},
        headers=AUTH_HEADERS,
    )

    second_run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert second_run.status_code == 200
    assert second_run.json() == {"reply": "Mock reply: continue"}


def test_run_endpoint_openrouter_retries_with_azure_tool_history_pruning() -> None:
    seen_payloads: list[dict[str, object]] = []
    responses = deque(
        [
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text":"hello from api"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": 'Tool result: {"echo": "hello from api"}'}}]},
            {
                "error": {
                    "message": "Provider returned error",
                    "code": 400,
                    "metadata": {
                        "raw": (
                            '{\n  "error": {\n    "message": '
                            '"No tool call found for function call output with call_id '
                            'call_123.",\n    "type": "invalid_request_error",\n    '
                            '"param": "input",\n    "code": null\n  }\n}'
                        ),
                        "provider_name": "Azure",
                        "is_byok": False,
                    },
                }
            },
            {"choices": [{"message": {"content": "Mock reply: continue"}}]},
        ]
    )
    status_codes = deque([200, 200, 400, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        seen_payloads.append(payload)
        if len(seen_payloads) == 2:
            assert [message["role"] for message in payload["messages"]] == [
                "system",
                "user",
                "assistant",
                "tool",
            ]
        if len(seen_payloads) == 3:
            assert [message["role"] for message in payload["messages"]] == [
                "system",
                "user",
                "assistant",
                "tool",
                "assistant",
                "user",
            ]
        if len(seen_payloads) == 4:
            assert [message["role"] for message in payload["messages"]] == [
                "system",
                "user",
                "assistant",
                "user",
            ]
            assert payload["messages"][2]["content"] == 'Tool result: {"echo": "hello from api"}'
        return httpx.Response(status_codes.popleft(), json=responses.popleft())

    adapter = OpenAICompatibleAdapter(
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    client = TestClient(create_app(llm_adapter=adapter, tool_registry=build_local_tool_registry()))
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "weather today"},
        headers=AUTH_HEADERS,
    )

    first_run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert first_run.status_code == 200
    assert first_run.json() == {"reply": 'Tool result: {"echo": "hello from api"}'}

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "continue"},
        headers=AUTH_HEADERS,
    )

    second_run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert second_run.status_code == 200
    assert second_run.json() == {"reply": "Mock reply: continue"}


def test_run_endpoint_returns_reply_when_tool_fails() -> None:
    class ToolFailingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
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


def test_run_endpoint_returns_reply_when_tool_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_TOOL_TIMEOUT_SECONDS", "0.01")

    class TimeoutLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            if messages and messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content=f"Tool result: {messages[-1].content}")
            return LLMResponse(
                tool_call=ToolCall(
                    id="call-slow",
                    name="slow_tool",
                    arguments={"delay": 1},
                )
            )

        def describe(self) -> dict[str, object]:
            return {
                "provider": "test",
                "model": None,
                "base_url": None,
                "headers": [],
                "adapter": "TimeoutLLM",
            }

    class SlowRegistry:
        def specs(self) -> list[object]:
            return []

        def mcp_servers(self) -> list[dict[str, object]]:
            return []

        async def execute(self, name: str, arguments: dict[str, object]) -> object:
            await asyncio.sleep(1)
            return {"ok": True}

    client = TestClient(create_app(llm_adapter=TimeoutLLM(), tool_registry=SlowRegistry()))  # type: ignore[arg-type]
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "run slow tool"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {
        "reply": (
            'Tool result: {"error": {"tool_name": "slow_tool", "status_code": 504, '
            '"code": "tool_timeout", "detail": "Tool call timed out after 0.01 seconds", '
            '"timeout_seconds": 0.01}}'
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


def test_create_app_fails_fast_for_jwt_without_key_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_AUTH_MODE", "jwt")
    monkeypatch.delenv("MINIGENT_JWT_SHARED_SECRET", raising=False)
    monkeypatch.delenv("MINIGENT_JWT_JWKS_URL", raising=False)

    with pytest.raises(RuntimeError, match="MINIGENT_JWT_JWKS_URL is required"):
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())


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


def test_tenant_execution_env_interpolation_replaces_nested_string_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_API_KEY", "tenant-secret")
    monkeypatch.setenv("MCP_URL", "http://127.0.0.1:9123/mcp")
    monkeypatch.setenv("MCP_TOKEN", "mcp-secret")
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock", "api_key": "${TENANT_API_KEY}"},
                    "tools": {
                        "mcp_servers": [
                            {
                                "name": "svc",
                                "url": "${MCP_URL}",
                                "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                            }
                        ]
                    },
                }
            }
        ),
    )

    context = build_execution_resolver_from_env().resolve("tenant-1")

    assert context.config.llm.api_key == "tenant-secret"
    server = context.config.tools.mcp_servers[0]
    assert server.url == "http://127.0.0.1:9123/mcp"
    assert server.headers == {"Authorization": "Bearer mcp-secret"}


def test_tenant_execution_config_inherits_global_llm_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", "generic-oauth")
    monkeypatch.setenv("MINIGENT_LLM_MODEL", "gpt-test")
    monkeypatch.setenv("MINIGENT_LLM_URL", "https://example.test/responses")
    monkeypatch.setenv("MINIGENT_OAUTH_PROVIDER_ID", "test-oauth")
    monkeypatch.setenv("MINIGENT_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("MINIGENT_OAUTH_AUTHORIZE_URL", "https://auth.example/authorize")
    monkeypatch.setenv("MINIGENT_OAUTH_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv("MINIGENT_OAUTH_SCOPE", "openid")
    monkeypatch.setenv("MINIGENT_OAUTH_STORE_PATH", str(tmp_path / "oauth.json"))
    monkeypatch.setenv(
        "MINIGENT_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/oauth/generic/callback"
    )
    monkeypatch.setenv(
        "MINIGENT_LLM_EXTRA_HEADERS",
        json.dumps({"OpenAI-Beta": "responses=experimental"}),
    )
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps({"tenant-1": {"tools": {"allowed_local_tools": ["calculator"]}}}),
    )

    context = build_execution_resolver_from_env().resolve("tenant-1")

    assert context.config.llm.provider == "generic-oauth"
    assert context.config.llm.model == "gpt-test"
    assert context.config.llm.base_url == "https://example.test/responses"
    assert context.config.llm.extra_headers == {"OpenAI-Beta": "responses=experimental"}
    assert context.llm_adapter.describe()["provider"] == "generic-oauth"


def test_tenant_execution_config_explicit_llm_overrides_global_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", "generic-oauth")
    monkeypatch.setenv("MINIGENT_LLM_MODEL", "gpt-test")
    monkeypatch.setenv("MINIGENT_LLM_URL", "https://example.test/responses")
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps({"tenant-1": {"llm": {"provider": "mock"}}}),
    )

    context = build_execution_resolver_from_env().resolve("tenant-1")

    assert context.config.llm.provider == "mock"
    assert context.llm_adapter.describe()["provider"] == "mock"


def test_tenant_execution_env_interpolation_preserves_non_string_values() -> None:
    payload = {
        "enabled": True,
        "timeout": 12.5,
        "items": ["${NAME}", 3, None],
    }

    interpolated = interpolate_tenant_execution_env_placeholders(payload, {"NAME": "demo"})

    assert interpolated == {"enabled": True, "timeout": 12.5, "items": ["demo", 3, None]}


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


def test_config_reports_peer_agent_mcp_broker_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "agent_backend": {
                        "type": "peer_agent",
                        "peer": "opencode",
                        "cwd": "/workspace/project",
                        "mcpBrokerEnabled": False,
                    }
                }
            }
        ),
    )
    client = TestClient(create_app())

    response = client.get("/config", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["agent_backend"]["mcp_broker_enabled"] is False


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


def test_execution_options_lists_sanitized_skills_and_capability_profiles(
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
                        "default_skill": "support",
                        "items": [
                            {
                                "name": "support",
                                "description": "Support assistant",
                                "system_prompt": "secret prompt text",
                            },
                            {
                                "name": "coding",
                                "system_prompt": "another secret prompt",
                            },
                        ],
                    },
                    "capability_profiles": {
                        "default_profile": "inspect",
                        "items": [
                            {
                                "name": "inspect",
                                "description": "Inspection tools",
                                "allowed_local_tools": ["echo"],
                            },
                            {
                                "name": "math",
                                "allowed_local_tools": ["calculator"],
                            },
                        ],
                    },
                    "agents": {
                        "items": [
                            {
                                "name": "support",
                                "description": "Support mode",
                                "skill_name": "support",
                                "capability_profile": "inspect",
                            },
                            {
                                "name": "math",
                                "skills": ["coding"],
                                "capability_profile": "math",
                            },
                        ],
                    },
                }
            }
        ),
    )
    client = TestClient(create_app())

    response = client.get("/execution-options", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "tenant-1",
        "skills": {
            "default": "support",
            "items": [
                {"name": "support", "description": "Support assistant"},
                {"name": "coding", "description": None},
            ],
        },
        "capability_profiles": {
            "default": "inspect",
            "items": [
                {"name": "inspect", "description": "Inspection tools"},
                {"name": "math", "description": None},
            ],
        },
        "agents": {
            "items": [
                {
                    "name": "support",
                    "description": "Support mode",
                    "skill_name": "support",
                    "skills": None,
                    "capability_profile": "inspect",
                },
                {
                    "name": "math",
                    "description": None,
                    "skill_name": None,
                    "skills": ["coding"],
                    "capability_profile": "math",
                },
            ],
        },
    }
    assert "system_prompt" not in response.text
    assert "allowed_local_tools" not in response.text


def test_imported_agent_skill_is_listed_and_loaded_through_api_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skills" / "code-reviewer"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: code-reviewer\n"
        "description: Reviews code changes.\n"
        "---\n\n"
        "Loaded imported skill instructions.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "minigent.toml"
    config_path.write_text(
        """
[agent_skills]
dirs = ["./skills"]

[tenant_execution_configs.tenant-1.llm]
provider = "mock"
""".strip(),
        encoding="utf-8",
    )

    seen_messages: list[Message] = []

    class RecordingLLMAdapter(LLMAdapter):
        async def generate(
            self,
            messages: list[Message],
            tools: list[ToolSpec],
        ) -> LLMResponse:
            seen_messages.extend(messages)
            return LLMResponse(content="imported skill reply")

        def describe(self) -> dict[str, object]:
            return {"provider": "recording"}

    monkeypatch.delenv("MINIGENT_TENANT_EXECUTION_CONFIGS", raising=False)
    monkeypatch.delenv("MINIGENT_DOTENV_FILE", raising=False)
    monkeypatch.setenv("MINIGENT_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("MINIGENT_THREAD_DB_PATH", str(tmp_path / "threads.db"))
    monkeypatch.setattr(
        execution_module,
        "_build_llm_adapter",
        lambda _config: RecordingLLMAdapter(),
    )
    load_environment(discover_default_files=False)
    client = TestClient(create_app())

    options_response = client.get("/execution-options", headers=AUTH_HEADERS)
    assert options_response.status_code == 200
    assert options_response.json()["skills"] == {
        "default": None,
        "items": [{"name": "code-reviewer", "description": "Reviews code changes."}],
    }
    assert "Loaded imported skill instructions" not in options_response.text

    create_response = client.post(
        "/threads",
        json={"skill_name": "code-reviewer"},
        headers=AUTH_HEADERS,
    )
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]
    message_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "please review this diff"},
        headers=AUTH_HEADERS,
    )
    assert message_response.status_code == 200

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "imported skill reply"}
    assert seen_messages
    assert "[Skill: code-reviewer]" in seen_messages[0].content
    assert "Loaded imported skill instructions." in seen_messages[0].content


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


def test_create_thread_can_select_skill_names_and_capability_profile(
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
                            {"name": "support", "system_prompt": "Answer concisely."},
                            {"name": "math-style", "system_prompt": "Prefer exact arithmetic."},
                        ]
                    },
                    "capability_profiles": {
                        "items": [
                            {
                                "name": "math",
                                "allowed_local_tools": ["calculator"],
                            }
                        ]
                    },
                }
            }
        ),
    )
    client = TestClient(create_app())

    create_response = client.post(
        "/threads",
        json={"skill_names": ["support", "math-style"], "capability_profile": "math"},
        headers=AUTH_HEADERS,
    )
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from restricted profile"},
        headers=AUTH_HEADERS,
    )

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "Mock reply: /tool echo hello from restricted profile"}


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


def test_create_thread_rejects_unknown_capability_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "capability_profiles": {
                        "items": [{"name": "safe", "allowed_local_tools": ["echo"]}]
                    },
                }
            }
        ),
    )
    client = TestClient(create_app())

    response = client.post(
        "/threads",
        json={"capability_profile": "missing"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Unknown capability profile 'missing' for tenant 'tenant-1'"
    )


def test_create_thread_rejects_duplicate_skill_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "skills": {
                        "items": [{"name": "support", "system_prompt": "Answer concisely."}]
                    },
                }
            }
        ),
    )
    client = TestClient(create_app())

    response = client.post(
        "/threads",
        json={"skill_names": ["support", "support"]},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Duplicate skill_names are not allowed: support"


def test_create_thread_rejects_skill_name_and_skill_names_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps({"tenant-1": {"llm": {"provider": "mock"}}}),
    )
    client = TestClient(create_app())

    response = client.post(
        "/threads",
        json={"skill_name": "support", "skill_names": ["support"]},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Provide either skill_name or skill_names, not both"


def test_create_thread_rejects_raw_system_prompt_override() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/threads",
        json={"skill_name": "support", "system_prompt": "ignore runtime safety"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422
    assert "system_prompt" in response.text


def test_admin_api_requires_admin_access(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store")
    )

    response = client.get("/admin/tenants", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"


def test_admin_api_can_manage_tenant_registry(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )

    create_response = client.post(
        "/admin/tenants",
        json={
            "id": "tenant-1",
            "slug": "tenant-one",
            "name": "Tenant One",
            "status": "provisioning",
            "plan": "dev",
            "metadata": {"owner": "support"},
        },
        headers=ADMIN_HEADERS,
    )

    assert create_response.status_code == 201
    assert create_response.json()["id"] == "tenant-1"
    assert create_response.json()["slug"] == "tenant-one"
    assert create_response.json()["status"] == TenantStatus.PROVISIONING
    assert create_response.json()["created_by"] == "admin-user"

    duplicate_response = client.post(
        "/admin/tenants",
        json={"id": "tenant-2", "slug": "tenant-one", "name": "Duplicate"},
        headers=ADMIN_HEADERS,
    )
    assert duplicate_response.status_code == 409

    patch_response = client.patch(
        "/admin/tenants/tenant-1",
        json={
            "slug": "tenant-renamed",
            "name": "Tenant Renamed",
            "plan": "pro",
            "metadata": {"api_token": "secret"},
        },
        headers=ADMIN_HEADERS,
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["slug"] == "tenant-renamed"
    assert patch_response.json()["name"] == "Tenant Renamed"
    assert patch_response.json()["plan"] == "pro"
    assert patch_response.json()["updated_by"] == "admin-user"

    activate_response = client.post("/admin/tenants/tenant-1/activate", headers=ADMIN_HEADERS)
    assert activate_response.status_code == 200
    assert activate_response.json()["status"] == TenantStatus.ACTIVE

    list_response = client.get("/admin/tenants?status=active&plan=pro", headers=ADMIN_HEADERS)
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert list_response.json()["tenants"][0]["id"] == "tenant-1"

    delete_response = client.delete("/admin/tenants/tenant-1", headers=ADMIN_HEADERS)
    assert delete_response.status_code == 200
    assert delete_response.json() == {
        "deleted": True,
        "tenant_id": "tenant-1",
        "status": TenantStatus.DELETED,
    }

    audit_response = client.get(
        "/admin/tenants/tenant-1/audit-records?action=tenants.update",
        headers=ADMIN_HEADERS,
    )
    assert audit_response.status_code == 200
    audit_record = audit_response.json()["audit_records"][0]
    assert audit_record["resource_type"] == "tenant"
    assert audit_record["resource_id"] == "tenant-1"
    assert audit_record["old_values"]["slug"] == "tenant-one"
    assert audit_record["new_values"]["slug"] == "tenant-renamed"
    assert audit_record["new_values"]["metadata"]["api_token"] == "<redacted>"


def test_admin_store_can_manage_tenant_users(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path)

    created = store.create_tenant_user(
        TenantUser(
            id="membership-1",
            tenant_id="tenant-1",
            user_id="user-1",
            email="user@example.com",
            display_name="User One",
            role=TenantUserRole.MEMBER,
            status=TenantUserStatus.INVITED,
            metadata={"team": "engineering"},
            created_by="admin-user",
            updated_by="admin-user",
        )
    )

    assert created.id == "membership-1"
    assert created.tenant_id == "tenant-1"
    assert created.user_id == "user-1"
    assert created.email == "user@example.com"
    assert created.role == TenantUserRole.MEMBER
    assert created.status == TenantUserStatus.INVITED
    assert created.metadata == {"team": "engineering"}

    assert store.get_tenant_user("tenant-1", "membership-1") == created
    assert store.get_tenant_user_by_user_id("tenant-1", "user-1") == created

    duplicate_user = TenantUser(
        id="membership-2",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.create_tenant_user(duplicate_user)

    updated = store.update_tenant_user(
        "tenant-1",
        "membership-1",
        display_name="User Renamed",
        role=TenantUserRole.ADMIN,
        status=TenantUserStatus.ACTIVE,
        metadata={"team": "support"},
        updated_by="admin-user",
    )
    assert updated is not None
    assert updated.display_name == "User Renamed"
    assert updated.role == TenantUserRole.ADMIN
    assert updated.status == TenantUserStatus.ACTIVE
    assert updated.metadata == {"team": "support"}
    assert updated.updated_by == "admin-user"

    users, total = store.list_tenant_users(
        "tenant-1",
        status=TenantUserStatus.ACTIVE,
        role=TenantUserRole.ADMIN,
    )
    assert total == 1
    assert users[0].id == "membership-1"

    assert store.delete_tenant_user("tenant-1", "membership-1", updated_by="admin-user") is True
    deleted = store.get_tenant_user("tenant-1", "membership-1")
    assert deleted is not None
    assert deleted.status == TenantUserStatus.DELETED


def test_admin_api_can_manage_tenant_users(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One"},
        headers=ADMIN_HEADERS,
    )

    create_response = client.post(
        "/admin/tenants/tenant-1/users",
        json={
            "user_id": "user-1",
            "email": "USER@Example.COM",
            "display_name": "User One",
            "role": "member",
            "status": "invited",
            "metadata": {"api_token": "secret", "team": "engineering"},
        },
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["tenant_id"] == "tenant-1"
    assert created["user_id"] == "user-1"
    assert created["email"] == "user@example.com"
    assert created["role"] == "member"
    assert created["status"] == "invited"
    assert created["created_by"] == "admin-user"
    user_record_id = created["id"]

    duplicate_response = client.post(
        "/admin/tenants/tenant-1/users",
        json={"user_id": "user-1"},
        headers=ADMIN_HEADERS,
    )
    assert duplicate_response.status_code == 409

    invalid_user_response = client.post(
        "/admin/tenants/tenant-1/users",
        json={"user_id": "   "},
        headers=ADMIN_HEADERS,
    )
    assert invalid_user_response.status_code == 400

    invalid_email_response = client.post(
        "/admin/tenants/tenant-1/users",
        json={"user_id": "user-2", "email": "not-an-email"},
        headers=ADMIN_HEADERS,
    )
    assert invalid_email_response.status_code == 400

    show_response = client.get(
        f"/admin/tenants/tenant-1/users/{user_record_id}",
        headers=ADMIN_HEADERS,
    )
    assert show_response.status_code == 200
    assert show_response.json()["id"] == user_record_id

    patch_response = client.patch(
        f"/admin/tenants/tenant-1/users/{user_record_id}",
        json={"display_name": "User Renamed", "role": "admin", "metadata": {"team": "support"}},
        headers=ADMIN_HEADERS,
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["display_name"] == "User Renamed"
    assert patch_response.json()["role"] == "admin"
    assert patch_response.json()["updated_by"] == "admin-user"

    activate_response = client.post(
        f"/admin/tenants/tenant-1/users/{user_record_id}/activate",
        headers=ADMIN_HEADERS,
    )
    assert activate_response.status_code == 200
    assert activate_response.json()["status"] == "active"

    list_response = client.get(
        "/admin/tenants/tenant-1/users?status=active&role=admin&email=USER@example.com",
        headers=ADMIN_HEADERS,
    )
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert list_response.json()["users"][0]["id"] == user_record_id

    suspend_response = client.post(
        f"/admin/tenants/tenant-1/users/{user_record_id}/suspend",
        headers=ADMIN_HEADERS,
    )
    assert suspend_response.status_code == 200
    assert suspend_response.json()["status"] == "suspended"

    delete_response = client.delete(
        f"/admin/tenants/tenant-1/users/{user_record_id}",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 200
    assert delete_response.json() == {
        "deleted": True,
        "tenant_id": "tenant-1",
        "id": user_record_id,
        "status": "deleted",
    }

    audit_response = client.get(
        "/admin/tenants/tenant-1/audit-records?action=tenant_users.create",
        headers=ADMIN_HEADERS,
    )
    assert audit_response.status_code == 200
    audit_record = audit_response.json()["audit_records"][0]
    assert audit_record["resource_type"] == "tenant_user"
    assert audit_record["resource_id"] == user_record_id
    assert audit_record["new_values"]["user_id"] == "user-1"
    assert audit_record["new_values"]["metadata"]["api_token"] == "<redacted>"


def test_admin_api_rejects_tenant_users_for_unknown_tenant(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )

    response = client.post(
        "/admin/tenants/missing/users",
        json={"user_id": "user-1"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 404


def test_admin_api_tenant_users_require_admin(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )

    response = client.get("/admin/tenants/tenant-1/users", headers=AUTH_HEADERS)

    assert response.status_code == 403


def test_admin_api_rejects_invalid_tenant_slug(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )

    response = client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "Bad Slug", "name": "Tenant One"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 400
    assert "slug" in response.json()["detail"]


def test_admin_api_can_manage_tenant_domains(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One"},
        headers=ADMIN_HEADERS,
    )

    create_response = client.post(
        "/admin/tenants/tenant-1/domains",
        json={"domain": "App.Example.COM."},
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 201
    assert create_response.json()["tenant_id"] == "tenant-1"
    assert create_response.json()["domain"] == "app.example.com"
    assert create_response.json()["verified"] is False
    domain_id = create_response.json()["id"]

    duplicate_response = client.post(
        "/admin/tenants/tenant-1/domains",
        json={"domain": "app.example.com"},
        headers=ADMIN_HEADERS,
    )
    assert duplicate_response.status_code == 409

    invalid_response = client.post(
        "/admin/tenants/tenant-1/domains",
        json={"domain": "https://app.example.com/path"},
        headers=ADMIN_HEADERS,
    )
    assert invalid_response.status_code == 400

    list_response = client.get("/admin/tenants/tenant-1/domains", headers=ADMIN_HEADERS)
    assert list_response.status_code == 200
    assert list_response.json()["domains"][0]["domain"] == "app.example.com"

    lookup_response = client.get(
        "/admin/tenant-domains/lookup?domain=APP.EXAMPLE.COM.",
        headers=ADMIN_HEADERS,
    )
    assert lookup_response.status_code == 200
    assert lookup_response.json()["id"] == domain_id
    assert lookup_response.json()["tenant_id"] == "tenant-1"

    unverified_lookup_response = client.get(
        "/admin/tenant-domains/lookup?domain=app.example.com&verified_only=true",
        headers=ADMIN_HEADERS,
    )
    assert unverified_lookup_response.status_code == 404

    verify_response = client.post(
        f"/admin/tenants/tenant-1/domains/{domain_id}/verify",
        headers=ADMIN_HEADERS,
    )
    assert verify_response.status_code == 200
    assert verify_response.json()["verified"] is True

    verified_lookup_response = client.get(
        "/admin/tenant-domains/lookup?domain=app.example.com&verified_only=true",
        headers=ADMIN_HEADERS,
    )
    assert verified_lookup_response.status_code == 200
    assert verified_lookup_response.json()["id"] == domain_id

    audit_response = client.get(
        "/admin/tenants/tenant-1/audit-records?action=tenant_domains.verify",
        headers=ADMIN_HEADERS,
    )
    assert audit_response.status_code == 200
    audit_record = audit_response.json()["audit_records"][0]
    assert audit_record["resource_type"] == "tenant"
    assert audit_record["resource_id"] == "tenant-1"
    assert audit_record["old_values"]["verified"] is False
    assert audit_record["new_values"]["verified"] is True

    delete_response = client.delete(
        f"/admin/tenants/tenant-1/domains/{domain_id}",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 204
    assert (
        client.get("/admin/tenants/tenant-1/domains", headers=ADMIN_HEADERS).json()["domains"] == []
    )


def test_admin_api_seeds_tenants_from_execution_configs(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path)
    store.upsert_raw_config("Tenant A", {"llm": {"provider": "mock"}})
    store.upsert_raw_config("tenant-a", {"llm": {"provider": "mock"}})
    client = TestClient(create_app(admin_store=store, tenant_config_source="store-with-defaults"))

    existing_response = client.post(
        "/admin/tenants",
        json={"id": "existing", "slug": "tenant-a", "name": "Existing"},
        headers=ADMIN_HEADERS,
    )
    assert existing_response.status_code == 201

    dry_run_response = client.post(
        "/admin/tenants/seed",
        json={
            "source": "execution-configs",
            "status": "active",
            "plan": "pro",
            "region": "us",
            "dry_run": True,
        },
        headers=ADMIN_HEADERS,
    )

    assert dry_run_response.status_code == 200
    dry_run_payload = dry_run_response.json()
    assert dry_run_payload["dry_run"] is True
    assert dry_run_payload["discovered"] == 2
    assert dry_run_payload["created"] == 0
    assert {item["action"] for item in dry_run_payload["tenants"]} == {"would_create"}
    assert client.get("/admin/tenants/Tenant A", headers=ADMIN_HEADERS).status_code == 404

    seed_response = client.post(
        "/admin/tenants/seed",
        json={
            "source": "execution-configs",
            "status": "active",
            "plan": "pro",
            "region": "us",
        },
        headers=ADMIN_HEADERS,
    )

    assert seed_response.status_code == 200
    payload = seed_response.json()
    assert payload["discovered"] == 2
    assert payload["created"] == 2
    assert payload["existing"] == 0
    assert payload["conflicts"] == 0
    by_id = {item["id"]: item for item in payload["tenants"]}
    assert by_id["Tenant A"]["slug"] == "tenant-a-2"
    assert by_id["tenant-a"]["slug"] == "tenant-a-3"

    get_response = client.get("/admin/tenants/Tenant A", headers=ADMIN_HEADERS)
    assert get_response.status_code == 200
    assert get_response.json()["status"] == TenantStatus.ACTIVE
    assert get_response.json()["plan"] == "pro"
    assert get_response.json()["region"] == "us"

    second_seed_response = client.post(
        "/admin/tenants/seed",
        json={"source": "execution-configs"},
        headers=ADMIN_HEADERS,
    )
    assert second_seed_response.status_code == 200
    assert second_seed_response.json()["existing"] == 2
    assert second_seed_response.json()["created"] == 0

    audit_response = client.get(
        "/admin/tenants/Tenant A/audit-records?action=tenants.seed",
        headers=ADMIN_HEADERS,
    )
    assert audit_response.status_code == 200
    audit_record = audit_response.json()["audit_records"][0]
    assert audit_record["resource_type"] == "tenant"
    assert audit_record["new_values"]["slug"] == "tenant-a-2"
    assert audit_record["metadata"] == {"source": "execution-configs", "slug": "tenant-a-2"}


def test_admin_api_seed_rejects_unknown_source(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )

    response = client.post(
        "/admin/tenants/seed",
        json={"source": "static-tokens"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 400


def test_admin_api_can_manage_tenant_entitlements(tmp_path: Path) -> None:
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )

    missing_response = client.get("/admin/tenants/tenant-1/entitlements", headers=ADMIN_HEADERS)
    assert missing_response.status_code == 404

    validate_response = client.post(
        "/admin/tenants/tenant-1/entitlements/validate",
        json={"features": {"mcp": True}, "limits": {"max_threads": 100}},
        headers=ADMIN_HEADERS,
    )
    assert validate_response.status_code == 200
    assert validate_response.json()["valid"] is True

    first_response = client.put(
        "/admin/tenants/tenant-1/entitlements",
        json={
            "features": {"mcp": True, "peer_agents": False},
            "limits": {"max_threads": 100, "tier": "pro"},
        },
        headers=ADMIN_HEADERS,
    )
    assert first_response.status_code == 200
    assert first_response.json()["version"] == 1
    assert first_response.json()["features"] == {"mcp": True, "peer_agents": False}

    second_response = client.put(
        "/admin/tenants/tenant-1/entitlements",
        json={"features": {"mcp": False}, "limits": {"max_threads": 50}},
        headers=ADMIN_HEADERS,
    )
    assert second_response.status_code == 200
    assert second_response.json()["version"] == 2

    get_response = client.get("/admin/tenants/tenant-1/entitlements", headers=ADMIN_HEADERS)
    assert get_response.status_code == 200
    assert get_response.json()["limits"] == {"max_threads": 50}

    audit_response = client.get(
        "/admin/tenants/tenant-1/audit-records?action=tenant_entitlements.put",
        headers=ADMIN_HEADERS,
    )
    assert audit_response.status_code == 200
    audit_record = audit_response.json()["audit_records"][0]
    assert audit_record["resource_type"] == "tenant"
    assert audit_record["resource_id"] == "tenant-1"
    assert audit_record["old_values"]["features"] == {"mcp": True, "peer_agents": False}
    assert audit_record["new_values"]["features"] == {"mcp": False}

    delete_response = client.delete(
        "/admin/tenants/tenant-1/entitlements",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 204
    assert (
        client.get("/admin/tenants/tenant-1/entitlements", headers=ADMIN_HEADERS).status_code == 404
    )


def test_tenant_context_is_minimal_without_registry_requirement(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            admin_store=_sqlite_store(tmp_path),
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )

    response = client.get("/tenant-context", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-1"
    assert response.json()["principal"] == {
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "is_admin": False,
    }
    assert response.json()["slug"] is None
    assert response.json()["features"] == {}
    assert response.json()["execution_config_version"] is None
    assert response.json()["entitlements_version"] is None


def test_tenant_context_enriches_known_tenant_without_requirement(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path)
    client = TestClient(
        create_app(
            admin_store=store,
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One", "status": "suspended"},
        headers=ADMIN_HEADERS,
    )
    client.put(
        "/admin/tenants/tenant-1/entitlements",
        json={"features": {"mcp": True}, "limits": {"max_threads": 100}},
        headers=ADMIN_HEADERS,
    )

    response = client.get("/tenant-context", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["slug"] == "tenant-one"
    assert response.json()["status"] == TenantStatus.SUSPENDED
    assert response.json()["features"] == {"mcp": True}
    assert response.json()["limits"] == {"max_threads": 100}
    assert response.json()["execution_config_version"] is None
    assert response.json()["entitlements_version"] == 1


def test_tenant_context_requires_active_tenant_user_when_user_registry_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_USER_REGISTRY_REQUIRED", "true")
    client = TestClient(
        create_app(
            admin_store=_sqlite_store(tmp_path),
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One", "status": "active"},
        headers=ADMIN_HEADERS,
    )

    missing_response = client.get("/tenant-context", headers=AUTH_HEADERS)
    assert missing_response.status_code == 403
    assert missing_response.json()["detail"] == "Tenant user is not active"

    create_user_response = client.post(
        "/admin/tenants/tenant-1/users",
        json={
            "user_id": "user-1",
            "email": "USER@example.com",
            "display_name": "User One",
            "role": "admin",
            "status": "active",
            "metadata": {"team": "engineering"},
        },
        headers=ADMIN_HEADERS,
    )
    assert create_user_response.status_code == 201
    user_record_id = create_user_response.json()["id"]

    response = client.get("/tenant-context", headers=AUTH_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["tenant_id"] == "tenant-1"
    assert payload["membership_id"] == user_record_id
    assert payload["membership_email"] == "user@example.com"
    assert payload["membership_display_name"] == "User One"
    assert payload["user_role"] == "admin"
    assert payload["user_status"] == "active"
    assert payload["membership_metadata"] == {"team": "engineering"}


@pytest.mark.parametrize("status", ["invited", "suspended", "deleted"])
def test_tenant_context_rejects_inactive_tenant_user_when_user_registry_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_USER_REGISTRY_REQUIRED", "true")
    client = TestClient(
        create_app(
            admin_store=_sqlite_store(tmp_path),
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One", "status": "active"},
        headers=ADMIN_HEADERS,
    )
    client.post(
        "/admin/tenants/tenant-1/users",
        json={"user_id": "user-1", "status": status},
        headers=ADMIN_HEADERS,
    )

    response = client.get("/tenant-context", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert response.json()["detail"] == "Tenant user is not active"


def test_tenant_context_requires_tenant_user_store_when_user_registry_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_USER_REGISTRY_REQUIRED", "true")
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )

    response = client.get("/tenant-context", headers=AUTH_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == "Tenant user registry is not enabled"


def test_tenant_context_includes_execution_config_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_REGISTRY_REQUIRED", "true")
    client = TestClient(
        create_app(admin_store=_sqlite_store(tmp_path), tenant_config_source="store-with-defaults")
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One", "status": "active"},
        headers=ADMIN_HEADERS,
    )

    initial_response = client.get("/tenant-context", headers=AUTH_HEADERS)
    assert initial_response.status_code == 200
    assert initial_response.json()["execution_config_version"] is None

    first_config_response = client.put(
        "/admin/tenants/tenant-1/execution-config",
        json={"config": {"llm": {"provider": "mock", "model": "first"}}},
        headers=ADMIN_HEADERS,
    )
    assert first_config_response.status_code == 200
    first_context_response = client.get("/tenant-context", headers=AUTH_HEADERS)
    assert first_context_response.status_code == 200
    assert first_context_response.json()["execution_config_version"] == 1

    second_config_response = client.put(
        "/admin/tenants/tenant-1/execution-config",
        json={"config": {"llm": {"provider": "mock", "model": "second"}}},
        headers=ADMIN_HEADERS,
    )
    assert second_config_response.status_code == 200
    second_context_response = client.get("/tenant-context", headers=AUTH_HEADERS)
    assert second_context_response.status_code == 200
    assert second_context_response.json()["execution_config_version"] == 2


def test_tenant_context_requires_active_tenant_when_registry_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_REGISTRY_REQUIRED", "true")
    client = TestClient(
        create_app(
            admin_store=_sqlite_store(tmp_path),
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One", "status": "active"},
        headers=ADMIN_HEADERS,
    )
    client.put(
        "/admin/tenants/tenant-1/entitlements",
        json={"features": {"mcp": True}, "limits": {"max_threads": 100}},
        headers=ADMIN_HEADERS,
    )

    response = client.get("/tenant-context", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["status"] == TenantStatus.ACTIVE
    assert response.json()["features"] == {"mcp": True}


def test_tenant_entitlements_enforce_thread_and_message_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_REGISTRY_REQUIRED", "true")
    client = TestClient(
        create_app(
            admin_store=_sqlite_store(tmp_path),
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One", "status": "active"},
        headers=ADMIN_HEADERS,
    )
    client.put(
        "/admin/tenants/tenant-1/entitlements",
        json={"features": {}, "limits": {"max_threads": 1, "max_messages_per_thread": 1}},
        headers=ADMIN_HEADERS,
    )

    first_thread_response = client.post("/threads", headers=AUTH_HEADERS)
    assert first_thread_response.status_code == 200
    thread_id = first_thread_response.json()["thread_id"]

    second_thread_response = client.post("/threads", headers=AUTH_HEADERS)
    assert second_thread_response.status_code == 429
    assert "max_threads" in second_thread_response.json()["detail"]

    first_message_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello"},
        headers=AUTH_HEADERS,
    )
    assert first_message_response.status_code == 200

    second_message_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "again"},
        headers=AUTH_HEADERS,
    )
    assert second_message_response.status_code == 429
    assert "max_messages_per_thread" in second_message_response.json()["detail"]


def test_tenant_entitlements_enforce_thread_run_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_REGISTRY_REQUIRED", "true")
    client = TestClient(
        create_app(
            admin_store=_sqlite_store(tmp_path),
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One", "status": "active"},
        headers=ADMIN_HEADERS,
    )
    client.put(
        "/admin/tenants/tenant-1/entitlements",
        json={"features": {}, "limits": {"max_thread_runs": 1}},
        headers=ADMIN_HEADERS,
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello"},
        headers=AUTH_HEADERS,
    )

    first_run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert first_run_response.status_code == 200

    second_run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert second_run_response.status_code == 429
    assert "max_thread_runs" in second_run_response.json()["detail"]

    stream_response = client.post(f"/threads/{thread_id}/run/stream", headers=AUTH_HEADERS)
    assert stream_response.status_code == 429
    assert "max_thread_runs" in stream_response.json()["detail"]


def test_tenant_entitlements_block_disabled_peer_agent_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_REGISTRY_REQUIRED", "true")
    store = _sqlite_store(tmp_path)
    client = TestClient(create_app(admin_store=store, tenant_config_source="store-with-defaults"))
    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One", "status": "active"},
        headers=ADMIN_HEADERS,
    )
    client.put(
        "/admin/tenants/tenant-1/entitlements",
        json={"features": {"peer_agents": False}, "limits": {}},
        headers=ADMIN_HEADERS,
    )
    client.put(
        "/admin/tenants/tenant-1/execution-config",
        json={
            "config": {
                "agent_backend": {
                    "type": "peer_agent",
                    "peer": "pi",
                    "cwd": "/workspace/project",
                    "mcp_broker_enabled": False,
                }
            }
        },
        headers=ADMIN_HEADERS,
    )

    create_response = client.post("/threads", headers=AUTH_HEADERS)

    assert create_response.status_code == 403
    assert create_response.json()["detail"] == "Tenant feature 'peer_agents' is disabled"


def test_tenant_registry_required_blocks_inactive_tenants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINIGENT_TENANT_REGISTRY_REQUIRED", "true")
    store = _sqlite_store(tmp_path)
    client = TestClient(
        create_app(
            admin_store=store,
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
        )
    )

    missing_response = client.post("/threads", headers=AUTH_HEADERS)
    assert missing_response.status_code == 403
    assert missing_response.json()["detail"] == "Tenant is not active"

    client.post(
        "/admin/tenants",
        json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One"},
        headers=ADMIN_HEADERS,
    )
    provisioning_response = client.post("/threads", headers=AUTH_HEADERS)
    assert provisioning_response.status_code == 403

    client.post("/admin/tenants/tenant-1/activate", headers=ADMIN_HEADERS)
    active_response = client.post("/threads", headers=AUTH_HEADERS)
    assert active_response.status_code == 200

    client.post("/admin/tenants/tenant-1/suspend", headers=ADMIN_HEADERS)
    suspended_response = client.post("/threads", headers=AUTH_HEADERS)
    assert suspended_response.status_code == 403


def test_admin_api_validates_tenant_execution_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_admin_api_lists_and_inspects_threads_with_tenant_isolation() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    tenant_thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{tenant_thread_id}/messages",
        json={"content": "tenant one message"},
        headers=AUTH_HEADERS,
    )
    client.post(f"/threads/{tenant_thread_id}/run", headers=AUTH_HEADERS)
    other_thread_id = client.post("/threads", headers=OTHER_TENANT_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{other_thread_id}/messages",
        json={"content": "tenant two message"},
        headers=OTHER_TENANT_HEADERS,
    )

    list_response = client.get("/admin/tenants/tenant-1/threads", headers=ADMIN_HEADERS)

    assert list_response.status_code == 200
    assert list_response.json()["tenant_id"] == "tenant-1"
    assert list_response.json()["limit"] == 50
    assert list_response.json()["offset"] == 0
    assert list_response.json()["total"] == 1
    assert list_response.json()["next_offset"] is None
    assert list_response.json()["threads"] == [
        {
            "thread_id": tenant_thread_id,
            "tenant_id": "tenant-1",
            "status": "idle",
            "created_at": list_response.json()["threads"][0]["created_at"],
            "updated_at": list_response.json()["threads"][0]["updated_at"],
            "skill_name": None,
            "skill_names": None,
            "capability_profile": None,
            "message_count": 2,
        }
    ]

    detail_response = client.get(
        f"/admin/tenants/tenant-1/threads/{tenant_thread_id}",
        headers=ADMIN_HEADERS,
    )

    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["message_count"] == 2
    assert detail["context"]["summary"] == ""
    assert [message["content"] for message in detail["messages"]] == [
        "tenant one message",
        "Mock reply: tenant one message",
    ]

    isolated_response = client.get(
        f"/admin/tenants/tenant-1/threads/{other_thread_id}",
        headers=ADMIN_HEADERS,
    )
    assert isolated_response.status_code == 404


def test_admin_api_filters_and_paginates_threads() -> None:
    store = InMemoryThreadStore()
    coding_thread_id = store.create_thread(
        "tenant-1",
        skill_name="coding",
        skill_names=["coding"],
        capability_profile="dev",
    ).thread_id
    research_thread_id = store.create_thread(
        "tenant-1",
        skill_names=["research", "writing"],
        capability_profile="default",
    ).thread_id
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=store,
        )
    )

    coding_response = client.get(
        "/admin/tenants/tenant-1/threads?skill=coding&profile=dev&limit=1",
        headers=ADMIN_HEADERS,
    )
    assert coding_response.status_code == 200
    assert coding_response.json()["total"] == 1
    assert coding_response.json()["threads"][0]["thread_id"] == coding_thread_id

    paged_response = client.get(
        "/admin/tenants/tenant-1/threads?limit=1&offset=1",
        headers=ADMIN_HEADERS,
    )
    assert paged_response.status_code == 200
    assert paged_response.json()["total"] == 2
    assert paged_response.json()["next_offset"] is None
    assert [thread["thread_id"] for thread in paged_response.json()["threads"]] == [
        coding_thread_id
    ]

    status_response = client.get(
        "/admin/tenants/tenant-1/threads?status=idle&skill=research",
        headers=ADMIN_HEADERS,
    )
    assert status_response.status_code == 200
    assert status_response.json()["total"] == 1
    assert status_response.json()["threads"][0]["thread_id"] == research_thread_id


def test_admin_api_deletes_thread_with_tenant_isolation() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    tenant_thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{tenant_thread_id}/messages",
        json={"content": "delete me"},
        headers=AUTH_HEADERS,
    )
    other_thread_id = client.post("/threads", headers=OTHER_TENANT_HEADERS).json()["thread_id"]

    isolated_response = client.delete(
        f"/admin/tenants/tenant-1/threads/{other_thread_id}",
        headers=ADMIN_HEADERS,
    )
    assert isolated_response.status_code == 404

    delete_response = client.delete(
        f"/admin/tenants/tenant-1/threads/{tenant_thread_id}",
        headers=ADMIN_HEADERS,
    )
    assert delete_response.status_code == 200
    assert delete_response.json() == {
        "deleted": True,
        "tenant_id": "tenant-1",
        "thread_id": tenant_thread_id,
    }
    assert (
        client.get(
            f"/admin/tenants/tenant-1/threads/{tenant_thread_id}",
            headers=ADMIN_HEADERS,
        ).status_code
        == 404
    )


def test_admin_api_prunes_threads_with_filters() -> None:
    store = InMemoryThreadStore()
    old_coding = store.create_thread("tenant-1", skill_name="coding", capability_profile="dev")
    old_research = store.create_thread("tenant-1", skill_name="research", capability_profile="dev")
    recent_coding = store.create_thread("tenant-1", skill_name="coding", capability_profile="dev")
    other_tenant = store.create_thread("tenant-2", skill_name="coding", capability_profile="dev")
    old_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 2, 1, tzinfo=timezone.utc)
    old_coding.updated_at = old_timestamp
    old_research.updated_at = old_timestamp
    other_tenant.updated_at = old_timestamp
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=store,
        )
    )

    prune_response = client.post(
        "/admin/tenants/tenant-1/threads/prune"
        "?updated_before=2026-02-01T00:00:00Z&skill=coding&profile=dev",
        headers=ADMIN_HEADERS,
    )

    assert prune_response.status_code == 200
    assert prune_response.json()["deleted_count"] == 1
    assert (
        client.get(
            f"/admin/tenants/tenant-1/threads/{old_coding.thread_id}", headers=ADMIN_HEADERS
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/admin/tenants/tenant-1/threads/{old_research.thread_id}", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/admin/tenants/tenant-1/threads/{recent_coding.thread_id}", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/admin/tenants/tenant-2/threads/{other_tenant.thread_id}", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    assert cutoff.isoformat().replace("+00:00", "Z") == "2026-02-01T00:00:00Z"


def test_admin_api_prune_dry_run_does_not_delete_or_audit() -> None:
    store = InMemoryThreadStore()
    thread = store.create_thread("tenant-1")
    thread.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=store,
        )
    )

    response = client.post(
        "/admin/tenants/tenant-1/threads/prune?updated_before=2026-02-01T00:00:00Z&dry_run=true",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["deleted_count"] == 0
    assert response.json()["dry_run"] is True
    assert response.json()["candidate_thread_ids"] == [thread.thread_id]
    assert (
        client.get(
            f"/admin/tenants/tenant-1/threads/{thread.thread_id}", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    audit_response = client.get("/admin/tenants/tenant-1/audit-records", headers=ADMIN_HEADERS)
    assert audit_response.status_code == 200
    assert audit_response.json()["audit_records"] == []


def test_admin_api_prune_deletes_sqlite_messages_durably(tmp_path: Path) -> None:
    db_path = tmp_path / "threads.db"
    store = SQLiteThreadStore(db_path)
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=store,
        )
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "persisted message"},
        headers=AUTH_HEADERS,
    )

    prune_response = client.post(
        "/admin/tenants/tenant-1/threads/prune?updated_before=2999-01-01T00:00:00Z",
        headers=ADMIN_HEADERS,
    )

    assert prune_response.status_code == 200
    assert prune_response.json()["deleted_count"] == 1
    restarted_client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=SQLiteThreadStore(db_path),
        )
    )
    assert (
        restarted_client.get(
            f"/admin/tenants/tenant-1/threads/{thread_id}", headers=ADMIN_HEADERS
        ).status_code
        == 404
    )
    audit_response = restarted_client.get(
        "/admin/tenants/tenant-1/audit-records", headers=ADMIN_HEADERS
    )
    assert audit_response.status_code == 200
    assert audit_response.json()["audit_records"][0]["action"] == "threads.prune"
    assert audit_response.json()["audit_records"][0]["actor_user_id"] == "admin-user"
    assert audit_response.json()["audit_records"][0]["affected_count"] == 1
    assert audit_response.json()["audit_records"][0]["thread_ids"] == [thread_id]


def test_admin_api_delete_writes_audit_record() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]

    delete_response = client.delete(
        f"/admin/tenants/tenant-1/threads/{thread_id}", headers=ADMIN_HEADERS
    )
    audit_response = client.get("/admin/tenants/tenant-1/audit-records", headers=ADMIN_HEADERS)

    assert delete_response.status_code == 200
    assert audit_response.status_code == 200
    assert audit_response.json()["audit_records"][0]["action"] == "threads.delete"
    assert audit_response.json()["audit_records"][0]["actor_user_id"] == "admin-user"
    assert audit_response.json()["audit_records"][0]["affected_count"] == 1
    assert audit_response.json()["audit_records"][0]["thread_ids"] == [thread_id]


def test_admin_api_audit_records_are_paginated_and_filtered() -> None:
    store = InMemoryThreadStore()
    old_delete = AuditRecord(
        tenant_id="tenant-1",
        actor_user_id="admin-user",
        action="threads.delete",
        affected_count=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    matching_prune = AuditRecord(
        tenant_id="tenant-1",
        actor_user_id="admin-user",
        action="threads.prune",
        affected_count=2,
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    other_actor = AuditRecord(
        tenant_id="tenant-1",
        actor_user_id="other-admin",
        action="threads.prune",
        affected_count=3,
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    other_tenant = AuditRecord(
        tenant_id="tenant-2",
        actor_user_id="admin-user",
        action="threads.prune",
        affected_count=4,
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    for record in [old_delete, matching_prune, other_actor, other_tenant]:
        store.append_audit_record(record)
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=store,
        )
    )

    response = client.get(
        "/admin/tenants/tenant-1/audit-records"
        "?limit=1&offset=0&action=threads.prune&actor=admin-user"
        "&created_after=2026-01-15T00:00:00Z&created_before=2026-03-01T00:00:00Z",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert body["total"] == 1
    assert body["next_offset"] is None
    assert [record["audit_id"] for record in body["audit_records"]] == [matching_prune.audit_id]


def test_admin_api_audit_records_pagination_metadata() -> None:
    store = InMemoryThreadStore()
    for index in range(3):
        store.append_audit_record(
            AuditRecord(
                tenant_id="tenant-1",
                actor_user_id="admin-user",
                action="threads.prune",
                affected_count=index,
                created_at=datetime(2026, 1, index + 1, tzinfo=timezone.utc),
            )
        )
    client = TestClient(
        create_app(
            llm_adapter=MockLLMAdapter(),
            tool_registry=build_local_tool_registry(),
            thread_store=store,
        )
    )

    response = client.get(
        "/admin/tenants/tenant-1/audit-records?limit=2&offset=0",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["next_offset"] == 2
    assert len(body["audit_records"]) == 2


def test_admin_thread_inspection_requires_admin() -> None:
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.get("/admin/tenants/tenant-1/threads", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"

    delete_response = client.delete(
        "/admin/tenants/tenant-1/threads/thread-1", headers=AUTH_HEADERS
    )
    assert delete_response.status_code == 403

    prune_response = client.post(
        "/admin/tenants/tenant-1/threads/prune?updated_before=2999-01-01T00:00:00Z",
        headers=AUTH_HEADERS,
    )
    assert prune_response.status_code == 403


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

    list_response = client.get("/admin/execution-config-tenants", headers=ADMIN_HEADERS)
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
