from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class MCPServerSummary(Protocol):
    name: str
    url: str
    transport: str
    managed: bool


def print_workspace_summary(
    *,
    env_file: str,
    no_env_file: bool,
    env_file_explicit: bool,
    workspace_roots: Sequence[Path],
    workspace_scope: str | None,
    tenant_id: str,
    mcp_servers_file: Path | None,
    mcp_server_specs: Sequence[MCPServerSummary],
    tenant_mcp_server_specs: Sequence[MCPServerSummary],
    gateway_url_prefix: str | None,
    api_host: str,
    api_port: int,
) -> None:
    env_path = Path(env_file)
    if not no_env_file:
        if env_path.exists():
            print(f"loaded_env_file={env_file}")
        elif not env_file_explicit:
            print(f"optional_env_file_not_found={env_file}")
    print("workspaces=" + ", ".join(str(workspace) for workspace in workspace_roots))
    if workspace_scope is not None:
        print(f"workspace_scope={workspace_scope}")
    print(f"tenant_id={tenant_id}")
    if mcp_servers_file is not None:
        print(f"mcp_servers_file={mcp_servers_file} (legacy input; export emits inline specs)")
    for spec, tenant_spec in zip(mcp_server_specs, tenant_mcp_server_specs, strict=True):
        if spec.url == tenant_spec.url:
            print(
                f"mcp_server={spec.name} url={spec.url} transport={spec.transport} "
                f"managed={str(spec.managed).lower()}"
            )
        else:
            print(
                f"mcp_server={spec.name} url={tenant_spec.url} "
                f"transport={spec.transport} managed={str(spec.managed).lower()} "
                f"bridge_url={spec.url}"
            )
    if gateway_url_prefix is not None:
        print(f"mcp_gateway={gateway_url_prefix}")
    print(f"api=http://{api_host}:{api_port}")


def print_demo_commands(
    api_host: str,
    api_port: int,
    tenant_id: str,
    workspace: Path,
    bridge_name: str,
    text_bridge_name: str | None = None,
    shell_bridge_name: str | None = None,
) -> None:
    base_url = f"http://{api_host}:{api_port}"
    print("\nTry it from another shell:")
    tool_message = f"/tool {bridge_name}.list_directory {json.dumps({'path': str(workspace)}, separators=(',', ':'))}"
    print(
        "uv run python scripts/demo_client.py "
        f"--base-url {base_url} --tenant-id {tenant_id} --capability-profile inspect "
        f"{shlex.quote(tool_message)}"
    )
    if text_bridge_name is not None:
        text_message = f"/tool {text_bridge_name}.read_text_file_around " + json.dumps(
            {"path": str(workspace / "README.md"), "line": 1, "after": 20}, separators=(",", ":")
        )
        print(
            "uv run python scripts/demo_client.py "
            f"--base-url {base_url} --tenant-id {tenant_id} --capability-profile inspect "
            f"{shlex.quote(text_message)}"
        )
    if shell_bridge_name is not None:
        shell_message = f"/tool {shell_bridge_name}.run_command " + json.dumps(
            {"command": "pwd && ls", "cwd": str(workspace)}, separators=(",", ":")
        )
        print(
            "uv run python scripts/demo_client.py "
            f"--base-url {base_url} --tenant-id {tenant_id} --capability-profile test "
            f"{shlex.quote(shell_message)}"
        )
    print("\nPress Ctrl-C to stop.")
