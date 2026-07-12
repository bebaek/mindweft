import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.mcp import MCPPathPolicy
from app.mcp_stdio_bridge import BridgeSettings
from app.mcp_stdio_gateway import (
    GatewaySettings,
    bridge_settings_from_mapping,
    create_gateway_app,
    load_gateway_settings,
)

FAKE_STDIO_MCP_SERVER = r"""
import json
import sys

for line in sys.stdin:
    payload = json.loads(line)
    method = payload["method"]
    if "id" not in payload:
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "serverInfo": {"name": "fake-stdio", "version": "1.0.0"},
            "capabilities": {"tools": {}},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object"}},
                {"name": "read_file", "description": "Read file", "inputSchema": {"type": "object"}},
                {"name": "write_file", "description": "Write file", "inputSchema": {"type": "object"}},
                {"name": "list_directory", "description": "List directory", "inputSchema": {"type": "object"}}
            ]
        }
    elif method == "tools/call":
        arguments = payload["params"]["arguments"]
        result = {
            "structuredContent": {"echo": arguments.get("text", "")},
            "content": [{"type": "text", "text": arguments.get("text", "")}],
        }
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}), flush=True)
"""


def test_stdio_gateway_routes_to_multiple_servers(tmp_path: Path) -> None:
    client = TestClient(
        create_gateway_app(
            GatewaySettings(
                bridges=[
                    _bridge_settings(tmp_path, name="alpha"),
                    _bridge_settings(tmp_path, name="beta", allowed_tools=["read_file"]),
                ]
            )
        )
    )

    with client:
        alpha_initialize = client.post(
            "/mcp/alpha",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        beta_initialize = client.post(
            "/mcp/beta",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        alpha_tools = client.post(
            "/mcp/alpha",
            headers={"MCP-Session-Id": alpha_initialize.headers["mcp-session-id"]},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        beta_tools = client.post(
            "/mcp/beta",
            headers={"MCP-Session-Id": beta_initialize.headers["mcp-session-id"]},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert alpha_initialize.status_code == 200
    assert beta_initialize.status_code == 200
    assert alpha_tools.status_code == 200
    assert [tool["name"] for tool in alpha_tools.json()["result"]["tools"]] == [
        "echo",
        "read_file",
        "write_file",
        "list_directory",
    ]
    assert beta_tools.status_code == 200
    assert [tool["name"] for tool in beta_tools.json()["result"]["tools"]] == ["read_file"]


def test_stdio_gateway_keeps_server_sessions_separate(tmp_path: Path) -> None:
    client = TestClient(
        create_gateway_app(
            GatewaySettings(
                bridges=[
                    _bridge_settings(tmp_path, name="alpha"),
                    _bridge_settings(tmp_path, name="beta"),
                ]
            )
        )
    )

    with client:
        alpha_initialize = client.post(
            "/mcp/alpha",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        client.post(
            "/mcp/beta",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        wrong_server = client.post(
            "/mcp/beta",
            headers={"MCP-Session-Id": alpha_initialize.headers["mcp-session-id"]},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert wrong_server.status_code == 400
    assert wrong_server.json()["detail"] == "No valid MCP session ID provided"


def test_stdio_gateway_reports_unknown_server(tmp_path: Path) -> None:
    client = TestClient(create_gateway_app(GatewaySettings(bridges=[_bridge_settings(tmp_path)])))

    with client:
        response = client.post(
            "/mcp/missing",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown MCP server 'missing'"


def test_load_gateway_settings_reads_config(tmp_path: Path) -> None:
    script = _fake_server_script(tmp_path)
    config_path = tmp_path / "gateway.json"
    config_path.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 9000,
                "path_prefix": "/tools",
                "servers": [
                    {
                        "name": "alpha",
                        "command": [sys.executable, str(script), "ok"],
                        "allowed_tools": ["read_file"],
                        "path_policy": {
                            "deny_globs": ["**/.env*"],
                            "allow_globs": ["**/.env*.template"],
                        },
                        "restart_on_timeout": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    settings = load_gateway_settings(config_path)

    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.path_prefix == "/tools"
    assert len(settings.bridges) == 1
    assert settings.bridges[0].name == "alpha"
    assert settings.bridges[0].command == [sys.executable, str(script), "ok"]
    assert settings.bridges[0].allowed_tools == ["read_file"]
    assert settings.bridges[0].path_policy.deny_globs == ["**/.env*"]
    assert settings.bridges[0].path_policy.allow_globs == ["**/.env*.template"]
    assert settings.bridges[0].restart_on_timeout is True


def test_bridge_settings_from_mapping_validates_command() -> None:
    try:
        bridge_settings_from_mapping({"name": "bad", "command": "server"})
    except RuntimeError as exc:
        assert "command must be a string array" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def _bridge_settings(
    tmp_path: Path,
    *,
    name: str = "fake",
    allowed_tools: list[str] | None = None,
    path_policy: MCPPathPolicy | None = None,
) -> BridgeSettings:
    script = _fake_server_script(tmp_path)
    return BridgeSettings(
        name=name,
        command=[sys.executable, str(script), "ok"],
        request_timeout=2.0,
        allowed_tools=allowed_tools,
        path_policy=path_policy or MCPPathPolicy(),
    )


def _fake_server_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_stdio_mcp.py"
    script.write_text(FAKE_STDIO_MCP_SERVER, encoding="utf-8")
    return script
