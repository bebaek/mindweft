import asyncio
import sys
import time
from pathlib import Path

import httpx2
from fastapi.testclient import TestClient

from app.mcp import MCPHTTPClient, MCPPathPolicy, MCPServerConfig
from app.mcp_stdio_bridge import BridgeSettings, build_parser, create_bridge_app

FAKE_STDIO_MCP_SERVER = r"""
import json
import os
import sys
import time

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
    if mode == "delayed-tools-list" and method == "tools/list":
        time.sleep(0.75)
    if mode == "large-line" and method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": [{"name": "large", "description": "x" * 200000, "inputSchema": {"type": "object"}}], "resultType": "complete", "ttlMs": 0, "cacheScope": "private"}}), flush=True)
        continue
    if method == "server/discover":
        result = {
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
            "resultType": "complete",
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "fake-stdio", "version": "2.0.0"}},
        }
    elif method == "initialize":
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
                },
                {
                    "name": "read_file",
                    "description": "Read file",
                    "inputSchema": {"type": "object"},
                },
                {
                    "name": "write_file",
                    "description": "Write file",
                    "inputSchema": {"type": "object"},
                },
                {
                    "name": "edit_file",
                    "description": "Edit file",
                    "inputSchema": {"type": "object"},
                },
                {
                    "name": "list_directory",
                    "description": "List directory",
                    "inputSchema": {"type": "object"},
                }
            ]
        }
    elif method == "tools/call":
        tool_name = payload["params"]["name"]
        arguments = payload["params"]["arguments"]
        if tool_name == "list_directory":
            result = {
                "content": [{"type": "text", "text": "[FILE] README.md\n[FILE] .env\n[FILE] .env.template\n[DIR] .git"}],
            }
        elif mode == "chmod-edit" and tool_name == "edit_file":
            path = arguments["path"]
            with open(path, "w", encoding="utf-8") as file:
                file.write("edited\n")
            os.chmod(path, 0o644)
            result = {
                "content": [{"type": "text", "text": "edited"}],
            }
        else:
            result = {
                "structuredContent": {"echo": arguments.get("text", "")},
                "content": [{"type": "text", "text": arguments.get("text", "")}],
            }
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "not found"}}), flush=True)
        continue
    metadata = payload.get("params", {}).get("_meta", {})
    if metadata.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28":
        result.update({"resultType": "complete", "ttlMs": 0, "cacheScope": "private"})
    print(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}), flush=True)
"""


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
    assert "resultType" not in tools.json()["result"]
    assert "_meta" not in tools.json()["result"]
    assert call.status_code == 200
    assert call.json()["result"]["structuredContent"] == {"echo": "hello"}
    assert "resultType" not in call.json()["result"]
    assert "_meta" not in call.json()["result"]


def test_stdio_bridge_allows_modern_stateless_requests_without_session(tmp_path: Path) -> None:
    client = _client(tmp_path, "ok")
    metadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1.0.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    with client:
        discover = client.post(
            "/mcp",
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "MCP-Method": "server/discover",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": metadata},
            },
        )
        tools = client.post(
            "/mcp",
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "MCP-Method": "tools/list",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": metadata},
            },
        )

    assert discover.status_code == 200
    assert discover.json()["result"]["supportedVersions"] == ["2026-07-28"]
    assert "mcp-session-id" not in discover.headers
    assert tools.status_code == 200
    assert tools.json()["result"]["tools"][0]["name"] == "echo"


