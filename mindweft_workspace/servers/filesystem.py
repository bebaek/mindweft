"""Packaged filesystem MCP server; mutations require explicit writable mode."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from mindweft_workspace.servers.readonly import WorkspaceReadPolicy
from mindweft_workspace.servers.writable import WorkspaceWritePolicy

MAX_READ_CHARS = 40_000


class FilesystemMCPServer:
    def __init__(self, workspaces: Sequence[Path], *, writable: bool = False) -> None:
        self.policy = (
            WorkspaceWritePolicy(workspaces) if writable else WorkspaceReadPolicy(workspaces)
        )

    def list_allowed_directories(self) -> dict[str, object]:
        return {"directories": [str(root) for root in self.policy.roots]}

    def list_directory(self, path: str) -> dict[str, object]:
        return self.policy.list_directory(path)

    def read_file(self, path: str, max_chars: int = MAX_READ_CHARS) -> dict[str, object]:
        if type(max_chars) is not int or not 1 <= max_chars <= MAX_READ_CHARS:
            raise ValueError(f"max_chars must be between 1 and {MAX_READ_CHARS}")
        resolved, content = self.policy.read_text(path)
        return {
            "path": str(resolved),
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
        }


def build_filesystem_sdk_server(server: FilesystemMCPServer) -> MCPServer[Any]:
    sdk = MCPServer(
        "mindweft-filesystem-mcp",
        version="0.1.0",
        instructions="Workspace inspection and bounded text editing."
        if isinstance(server.policy, WorkspaceWritePolicy)
        else "Read-only workspace file inspection.",
    )

    @sdk.tool(structured_output=True)
    def list_allowed_directories() -> dict[str, object]:
        """List the canonical workspace roots this server may inspect."""
        return server.list_allowed_directories()

    @sdk.tool(structured_output=True)
    def list_directory(path: str) -> dict[str, object]:
        """List up to 1000 inspected entries; hide denied paths and escaping symlinks."""
        return server.list_directory(path)

    @sdk.tool(structured_output=True)
    def read_file(path: str, max_chars: int = MAX_READ_CHARS) -> dict[str, object]:
        """Read UTF-8 text (1 MiB file limit, up to 40000 characters returned)."""
        return server.read_file(path, max_chars)

    if isinstance(server.policy, WorkspaceWritePolicy):
        policy = server.policy

        @sdk.tool(structured_output=True)
        def write_file(path: str, content: str) -> dict[str, object]:
            """Create/replace UTF-8 text within workspace roots (1 MiB maximum); parents must exist."""
            return policy.write_text(path, content)

        @sdk.tool(structured_output=True)
        def edit_file(
            path: str, old_text: str, new_text: str, dry_run: bool = False
        ) -> dict[str, object]:
            """Replace exactly one matching text block; return a bounded unified diff."""
            return policy.edit_text(path, old_text, new_text, dry_run=dry_run)

    return sdk


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Packaged read-only filesystem MCP server.")
    parser.add_argument(
        "--workspace",
        action="append",
        required=True,
        help="Allowed root; repeat for multiple directories.",
    )
    parser.add_argument(
        "--writable", action="store_true", help="Enable workspace file creation and editing."
    )
    args = parser.parse_args(argv)
    build_filesystem_sdk_server(
        FilesystemMCPServer([Path(root) for root in args.workspace], writable=args.writable)
    ).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
