import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mcp import Client
from mcp import types as mcp_types
from mcp.shared.message import SessionMessage

from app.llm import MockLLMAdapter
from app.mcp import LEGACY_MCP_PROTOCOL_VERSION, MODERN_MCP_PROTOCOL_VERSION
from app.mcp_broker import MCPBrokerSessionStore, MCPBrokerStoreSettings
from app.models import Principal
from app.store import InMemoryThreadStore
from app.tools import build_local_tool_registry


def test_mcp_broker_store_settings_prefers_mindweft_env() -> None:
    assert MCPBrokerStoreSettings.from_env(
        {
            "MINDWEFT_MCP_BROKER_DB_PATH": " .data/mindweft-broker.db ",
            "MINIGENT_MCP_BROKER_DB_PATH": ".data/legacy-broker.db",
        }
    ) == MCPBrokerStoreSettings(db_path=".data/mindweft-broker.db")
    assert MCPBrokerStoreSettings.from_env(
        {"MINIGENT_MCP_BROKER_DB_PATH": ".data/legacy-broker.db"}
    ) == MCPBrokerStoreSettings(db_path=".data/legacy-broker.db")


def test_mcp_broker_lists_and_calls_session_tools() -> None:
    from app.main import create_app

    registry = build_local_tool_registry(allowed_tools=["echo"])
    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=registry)
    session = app.state.mcp_broker_sessions.create_session(
        principal=Principal(user_id="user-1", tenant_id="tenant-1"),
        thread_id="thread-1",
        tool_registry=registry,
        ttl_seconds=60,
    )
    headers = {"X-Mindweft-MCP-Broker-Token": session.token}
    modern_metadata = {
        "io.modelcontextprotocol/protocolVersion": MODERN_MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1.0.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    with TestClient(app) as client:
        discover = client.post(
            f"/mcp/peer/{session.session_id}",
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "server/discover",
                "params": {"_meta": modern_metadata},
            },
            headers=headers,
        )
        initialize = client.post(
            f"/mcp/peer/{session.session_id}",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers=headers,
        )
        tools = client.post(
            f"/mcp/peer/{session.session_id}",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=headers,
        )
        call = client.post(
            f"/mcp/peer/{session.session_id}",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hello"}},
            },
            headers=headers,
        )
        modern_tools = client.post(
            f"/mcp/peer/{session.session_id}",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/list",
                "params": {"_meta": modern_metadata},
            },
            headers=headers,
        )
        modern_call = client.post(
            f"/mcp/peer/{session.session_id}",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "echo",
                    "arguments": {"text": "modern"},
                    "_meta": modern_metadata,
                },
            },
            headers=headers,
        )
        invalid_call = client.post(
            f"/mcp/peer/{session.session_id}",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": ["not", "an", "object"]},
            },
            headers=headers,
        )

    assert discover.status_code == 200
    assert discover.json()["result"]["supportedVersions"] == [MODERN_MCP_PROTOCOL_VERSION]
    assert discover.json()["result"]["resultType"] == "complete"
    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "mindweft-mcp-broker"
    assert tools.status_code == 200
    assert [tool["name"] for tool in tools.json()["result"]["tools"]] == ["echo"]
    assert "resultType" not in tools.json()["result"]
    assert "_meta" not in tools.json()["result"]
    assert modern_tools.json()["result"]["resultType"] == "complete"
    assert (
        modern_tools.json()["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
        == "mindweft-mcp-broker"
    )
    assert call.status_code == 200
    assert call.json()["result"]["structuredContent"] == {"echo": "hello"}
    assert "resultType" not in call.json()["result"]
    assert "_meta" not in call.json()["result"]
    assert modern_call.json()["result"]["resultType"] == "complete"
    assert (
        modern_call.json()["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
        == "mindweft-mcp-broker"
    )
    assert invalid_call.json()["error"]["code"] == -32602


def test_official_sdk_client_interoperates_with_mcp_broker() -> None:
    from app.main import create_app

    registry = build_local_tool_registry(allowed_tools=["echo"])
    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=registry)
    session = app.state.mcp_broker_sessions.create_session(
        principal=Principal(user_id="user-1", tenant_id="tenant-1"),
        thread_id="thread-1",
        tool_registry=registry,
        ttl_seconds=60,
    )

    with TestClient(app) as http_client:

        async def run() -> None:
            transport = _broker_test_transport(
                http_client,
                f"/mcp/peer/{session.session_id}",
                {"Authorization": f"Bearer {session.token}"},
            )
            async with Client(transport) as sdk_client:
                tools = await sdk_client.list_tools()
                result = await sdk_client.call_tool("echo", {"text": "sdk"})

                assert sdk_client.protocol_version == MODERN_MCP_PROTOCOL_VERSION
                assert sdk_client.server_info is not None
                assert sdk_client.server_info.name == "mindweft-mcp-broker"
                assert [tool.name for tool in tools.tools] == ["echo"]
                assert result.structured_content == {"echo": "sdk"}

            legacy_transport = _broker_test_transport(
                http_client,
                f"/mcp/peer/{session.session_id}",
                {"Authorization": f"Bearer {session.token}"},
            )
            async with Client(legacy_transport, mode="legacy") as legacy_client:
                legacy_tools = await legacy_client.list_tools()
                legacy_result = await legacy_client.call_tool("echo", {"text": "legacy sdk"})

                assert legacy_client.protocol_version == LEGACY_MCP_PROTOCOL_VERSION
                assert [tool.name for tool in legacy_tools.tools] == ["echo"]
                assert legacy_result.structured_content == {"echo": "legacy sdk"}

        asyncio.run(run())


@asynccontextmanager
async def _broker_test_transport(
    client: TestClient,
    path: str,
    headers: dict[str, str],
) -> AsyncIterator[tuple[Any, Any]]:
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)

    async def exchange() -> None:
        async with read_writer, write_reader:
            async for message in write_reader:
                payload = message.message.model_dump(
                    by_alias=True,
                    mode="json",
                    exclude_unset=True,
                )
                response = await asyncio.to_thread(
                    client.post,
                    path,
                    headers=headers,
                    json=payload,
                )
                if response.status_code == 202:
                    continue
                response_message = mcp_types.jsonrpc_message_adapter.validate_python(
                    response.json(), by_name=False
                )
                await read_writer.send(SessionMessage(response_message))

    async with read_stream, write_stream, anyio.create_task_group() as task_group:
        task_group.start_soon(exchange)
        try:
            yield read_stream, write_stream
        finally:
            task_group.cancel_scope.cancel()