def test_stdio_bridge_interoperates_with_sdk_v2_text_server(tmp_path: Path) -> None:
    client = TestClient(
        create_bridge_app(
            BridgeSettings(
                name="text-sdk",
                command=[
                    sys.executable,
                    "-m",
                    "app.text_mcp_server",
                    "--workspace",
                    str(tmp_path),
                ],
            )
        )
    )
    metadata = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1.0.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    with client:
        discover = client.post(
            "/mcp",
            headers={"MCP-Protocol-Version": "2026-07-28"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": metadata},
            },
        )
        modern_tools = client.post(
            "/mcp",
            headers={"MCP-Protocol-Version": "2026-07-28"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": metadata},
            },
        )

    assert discover.json()["result"]["supportedVersions"] == ["2026-07-28"]
    assert "mcp-session-id" not in discover.headers
    assert modern_tools.status_code == 200
    assert modern_tools.json()["result"]["resultType"] == "complete"
    assert modern_tools.json()["result"]["tools"][0]["name"] == "read_text_file_lines"


def test_sdk_http_client_interoperates_with_sdk_text_server_through_bridge(
    tmp_path: Path,
) -> None:
    bridge_app = create_bridge_app(
        BridgeSettings(
            name="text-sdk",
            command=[
                sys.executable,
                "-m",
                "app.text_mcp_server",
                "--workspace",
                str(tmp_path),
            ],
        )
    )

    async def run() -> None:
        async with bridge_app.router.lifespan_context(bridge_app):
            client = MCPHTTPClient(
                MCPServerConfig(name="text", url="http://testserver/mcp", headers={}),
                transport=httpx2.ASGITransport(app=bridge_app),
            )
            tools = await client.list_tools()

        assert [tool.name for tool in tools] == [
            "text.read_text_file_lines",
            "text.read_text_file_around",
            "text.search_text_file",
        ]
        assert client.server_info().protocol_version == "2026-07-28"
        assert client.server_info().server_name == "minigent-text-mcp"

    asyncio.run(run())


def test_stdio_bridge_keeps_multiple_legacy_sessions_valid(tmp_path: Path) -> None:
    client = _client(tmp_path, "ok")

    with client:
        first_initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        second_initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
        )
        first_tools = client.post(
            "/mcp",
            headers={"MCP-Session-Id": first_initialize.headers["mcp-session-id"]},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        second_tools = client.post(
            "/mcp",
            headers={"MCP-Session-Id": second_initialize.headers["mcp-session-id"]},
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        )

    assert first_initialize.status_code == 200
    assert second_initialize.status_code == 200
    assert first_initialize.headers["mcp-session-id"] != second_initialize.headers["mcp-session-id"]
    assert first_tools.status_code == 200
    assert second_tools.status_code == 200


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


def test_stdio_bridge_reads_large_subprocess_response_lines(tmp_path: Path) -> None:
    client = _client(tmp_path, "large-line", stdio_stream_limit=256000)

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

    assert response.status_code == 200
    assert len(response.json()["result"]["tools"][0]["description"]) == 200000


def test_stdio_bridge_reports_oversized_subprocess_response_lines(tmp_path: Path) -> None:
    client = _client(tmp_path, "large-line", stdio_stream_limit=1024)

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
    assert response.json()["detail"] == "MCP stdio server response exceeded stream buffer limit"


def test_stdio_bridge_skips_stale_response_after_timeout(tmp_path: Path) -> None:
    client = _client(tmp_path, "delayed-tools-list", request_timeout=0.3)

    with client:
        initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        session_id = initialize.headers["mcp-session-id"]
        timed_out = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        time.sleep(0.8)
        response = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "fresh"}},
            },
        )

    assert timed_out.status_code == 504
    assert response.status_code == 200
    assert response.json()["id"] == 3
    assert response.json()["result"]["structuredContent"] == {"echo": "fresh"}


def test_stdio_bridge_can_restart_subprocess_after_timeout(tmp_path: Path) -> None:
    client = _client(tmp_path, "delayed-tools-list", request_timeout=0.3, restart_on_timeout=True)

    with client:
        initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        session_id = initialize.headers["mcp-session-id"]
        timed_out = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        response = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "after restart"}},
            },
        )

    assert timed_out.status_code == 504
    assert response.status_code == 200
    assert response.json()["id"] == 3
    assert response.json()["result"]["structuredContent"] == {"echo": "after restart"}


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
        "MCP stdio server request failed: Connection closed",
        "MCP stdio server request failed: 502: MCP stdio server exited with code 7",
    }


