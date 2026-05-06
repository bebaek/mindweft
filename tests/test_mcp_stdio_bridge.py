import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.mcp_stdio_bridge import BridgeSettings, build_parser, create_bridge_app

FAKE_STDIO_MCP_SERVER = r'''
import json
import sys

mode = sys.argv[1]

if mode == "exit":
    sys.exit(7)

for line in sys.stdin:
    payload = json.loads(line)
    method = payload["method"]
    if "id" not in payload:
        continue
    if mode == "invalid-json" and method == "tools/list":
        print("not json", flush=True)
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
                {
                    "name": "echo",
                    "description": "Echo text",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                }
            ]
        }
    elif method == "tools/call":
        arguments = payload["params"]["arguments"]
        result = {
            "structuredContent": {"echo": arguments["text"]},
            "content": [{"type": "text", "text": arguments["text"]}],
        }
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}), flush=True)
'''


def test_stdio_bridge_initializes_lists_tools_and_calls_tool(tmp_path: Path) -> None:
    client = _client(tmp_path, "ok")

    with client:
        initialize = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1.0"},
                },
            },
        )
        session_id = initialize.headers["mcp-session-id"]

        notification = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        tools = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        call = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hello"}},
            },
        )

    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "fake-stdio"
    assert notification.status_code == 202
    assert tools.status_code == 200
    assert tools.json()["result"]["tools"][0]["name"] == "echo"
    assert call.status_code == 200
    assert call.json()["result"]["structuredContent"] == {"echo": "hello"}


def test_stdio_bridge_requires_session_after_initialize(tmp_path: Path) -> None:
    client = _client(tmp_path, "ok")

    with client:
        initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        bad_session = client.post(
            "/mcp",
            headers={"MCP-Session-Id": "wrong"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert initialize.status_code == 200
    assert bad_session.status_code == 400
    assert bad_session.json()["detail"] == "No valid MCP session ID provided"


def test_stdio_bridge_reports_invalid_subprocess_json(tmp_path: Path) -> None:
    client = _client(tmp_path, "invalid-json")

    with client:
        initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        response = client.post(
            "/mcp",
            headers={"MCP-Session-Id": initialize.headers["mcp-session-id"]},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "MCP stdio server returned invalid JSON"


def test_stdio_bridge_reports_exited_subprocess(tmp_path: Path) -> None:
    client = _client(tmp_path, "exit")

    with client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 502
    assert response.json()["detail"] in {
        "MCP stdio server closed stdin",
        "MCP stdio server closed stdout",
        "MCP stdio server exited with code 7",
    }


def test_stdio_bridge_parser_preserves_command_argv() -> None:
    parser = build_parser()

    args = parser.parse_args(
        ["--name", "demo", "--port", "9000", "--", "python", "server.py", "--token", "abc"]
    )

    assert args.name == "demo"
    assert args.port == 9000
    assert args.command == ["--", "python", "server.py", "--token", "abc"]


def _client(tmp_path: Path, mode: str) -> TestClient:
    script = tmp_path / "fake_stdio_mcp.py"
    script.write_text(FAKE_STDIO_MCP_SERVER)
    settings = BridgeSettings(
        name="fake",
        command=[sys.executable, str(script), mode],
        request_timeout=2.0,
    )
    return TestClient(create_bridge_app(settings))
