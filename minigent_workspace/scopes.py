from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple


class WorkspaceScope(NamedTuple):
    name: str
    roots: list[Path]
    description: str | None = None


def resolve_workspace_roots(
    cli_workspaces: list[str] | None, env_workspace: str | None
) -> list[Path]:
    if cli_workspaces:
        raw_workspaces = cli_workspaces
    elif env_workspace:
        separator = "," if "," in env_workspace else os.pathsep
        raw_workspaces = [item for item in env_workspace.split(separator) if item.strip()]
    else:
        raw_workspaces = []
    if not raw_workspaces:
        raw_workspaces = [str(Path.cwd())]
    return [Path(workspace).expanduser().resolve() for workspace in raw_workspaces]


def load_workspace_scopes_from_env(env: dict[str, str]) -> dict[str, WorkspaceScope]:
    raw = env.get("MINIGENT_CODING_WORKSPACE_SCOPES", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MINIGENT_CODING_WORKSPACE_SCOPES must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MINIGENT_CODING_WORKSPACE_SCOPES must be a JSON object")
    scopes: dict[str, WorkspaceScope] = {}
    for name, entry in payload.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("coding workspace scope names must be non-empty strings")
        if not isinstance(entry, dict):
            raise RuntimeError(f"coding workspace scope '{name}' must be an object")
        raw_roots = entry.get("roots")
        if (
            not isinstance(raw_roots, list)
            or not raw_roots
            or not all(isinstance(root, str) and root.strip() for root in raw_roots)
        ):
            raise RuntimeError(
                f"coding workspace scope '{name}' roots must be a non-empty string array"
            )
        description = entry.get("description")
        if description is not None and not isinstance(description, str):
            raise RuntimeError(f"coding workspace scope '{name}' description must be a string")
        scopes[name] = WorkspaceScope(
            name=name,
            roots=[Path(root).expanduser().resolve() for root in raw_roots],
            description=description,
        )
    return scopes


def skill_workspace_scope_from_env(env: dict[str, str], tenant_id: str) -> str | None:
    raw_config = env.get("MINIGENT_TENANT_EXECUTION_CONFIGS")
    if not raw_config:
        return None
    try:
        payload = json.loads(raw_config)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    tenant = payload.get(tenant_id)
    if not isinstance(tenant, dict):
        return None
    skills = tenant.get("skills")
    if not isinstance(skills, dict):
        return None
    default_skill = skills.get("default_skill") or skills.get("defaultSkill")
    if not isinstance(default_skill, str) or not default_skill:
        return None
    items = skills.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("name") != default_skill:
            continue
        scope = item.get("workspace_scope") or item.get("workspaceScope")
        return scope if isinstance(scope, str) and scope else None
    return None


def resolve_active_workspace_scope(
    workspace_roots: list[Path],
    env: dict[str, str],
    *,
    tenant_id: str,
    explicit_scope: str | None = None,
    validate_under_configured_roots: bool = False,
) -> tuple[list[Path], WorkspaceScope | None]:
    scopes = load_workspace_scopes_from_env(env)
    requested_scope = (
        explicit_scope
        or env.get("MINIGENT_CODING_WORKSPACE_SCOPE")
        or skill_workspace_scope_from_env(env, tenant_id)
        or env.get("MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE")
    )
    if not requested_scope:
        return workspace_roots, None
    if not scopes:
        raise RuntimeError(
            f"coding workspace scope '{requested_scope}' was requested, but no workspace scopes are configured"
        )
    scope = scopes.get(requested_scope)
    if scope is None:
        available = ", ".join(sorted(scopes)) or "none"
        raise RuntimeError(
            f"unknown coding workspace scope '{requested_scope}'. Available scopes: {available}"
        )
    if validate_under_configured_roots:
        outside_roots = [
            root
            for root in scope.roots
            if not any(
                root == workspace or workspace in root.parents for workspace in workspace_roots
            )
        ]
        if outside_roots:
            configured = ", ".join(str(root) for root in workspace_roots)
            outside = ", ".join(str(root) for root in outside_roots)
            raise RuntimeError(
                f"coding workspace scope '{scope.name}' contains roots outside configured workspaces: "
                f"{outside}. Configured workspaces: {configured}"
            )
    return scope.roots, scope


def resolve_workspace_selection(
    cli_workspaces: list[str] | None,
    explicit_scope: str | None,
    env: dict[str, str],
    *,
    tenant_id: str,
) -> tuple[list[Path], WorkspaceScope | None]:
    env_workspace = env.get("MINIGENT_CODING_WORKSPACES") or env.get("MINIGENT_CODING_WORKSPACE")
    workspace_roots = resolve_workspace_roots(cli_workspaces, env_workspace)
    workspace_roots, active_workspace_scope = resolve_active_workspace_scope(
        workspace_roots,
        env,
        tenant_id=tenant_id,
        explicit_scope=explicit_scope,
        validate_under_configured_roots=bool(cli_workspaces or env_workspace),
    )
    for workspace in workspace_roots:
        if not workspace.exists() or not workspace.is_dir():
            raise RuntimeError(f"Workspace does not exist or is not a directory: {workspace}")
    return workspace_roots, active_workspace_scope
