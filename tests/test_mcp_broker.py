import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.llm import MockLLMAdapter
from app.mcp import MODERN_MCP_PROTOCOL_VERSION
from app.mcp_broker import MCPBrokerSessionStore
from app.models import Principal
from app.store import InMemoryThreadStore
from app.tools import build_local_tool_registry


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
    headers = {"Authorization": f"Bearer {session.token}"}
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

    assert discover.status_code == 200
    assert discover.json()["result"]["supportedVersions"] == [MODERN_MCP_PROTOCOL_VERSION]
    assert discover.json()["result"]["resultType"] == "complete"
    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "minigent-mcp-broker"
    assert tools.status_code == 200
    assert [tool["name"] for tool in tools.json()["result"]["tools"]] == ["echo"]
    assert modern_tools.json()["result"]["resultType"] == "complete"
    assert (
        modern_tools.json()["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
        == "minigent-mcp-broker"
    )
    assert call.status_code == 200
    assert call.json()["result"]["structuredContent"] == {"echo": "hello"}


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
