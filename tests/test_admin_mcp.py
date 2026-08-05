from fastapi.testclient import TestClient
from mcp.types import LATEST_PROTOCOL_VERSION

from app.admin_mcp import ADMIN_CHAT_SETUP_TOOL
from app.llm import MockLLMAdapter
from app.main import create_app
from app.tools import build_local_tool_registry

ADMIN_HEADERS = {
    "X-Minigent-User-Id": "admin-1",
    "X-Minigent-Tenant-Id": "tenant-1",
    "X-Minigent-Admin": "true",
}
USER_HEADERS = {
    "X-Minigent-User-Id": "user-1",
    "X-Minigent-Tenant-Id": "tenant-1",
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
}
MCP_METADATA = {
    "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "minigent-test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _request(
    client: TestClient, request_id: int, method: str, params: dict, headers: dict[str, str]
):
    mcp_headers = {**MCP_HEADERS, **headers, "Mcp-Method": method}
    if method == "tools/call" and isinstance(params.get("name"), str):
        mcp_headers["Mcp-Name"] = params["name"]
    return client.post(
        "/mcp",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


def test_admin_mcp_requires_admin_and_exposes_only_read_tools() -> None:
    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    with TestClient(app) as client:
        rejected = _request(client, 1, "server/discover", {"_meta": MCP_METADATA}, USER_HEADERS)
        assert rejected.status_code == 403

        discovered = _request(client, 2, "server/discover", {"_meta": MCP_METADATA}, ADMIN_HEADERS)
        listed = _request(client, 3, "tools/list", {"_meta": MCP_METADATA}, ADMIN_HEADERS)
        status = _request(
            client,
            4,
            "tools/call",
            {"name": "get_setup_status", "arguments": {}, "_meta": MCP_METADATA},
            ADMIN_HEADERS,
        )

    assert discovered.status_code == 200
    assert discovered.json()["result"]["supportedVersions"] == [LATEST_PROTOCOL_VERSION]
    assert listed.status_code == 200
    assert {tool["name"] for tool in listed.json()["result"]["tools"]} == {
        "get_setup_status",
        "diagnose_tenant_setup",
        "list_mcp_server_catalog_access",
    }
    assert status.status_code == 200
    result = status.json()["result"]["structuredContent"]
    assert "readiness_checks" in result
    assert "authorization" not in str(result).lower()


def _chat_with_tool(client: TestClient, headers: dict[str, str], tool_name: str) -> str:
    created = client.post("/threads", headers=headers)
    assert created.status_code == 200
    thread_id = created.json()["thread_id"]
    message = client.post(
        f"/threads/{thread_id}/messages",
        headers=headers,
        json={"content": f"/tool {tool_name}"},
    )
    assert message.status_code == 200
    run = client.post(f"/threads/{thread_id}/run", headers=headers)
    assert run.status_code == 200
    return run.json()["reply"]


def test_admin_chat_can_call_minigent_admin_tools_in_process() -> None:
    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    with TestClient(app) as client:
        admin_reply = _chat_with_tool(client, ADMIN_HEADERS, ADMIN_CHAT_SETUP_TOOL)
        user_reply = _chat_with_tool(client, USER_HEADERS, ADMIN_CHAT_SETUP_TOOL)

    assert admin_reply.startswith("Tool result:")
    assert "readiness_checks" in admin_reply
    assert user_reply == f"Mock reply: /tool {ADMIN_CHAT_SETUP_TOOL}"
