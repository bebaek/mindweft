from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODERN_MCP_PROTOCOL_VERSION = "2026-07-28"


def test_official_mcp_v2_client_interoperates_with_text_server(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")

    async def run() -> None:
        server = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "app.text_mcp_server",
                "--workspace",
                str(tmp_path),
            ],
            cwd=PROJECT_ROOT,
        )
        async with Client(stdio_client(server)) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "read_text_file_lines",
                {"path": str(file_path), "start_line": 2, "end_line": 2},
            )

            assert client.protocol_version == MODERN_MCP_PROTOCOL_VERSION
            assert client.server_info is not None
            assert client.server_info.name == "minigent-text-mcp"
            assert [tool.name for tool in tools.tools] == [
                "read_text_file_lines",
                "read_text_file_around",
                "search_text_file",
            ]
            assert tools.result_type == "complete"
            assert result.result_type == "complete"
            assert result.structured_content is not None
            assert result.structured_content["content"] == "beta\n"

    asyncio.run(run())


def test_official_mcp_v2_client_interoperates_with_shell_server(tmp_path: Path) -> None:
    async def run() -> None:
        server = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "app.shell_mcp_server",
                "--workspace",
                str(tmp_path),
            ],
            cwd=PROJECT_ROOT,
        )
        async with Client(stdio_client(server)) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "run_command",
                {"command": "printf interop", "cwd": str(tmp_path)},
            )

            assert client.protocol_version == MODERN_MCP_PROTOCOL_VERSION
            assert client.server_info is not None
            assert client.server_info.name == "minigent-shell-mcp"
            assert [tool.name for tool in tools.tools] == ["run_command"]
            assert tools.result_type == "complete"
            assert result.result_type == "complete"
            assert result.structured_content is not None
            assert result.structured_content["exit_code"] == 0
            assert result.structured_content["stdout"] == "interop"

    asyncio.run(run())