def test_mcp_broker_rejects_invalid_token() -> None:
    store = MCPBrokerSessionStore()
    session = store.create_session(
        principal=Principal(user_id="user-1", tenant_id="tenant-1"),
        thread_id="thread-1",
        tool_registry=build_local_tool_registry(allowed_tools=["echo"]),
        ttl_seconds=60,
    )

    with pytest.raises(HTTPException) as exc_info:
        store.require_session(session.session_id, "wrong")

    assert exc_info.value.status_code == 401


def test_mcp_broker_expires_sessions() -> None:
    store = MCPBrokerSessionStore()
    session = store.create_session(
        principal=Principal(user_id="user-1", tenant_id="tenant-1"),
        thread_id="thread-1",
        tool_registry=build_local_tool_registry(allowed_tools=["echo"]),
        ttl_seconds=-1,
    )

    with pytest.raises(HTTPException) as exc_info:
        store.require_session(session.session_id, session.token)

    assert exc_info.value.status_code == 404


def test_sqlite_mcp_broker_session_works_from_another_app_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.main import create_app

    database = tmp_path / "mcp-broker.db"
    monkeypatch.setenv("MINIGENT_MCP_BROKER_DB_PATH", str(database))
    thread_store = InMemoryThreadStore()
    thread = thread_store.create_thread("tenant-1")
    creator_registry = build_local_tool_registry(allowed_tools=["echo"])
    resolver_registry = build_local_tool_registry(allowed_tools=["echo", "current_time"])
    creator = create_app(
        thread_store=thread_store,
        llm_adapter=MockLLMAdapter(),
        tool_registry=creator_registry,
    )
    resolver = create_app(
        thread_store=thread_store,
        llm_adapter=MockLLMAdapter(),
        tool_registry=resolver_registry,
    )
    session = creator.state.mcp_broker_sessions.create_session(
        principal=Principal(user_id="user-1", tenant_id="tenant-1"),
        thread_id=thread.thread_id,
        tool_registry=creator_registry,
        ttl_seconds=60,
    )

    with TestClient(resolver) as client:
        tools = client.post(
            f"/mcp/peer/{session.session_id}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {session.token}"},
        )
        call = client.post(
            f"/mcp/peer/{session.session_id}",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "cross replica"}},
            },
            headers={"Authorization": f"Bearer {session.token}"},
        )
        expanded_call = client.post(
            f"/mcp/peer/{session.session_id}",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "current_time", "arguments": {}},
            },
            headers={"Authorization": f"Bearer {session.token}"},
        )

    assert [tool["name"] for tool in tools.json()["result"]["tools"]] == ["echo"]
    assert call.json()["result"]["structuredContent"] == {"echo": "cross replica"}
    assert expanded_call.json()["error"]["message"] == "Unknown tool 'current_time'"
    with sqlite3.connect(database) as connection:
        stored_hash = connection.execute(
            "SELECT token_hash FROM mcp_broker_sessions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
    assert stored_hash != session.token
    assert session.token not in database.read_bytes().decode("utf-8", errors="ignore")
