from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from minigent_config.unified_config import normalize_mindweft_env
from minigent_workspace.mcp_resolution import ResolvedMCPServers, resolve_workspace_mcp_servers
from minigent_workspace.runtime_settings import (
    WorkspaceRuntimeSettings,
    resolve_workspace_runtime_settings,
)
from minigent_workspace.scopes import WorkspaceScope, resolve_workspace_selection
from minigent_workspace.tenant_config import (
    apply_tenant_runtime_environment,
    tenant_gateway_mcp_server_mismatches,
)

DEFAULT_TENANT_ID = "demo-tenant"


@dataclass(frozen=True)
class WorkspaceRuntimePlan:
    env: dict[str, str]
    tenant_id: str
    workspace_roots: list[Path]
    active_workspace_scope: WorkspaceScope | None
    settings: WorkspaceRuntimeSettings
    mcp_servers: ResolvedMCPServers
    gateway_mcp_server_mismatches: list[str]


def prepare_workspace_runtime(
    args: argparse.Namespace,
    env: dict[str, str],
) -> WorkspaceRuntimePlan:
    normalize_mindweft_env(env)
    tenant_id = args.tenant_id or env.get("MINIGENT_CODING_TENANT_ID") or DEFAULT_TENANT_ID
    workspace_roots, active_workspace_scope = resolve_workspace_selection(
        args.workspace,
        args.workspace_scope,
        env,
        tenant_id=tenant_id,
    )
    settings = resolve_workspace_runtime_settings(args, env)
    mcp_servers = resolve_workspace_mcp_servers(
        args,
        env,
        tenant_id=tenant_id,
        workspace_roots=workspace_roots,
        settings=settings,
    )
    workspace_scope = active_workspace_scope.name if active_workspace_scope else None
    apply_tenant_runtime_environment(
        env,
        tenant_id,
        mcp_servers.tenant_specs,
        workspace_roots=workspace_roots,
        workspace_scope=workspace_scope,
    )
    gateway_mismatches = (
        tenant_gateway_mcp_server_mismatches(
            env,
            tenant_id,
            gateway_url_prefix=settings.gateway_url_prefix,
            specs=mcp_servers.process_specs,
        )
        if settings.gateway_enabled
        else []
    )
    return WorkspaceRuntimePlan(
        env=env,
        tenant_id=tenant_id,
        workspace_roots=workspace_roots,
        active_workspace_scope=active_workspace_scope,
        settings=settings,
        mcp_servers=mcp_servers,
        gateway_mcp_server_mismatches=gateway_mismatches,
    )
