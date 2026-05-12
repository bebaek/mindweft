import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.llm import MockLLMAdapter
from app.mcp_broker import MCPBrokerSessionStore
from app.models import Principal
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

    with TestClient(app) as client:
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

    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "minigent-mcp-broker"
    assert tools.status_code == 200
    assert [tool["name"] for tool in tools.json()["result"]["tools"]] == ["echo"]
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