def test_stdio_bridge_filters_tools_and_denies_disallowed_calls(tmp_path: Path) -> None:
    client = _client(tmp_path, "ok", allowed_tools=["read_file"])

    with client:
        initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        session_id = initialize.headers["mcp-session-id"]
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
                "params": {"name": "write_file", "arguments": {"path": "/tmp/x"}},
            },
        )

    assert [tool["name"] for tool in tools.json()["result"]["tools"]] == ["read_file"]
    assert call.status_code == 403
    assert "not allowed" in call.json()["detail"]


def test_stdio_bridge_preserves_existing_file_mode_after_edit_tool(tmp_path: Path) -> None:
    target = tmp_path / "script.sh"
    target.write_text("before\n", encoding="utf-8")
    target.chmod(0o755)
    client = _client(tmp_path, "chmod-edit", allowed_tools=["edit_file"])

    with client:
        initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        response = client.post(
            "/mcp",
            headers={"MCP-Session-Id": initialize.headers["mcp-session-id"]},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "edit_file", "arguments": {"path": str(target)}},
            },
        )

    assert response.status_code == 200
    assert target.read_text(encoding="utf-8") == "edited\n"
    assert target.stat().st_mode & 0o777 == 0o755


def test_stdio_bridge_denies_paths_and_filters_directory_listing(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        "ok",
        allowed_tools=["read_file", "list_directory"],
        path_policy=MCPPathPolicy(
            deny_globs=["**/.env*", "**/.git/**"],
            allow_globs=["**/.env*.template"],
        ),
    )

    with client:
        initialize = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        session_id = initialize.headers["mcp-session-id"]
        denied = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "/repo/.env"}},
            },
        )
        allowed = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "read_file",
                    "arguments": {"path": "/repo/.env.coding.template"},
                },
            },
        )
        listing = client.post(
            "/mcp",
            headers={"MCP-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "list_directory", "arguments": {"path": "/repo"}},
            },
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    text = listing.json()["result"]["content"][0]["text"]
    assert "README.md" in text
    assert ".env.template" in text
    assert "[FILE] .env\n" not in text
    assert ".git" not in text


def test_stdio_bridge_parser_preserves_command_argv() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "--name",
            "demo",
            "--port",
            "9000",
            "--stdio-stream-limit",
            "123456",
            "--allowed-tool",
            "read_file",
            "--deny-glob",
            "**/.env*",
            "--allow-glob",
            "**/.env*.template",
            "--",
            "python",
            "server.py",
            "--token",
            "abc",
        ]
    )

    assert args.name == "demo"
    assert args.port == 9000
    assert args.stdio_stream_limit == 123456
    assert args.allowed_tool == ["read_file"]
    assert args.deny_glob == ["**/.env*"]
    assert args.allow_glob == ["**/.env*.template"]
    assert args.command == ["--", "python", "server.py", "--token", "abc"]


def _client(
    tmp_path: Path,
    mode: str,
    *,
    allowed_tools: list[str] | None = None,
    path_policy: MCPPathPolicy | None = None,
    stdio_stream_limit: int | None = None,
    request_timeout: float = 2.0,
    restart_on_timeout: bool = False,
) -> TestClient:
    script = tmp_path / "fake_stdio_mcp.py"
    script.write_text(FAKE_STDIO_MCP_SERVER)
    settings = BridgeSettings(
        name="fake",
        command=[sys.executable, str(script), mode],
        request_timeout=request_timeout,
        stdio_stream_limit=stdio_stream_limit or 16 * 1024 * 1024,
        allowed_tools=allowed_tools,
        path_policy=path_policy or MCPPathPolicy(),
        restart_on_timeout=restart_on_timeout,
    )
    return TestClient(create_bridge_app(settings))
