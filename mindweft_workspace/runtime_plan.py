from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from mindweft_config.unified_config import normalize_mindweft_env
from mindweft_workspace.mcp_resolution import ResolvedMCPServers, resolve_workspace_mcp_servers
from mindweft_workspace.runtime_settings import (
    WorkspaceRuntimeSettings,
    resolve_workspace_runtime_settings,
)
from mindweft_workspace.scopes import WorkspaceScope, resolve_workspace_selection
from mindweft_workspace.tenant_config import (
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
    *,
    bundled_readonly: bool = False,
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
        **({"bundled_readonly": True} if bundled_readonly else {}),
    )
    if bundled_readonly and env.get("MINDWEFT_LOCAL_GATEWAY_CREDENTIAL_DIR"):
        from mindweft_workspace.local_credentials import read_credential

        credential = read_credential(
            Path(env["MINDWEFT_LOCAL_GATEWAY_CREDENTIAL_DIR"]),
            launch_id=env["MINDWEFT_LOCAL_LAUNCH_ID"],
        )
        for spec in mcp_servers.tenant_specs:
            spec.headers["Authorization"] = f"Bearer {credential.token}"
            spec.headers["X-Mindweft-Launch-Id"] = credential.launch_id
    workspace_scope = active_workspace_scope.name if active_workspace_scope else None
    apply_tenant_runtime_environment(
        env,
        tenant_id,
        mcp_servers.tenant_specs,
        workspace_roots=workspace_roots,
        workspace_scope=workspace_scope,
    )
    if bundled_readonly:
        profile = "coding" if env.get("MINDWEFT_LOCAL_CODING_TRUSTED") == "1" else "inspect"
        configured = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])
        tenant = configured[tenant_id]
        tenant["capability_profiles"]["default_profile"] = profile
        if profile == "inspect":
            # Keep personal copies referencing shared:coding valid when the same
            # instance is restarted read-only. This alias cannot add any tools.
            inspect = next(
                item for item in tenant["capability_profiles"]["items"] if item["name"] == "inspect"
            )
            tenant["capability_profiles"]["items"].append({**inspect, "name": "coding"})

        tenant["agents"] = {
            "default_agent": "coding",
            "items": [
                {
                    "name": "coding",
                    "description": "Built-in coding assistant; duplicate in Personal setup to customize.",
                    "skills": ["coding-workspace"],
                    "capability_profile": "coding",
                }
            ],
        }
        for skill in tenant["skills"]["items"]:
            if skill["name"] == "coding-workspace":
                skill["system_prompt"] += (
                    "\nUse the available filesystem, text, and shell tools to implement and verify requested changes. "
                    "Read project instructions first; preserve unrelated work. Run targeted tests and inspect diffs. "
                    "Ask before commits, pushes, dependency installation, or destructive operations unless explicitly requested. "
                    "Never claim shell execution is sandboxed; do not access secrets without explicit permission. "
                    if profile == "coding"
                    else "\nThis launch is inspect-only: edits and shell execution are unavailable regardless of agent choice."
                )
        serialized = json.dumps(configured)
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = serialized
        env["MINDWEFT_TENANT_EXECUTION_CONFIGS"] = serialized
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
