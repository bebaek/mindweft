from __future__ import annotations

from pathlib import Path

import pytest

from app.mcp import MODERN_MCP_PROTOCOL_VERSION
from app.shell_mcp_server import ShellMCPServer


def test_shell_mcp_server_lists_run_command_tool(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh")

    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response is not None
    assert response["result"]["tools"][0]["name"] == "run_command"


def test_shell_mcp_server_supports_modern_discovery_and_results(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh")
    metadata = {
        "io.modelcontextprotocol/protocolVersion": MODERN_MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1.0.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    discover = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {"_meta": metadata},
        }
    )
    tools = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {"_meta": metadata},
        }
    )

    assert discover is not None
    assert discover["result"]["supportedVersions"] == [MODERN_MCP_PROTOCOL_VERSION]
    assert discover["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == (
        "minigent-shell-mcp"
    )
    assert tools is not None
    assert tools["result"]["resultType"] == "complete"
    assert tools["result"]["ttlMs"] == 0
    assert tools["result"]["cacheScope"] == "private"


def test_run_command_executes_inside_workspace(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh")

    result = server.run_command({"command": "pwd", "cwd": str(tmp_path)})

    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["stdout"].strip() == str(tmp_path)
    assert result["cwd"] == str(tmp_path)


def test_run_command_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh")

    with pytest.raises(ValueError, match="cwd must be inside a workspace root"):
        server.run_command({"command": "pwd", "cwd": "/tmp"})


def test_run_command_allows_cwd_inside_any_configured_workspace(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    server = ShellMCPServer(workspaces=[tmp_path, other_workspace], shell="/bin/sh")

    result = server.run_command({"command": "pwd", "cwd": str(other_workspace)})

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == str(other_workspace)


def test_run_command_allows_configured_command_prefix(tmp_path: Path) -> None:
    server = ShellMCPServer(
        workspace=tmp_path,
        shell="/bin/sh",
        allowed_command_prefixes=["printf", "git status"],
    )

    result = server.run_command({"command": "printf ok"})

    assert result["exit_code"] == 0
    assert result["stdout"] == "ok"


def test_run_command_rejects_command_outside_prefix_allowlist(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh", allowed_command_prefixes=["git"])

    with pytest.raises(ValueError, match="command is not allowed"):
        server.run_command({"command": "cat .env"})


def test_run_command_truncates_output(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh", max_output_chars=20)

    result = server.run_command({"command": "printf 1234567890abcdef", "max_output_chars": 8})

    assert result["stdout"] == "12345678"
    assert result["stdout_truncated"] is True


def test_run_command_reports_timeout(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh")

    result = server.run_command({"command": "sleep 2", "timeout_seconds": 0.1})

    assert result["timed_out"] is True
    assert result["exit_code"] is not None
