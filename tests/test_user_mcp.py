from pathlib import Path

from fastapi.testclient import TestClient
from mcp.types import LATEST_PROTOCOL_VERSION

from app.admin_store import SQLiteTenantConfigStore
from app.main import create_app

USER_HEADERS = {
    "X-Minigent-User-Id": "user-1",
    "X-Minigent-Tenant-Id": "tenant-1",
}
OTHER_USER_HEADERS = {
    "X-Minigent-User-Id": "user-2",
    "X-Minigent-Tenant-Id": "tenant-1",
}
ADMIN_HEADERS = {
    "X-Minigent-User-Id": "admin-1",
    "X-Minigent-Tenant-Id": "tenant-1",
    "X-Minigent-Admin": "true",
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
        "/user-mcp",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


def test_user_mcp_requires_active_non_admin_user_and_lists_only_user_tools(tmp_path: Path) -> None:
    app = create_app(admin_store=SQLiteTenantConfigStore(str(tmp_path / "admin.db")))
    with TestClient(app) as client:
        rejected_admin = _request(
            client, 1, "server/discover", {"_meta": MCP_METADATA}, ADMIN_HEADERS
        )
        discovered = _request(client, 2, "server/discover", {"_meta": MCP_METADATA}, USER_HEADERS)
        listed = _request(client, 3, "tools/list", {"_meta": MCP_METADATA}, USER_HEADERS)
        status = _request(
            client,
            4,
            "tools/call",
            {"name": "get_user_execution_status", "arguments": {}, "_meta": MCP_METADATA},
            USER_HEADERS,
        )

    assert rejected_admin.status_code == 403
    assert discovered.status_code == 200
    assert listed.status_code == 200
    assert {tool["name"] for tool in listed.json()["result"]["tools"]} == {
        "get_user_execution_status",
        "get_user_execution_config",
        "validate_user_execution_config",
        "list_user_mcp_access",
    }
    assert status.status_code == 200
    result = status.json()["result"]["structuredContent"]
    assert result["tenant_id"] == "tenant-1"
    assert result["user_id"] == "user-1"
    assert "authorization" not in str(result).lower()


def test_user_mcp_config_is_principal_scoped_and_redacted(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"), encryption_key="user-mcp-test-key")
    app = create_app(admin_store=store)
    config = {
        "mcp_servers": {
            "items": [
                {
                    "id": "user:linear",
                    "name": "linear",
                    "url": "https://mcp.example.com/linear",
                    "credential_ref": "oauth:linear",
                    "headers": {"X-Workspace": "workspace-1"},
                }
            ]
        }
    }
    with TestClient(app) as client:
        saved = client.put(
            "/me/execution-config",
            headers=USER_HEADERS,
            json={"config": config, "expected_version": 0},
        )
        fetched = _request(
            client,
            1,
            "tools/call",
            {"name": "get_user_execution_config", "arguments": {}, "_meta": MCP_METADATA},
            USER_HEADERS,
        )
        other_user = _request(
            client,
            2,
            "tools/call",
            {"name": "get_user_execution_config", "arguments": {}, "_meta": MCP_METADATA},
            OTHER_USER_HEADERS,
        )

    assert saved.status_code == 200
    assert fetched.status_code == 200
    result = fetched.json()["result"]["structuredContent"]
    assert result["user_id"] == "user-1"
    assert result["config"]["mcp_servers"]["items"][0]["headers"] == {"X-Workspace": "<redacted>"}
    assert "oauth:linear" in fetched.text
    assert other_user.status_code == 200
    assert other_user.json()["result"]["isError"] is True
    assert "user-1" not in other_user.text


def test_user_mcp_validate_does_not_require_stored_config() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = _request(
            client,
            1,
            "tools/call",
            {
                "name": "validate_user_execution_config",
                "arguments": {"config": {"unknown": True}},
                "_meta": MCP_METADATA,
            },
            USER_HEADERS,
        )

    assert response.status_code == 200
    result = response.json()["result"]["structuredContent"]
    assert result["valid"] is False
    assert result["errors"]
