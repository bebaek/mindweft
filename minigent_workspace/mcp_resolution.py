from __future__ import annotations

import argparse
from pathlib import Path
from typing import NamedTuple

from minigent_config.unified_config import normalize_mindweft_env
from minigent_workspace.launch_commands import build_builtin_mcp_server_specs
from minigent_workspace.mcp_specs import (
    CodingMCPServerSpec,
    load_coding_mcp_server_specs,
    load_coding_mcp_server_specs_from_json,
    mcp_server_specs_for_gateway,
    resolve_mcp_servers_file,
)
from minigent_workspace.runtime_settings import WorkspaceRuntimeSettings


class ResolvedMCPServers(NamedTuple):
    source_file: Path | None
    process_specs: list[CodingMCPServerSpec]
    tenant_specs: list[CodingMCPServerSpec]


def resolve_workspace_mcp_servers(
    args: argparse.Namespace,
    env: dict[str, str],
    *,
    tenant_id: str,
    workspace_roots: list[Path],
    settings: WorkspaceRuntimeSettings,
) -> ResolvedMCPServers:
    normalize_mindweft_env(env)
    source_file = resolve_mcp_servers_file(
        args.mcp_servers_file,
        env,
        base_dir=Path(args.env_file).expanduser().resolve().parent,
    )
    if env.get("MINIGENT_CODING_MCP_SERVER_SPECS"):
        process_specs = load_coding_mcp_server_specs_from_json(
            env["MINIGENT_CODING_MCP_SERVER_SPECS"],
            bridge_host=settings.bridge_host,
            workspace_roots=workspace_roots,
            env=env,
        )
    elif source_file is not None:
        process_specs = load_coding_mcp_server_specs(
            source_file,
            bridge_host=settings.bridge_host,
            workspace_roots=workspace_roots,
            env=env,
        )
    else:
        process_specs = build_builtin_mcp_server_specs(
            env,
            tenant_id,
            bridge_name=settings.bridge_name,
            bridge_host=settings.bridge_host,
            bridge_port=settings.bridge_port,
            bridge_url=settings.bridge_url,
            workspace_roots=workspace_roots,
            text_enabled=settings.text_enabled,
            text_bridge_name=settings.text_bridge_name,
            text_bridge_port=settings.text_bridge_port,
            text_bridge_url=settings.text_bridge_url,
            shell_enabled=settings.shell_enabled,
            shell_bridge_name=settings.shell_bridge_name,
            shell_bridge_port=settings.shell_bridge_port,
            shell_bridge_url=settings.shell_bridge_url,
        )
    tenant_specs = (
        mcp_server_specs_for_gateway(process_specs, settings.gateway_url_prefix)
        if settings.gateway_enabled
        else process_specs
    )
    return ResolvedMCPServers(source_file, process_specs, tenant_specs)
