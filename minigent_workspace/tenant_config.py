from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from minigent_config.unified_config import normalize_mindweft_env
from minigent_workspace.mcp_specs import CodingMCPServerSpec

DEFAULT_BRIDGE_ALLOWED_TOOLS = (
    "list_allowed_directories",
    "list_directory",
    "read_file",
)
DEFAULT_BRIDGE_DENY_GLOBS = (
    "**/.env*",
    "**/.git/**",
    "**/.venv/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/.uv-cache/**",
)
DEFAULT_BRIDGE_ALLOW_GLOBS = ("**/.env*.template",)
DEFAULT_SHELL_BRIDGE_NAME = "shell-workspace"
DEFAULT_SHELL_BRIDGE_PORT = 8766
DEFAULT_TEXT_BRIDGE_NAME = "text-workspace"
DEFAULT_TEXT_BRIDGE_PORT = 8767


def apply_tenant_runtime_environment(
    env: dict[str, str],
    tenant_id: str,
    specs: list[CodingMCPServerSpec],
    *,
    workspace_roots: list[Path],
    workspace_scope: str | None,
) -> None:
    normalize_mindweft_env(env)
    env.setdefault("MINIGENT_AUTH_MODE", "dev-headers")
    env.setdefault("MINIGENT_LLM_PROVIDER", "mock")
    env["MINIGENT_CODING_TENANT_ID"] = tenant_id
    env.setdefault("MINIGENT_CODING_OAUTH_GLOBAL_FALLBACK", "true")
    if "MINIGENT_TENANT_EXECUTION_CONFIGS" not in env:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = json.dumps(
            default_tenant_config_from_servers(
                tenant_id,
                specs,
                workspace_roots=workspace_roots,
                workspace_scope=workspace_scope,
            ),
            separators=(",", ":"),
        )
    else:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = inject_coding_mcp_servers(
            env["MINIGENT_TENANT_EXECUTION_CONFIGS"], tenant_id, specs
        )
    if "MINIGENT_CODING_INJECT_WORKSPACE_SKILL" not in env or env[
        "MINIGENT_CODING_INJECT_WORKSPACE_SKILL"
    ].lower() not in {
        "0",
        "false",
        "no",
    }:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = inject_coding_workspace_skill(
            env["MINIGENT_TENANT_EXECUTION_CONFIGS"],
            tenant_id,
            workspace_roots=workspace_roots,
            workspace_scope=workspace_scope,
        )


def tenant_mcp_server_from_spec(spec: CodingMCPServerSpec) -> dict[str, Any]:
    server: dict[str, Any] = {
        "name": spec.name,
        "url": spec.url,
        "headers": dict(spec.headers),
        "timeout_seconds": spec.timeout_seconds,
    }
    if spec.allowed_tools is not None:
        server["allowed_tools"] = list(spec.allowed_tools)
    if spec.path_policy:
        server["path_policy"] = spec.path_policy
    return server


def capability_profiles_from_specs(specs: list[CodingMCPServerSpec]) -> list[dict[str, Any]]:
    profile_names: list[str] = []
    for spec in specs:
        for profile_name in spec.profiles:
            if profile_name not in profile_names:
                profile_names.append(profile_name)
    if "inspect" not in profile_names:
        profile_names.insert(0, "inspect")
    profiles: list[dict[str, Any]] = []
    for profile_name in profile_names:
        server_names = [spec.name for spec in specs if profile_name in spec.profiles]
        profiles.append(
            {
                "name": profile_name,
                "allowed_local_tools": ["current_time", "calculator"],
                "mcp_server_names": server_names,
            }
        )
    return profiles


def default_tenant_config_from_servers(
    tenant_id: str,
    specs: list[CodingMCPServerSpec],
    *,
    workspace_roots: list[Path] | None = None,
    workspace_scope: str | None = None,
) -> dict[str, Any]:
    return {
        tenant_id: {
            "tools": {
                "allowed_local_tools": ["current_time", "calculator"],
                "mcp_servers": [tenant_mcp_server_from_spec(spec) for spec in specs],
            },
            "skills": {
                "default_skill": "coding-workspace",
                "items": [
                    coding_workspace_skill(
                        workspace_roots=workspace_roots, workspace_scope=workspace_scope
                    )
                ],
            },
            "capability_profiles": {
                "default_profile": "inspect",
                "items": capability_profiles_from_specs(specs),
            },
        }
    }


def default_tenant_config(
    tenant_id: str,
    bridge_name: str,
    bridge_url: str,
    *,
    text_enabled: bool = False,
    text_bridge_name: str = DEFAULT_TEXT_BRIDGE_NAME,
    text_bridge_url: str | None = None,
    shell_enabled: bool = False,
    shell_bridge_name: str = DEFAULT_SHELL_BRIDGE_NAME,
    shell_bridge_url: str | None = None,
    workspace_roots: list[Path] | None = None,
) -> dict[str, Any]:
    mcp_servers: list[dict[str, Any]] = [
        {
            "name": bridge_name,
            "url": bridge_url,
            "headers": {},
            "allowed_tools": [
                "list_allowed_directories",
                "list_directory",
                "read_file",
            ],
            "path_policy": {
                "deny_globs": [
                    "**/.env*",
                    "**/.git/**",
                    "**/.venv/**",
                    "**/.pytest_cache/**",
                    "**/.ruff_cache/**",
                    "**/.uv-cache/**",
                ],
                "allow_globs": ["**/.env*.template"],
            },
        }
    ]
    profiles: list[dict[str, Any]] = [
        {
            "name": "inspect",
            "allowed_local_tools": ["current_time", "calculator"],
            "mcp_server_names": [bridge_name],
        }
    ]
    if text_enabled:
        mcp_servers.append(
            {
                "name": text_bridge_name,
                "url": text_bridge_url or f"http://127.0.0.1:{DEFAULT_TEXT_BRIDGE_PORT}/mcp",
                "headers": {},
                "allowed_tools": [
                    "read_text_file_lines",
                    "read_text_file_around",
                    "search_text_file",
                ],
                "path_policy": {
                    "deny_globs": [
                        "**/.env*",
                        "**/.git/**",
                        "**/.venv/**",
                        "**/.pytest_cache/**",
                        "**/.ruff_cache/**",
                        "**/.uv-cache/**",
                    ],
                    "allow_globs": ["**/.env*.template"],
                },
            }
        )
        profiles[0]["mcp_server_names"].append(text_bridge_name)
    if shell_enabled:
        mcp_servers.append(
            {
                "name": shell_bridge_name,
                "url": shell_bridge_url or f"http://127.0.0.1:{DEFAULT_SHELL_BRIDGE_PORT}/mcp",
                "headers": {},
                "allowed_tools": ["run_command"],
            }
        )
        profiles.append(
            {
                "name": "test",
                "allowed_local_tools": ["current_time", "calculator"],
                "mcp_server_names": [bridge_name, shell_bridge_name],
            }
        )
    return {
        tenant_id: {
            "tools": {
                "allowed_local_tools": ["current_time", "calculator"],
                "mcp_servers": mcp_servers,
            },
            "skills": {
                "default_skill": "coding-workspace",
                "items": [coding_workspace_skill(workspace_roots=workspace_roots)],
            },
            "capability_profiles": {
                "default_profile": "inspect",
                "items": profiles,
            },
        }
    }


def inject_coding_mcp_servers(
    raw_config: str, tenant_id: str, specs: list[CodingMCPServerSpec]
) -> str:
    payload = json.loads(raw_config)
    if not isinstance(payload, dict):
        raise RuntimeError("MINDWEFT_TENANT_EXECUTION_CONFIGS must be a JSON object")
    tenant = payload.get(tenant_id)
    if not isinstance(tenant, dict):
        return raw_config
    tools = tenant.setdefault("tools", {})
    if not isinstance(tools, dict):
        return raw_config
    servers = tools.setdefault("mcp_servers", [])
    if not isinstance(servers, list):
        return raw_config
    existing_by_name = {
        server.get("name"): server
        for server in servers
        if isinstance(server, dict) and isinstance(server.get("name"), str)
    }
    for spec in specs:
        generated = tenant_mcp_server_from_spec(spec)
        existing = existing_by_name.get(spec.name)
        if not isinstance(existing, dict):
            servers.append(generated)
            existing_by_name[spec.name] = generated
            continue
        existing_allowed_tools = _string_list(existing.get("allowed_tools"))
        generated_allowed_tools = _string_list(generated.get("allowed_tools"))
        existing.update(generated)
        if existing_allowed_tools is not None and generated_allowed_tools is not None:
            existing["allowed_tools"] = [
                tool for tool in generated_allowed_tools if tool in set(existing_allowed_tools)
            ]
    return json.dumps(payload, separators=(",", ":"))


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def inject_coding_workspace_skill(
    raw_config: str,
    tenant_id: str,
    *,
    workspace_roots: list[Path] | None = None,
    workspace_scope: str | None = None,
) -> str:
    payload = json.loads(raw_config)
    if not isinstance(payload, dict):
        raise RuntimeError("MINDWEFT_TENANT_EXECUTION_CONFIGS must be a JSON object")
    tenant = payload.get(tenant_id)
    if not isinstance(tenant, dict):
        return raw_config
    skills = tenant.setdefault("skills", {})
    if not isinstance(skills, dict):
        return raw_config
    items = skills.setdefault("items", [])
    if not isinstance(items, list):
        return raw_config
    existing_skill = next(
        (
            item
            for item in items
            if isinstance(item, dict) and item.get("name") == "coding-workspace"
        ),
        None,
    )
    if existing_skill is None:
        items.append(
            coding_workspace_skill(workspace_roots=workspace_roots, workspace_scope=workspace_scope)
        )
    elif workspace_roots:
        enrich_coding_workspace_skill(
            existing_skill, workspace_roots, workspace_scope=workspace_scope
        )
    skills.setdefault("default_skill", "coding-workspace")
    return json.dumps(payload, separators=(",", ":"))


def coding_workspace_skill(
    *, workspace_roots: list[Path] | None = None, workspace_scope: str | None = None
) -> dict[str, str]:
    system_prompt = (
        "You are assisting with a code workspace. When the user says current directory, "
        "workspace, repo, or repository root, use its absolute path. Filesystem MCP tools "
        "require explicit absolute paths; always pass the path argument for directory and "
        "file operations. Prefer targeted text-read MCP tools for exact line ranges when "
        "they are available; use broader filesystem reads only when broader file context is "
        "needed. Prefer working with git-tracked source files; use git status "
        "or git ls-files when needed to distinguish tracked, untracked, ignored, and "
        "generated files. Do not read or write secrets such as .env files unless the user "
        "explicitly asks and the active tool policy permits it."
    )
    if workspace_roots:
        system_prompt = append_workspace_roots_to_prompt(
            system_prompt, workspace_roots, workspace_scope=workspace_scope
        )
    skill = {"name": "coding-workspace", "system_prompt": system_prompt}
    if workspace_scope:
        skill["workspace_scope"] = workspace_scope
    return skill


def enrich_coding_workspace_skill(
    skill: dict[str, Any], workspace_roots: list[Path], *, workspace_scope: str | None = None
) -> None:
    system_prompt = skill.get("system_prompt", skill.get("systemPrompt"))
    if not isinstance(system_prompt, str):
        return
    skill["system_prompt"] = append_workspace_roots_to_prompt(
        system_prompt, workspace_roots, workspace_scope=workspace_scope
    )
    if workspace_scope:
        skill["workspace_scope"] = workspace_scope
    skill.pop("systemPrompt", None)


def append_workspace_roots_to_prompt(
    system_prompt: str, workspace_roots: list[Path], *, workspace_scope: str | None = None
) -> str:
    marker = "Configured workspace roots:"
    if marker in system_prompt:
        return system_prompt
    roots = ", ".join(str(workspace) for workspace in workspace_roots)
    root_label = "a workspace root" if len(workspace_roots) == 1 else "workspace roots"
    scope_text = f" Active workspace scope: {workspace_scope}." if workspace_scope else ""
    stay_within = (
        " Stay within these roots for file inspection and edits unless the user explicitly asks "
        "to switch scope."
        if workspace_scope
        else ""
    )
    return (
        f"{system_prompt} {marker} {roots}. Treat each listed path as {root_label}."
        f"{scope_text}{stay_within}"
    )


def tenant_gateway_mcp_server_mismatches(
    env: dict[str, str],
    tenant_id: str,
    *,
    gateway_url_prefix: str,
    specs: list[CodingMCPServerSpec],
) -> list[str]:
    """Return tenant gateway MCP server names with no loaded stdio server spec."""

    normalize_mindweft_env(env)
    raw_config = env.get("MINIGENT_TENANT_EXECUTION_CONFIGS")
    if not raw_config:
        return []
    try:
        payload = json.loads(raw_config)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    tenant = payload.get(tenant_id)
    if not isinstance(tenant, dict):
        return []
    tools = tenant.get("tools")
    if not isinstance(tools, dict):
        return []
    mcp_servers = tools.get("mcp_servers", tools.get("mcpServers"))
    if not isinstance(mcp_servers, list):
        return []

    gateway_prefix = gateway_url_prefix.rstrip("/") + "/"
    gateway_server_names = {spec.name for spec in specs if spec.transport == "stdio"}
    missing: list[str] = []
    for server in mcp_servers:
        if not isinstance(server, dict):
            continue
        name = server.get("name")
        url = server.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        if not url.startswith(gateway_prefix):
            continue
        if name not in gateway_server_names and name not in missing:
            missing.append(name)
    return missing


def bridge_allowed_tools_from_config(
    env: dict[str, str],
    tenant_id: str,
    bridge_name: str,
) -> list[str]:
    normalize_mindweft_env(env)
    raw_config = env.get("MINIGENT_TENANT_EXECUTION_CONFIGS")
    if not raw_config:
        return list(DEFAULT_BRIDGE_ALLOWED_TOOLS)

    payload = json.loads(raw_config)
    if not isinstance(payload, dict):
        raise RuntimeError("MINDWEFT_TENANT_EXECUTION_CONFIGS must be a JSON object")
    tenant = payload.get(tenant_id)
    if not isinstance(tenant, dict):
        return list(DEFAULT_BRIDGE_ALLOWED_TOOLS)
    tools = tenant.get("tools")
    if not isinstance(tools, dict):
        return list(DEFAULT_BRIDGE_ALLOWED_TOOLS)
    mcp_servers = tools.get("mcp_servers", tools.get("mcpServers"))
    if not isinstance(mcp_servers, list):
        return list(DEFAULT_BRIDGE_ALLOWED_TOOLS)

    for server in mcp_servers:
        if not isinstance(server, dict) or server.get("name") != bridge_name:
            continue
        allowed_tools = server.get("allowed_tools", server.get("allowedTools"))
        if allowed_tools is None:
            return []
        if not isinstance(allowed_tools, list) or not all(
            isinstance(item, str) and item for item in allowed_tools
        ):
            raise RuntimeError(f"MCP server '{bridge_name}' has invalid allowed_tools")
        return list(allowed_tools)

    return list(DEFAULT_BRIDGE_ALLOWED_TOOLS)


def bridge_path_globs(
    env: dict[str, str],
    tenant_id: str,
    bridge_name: str,
    *,
    env_name: str,
    policy_key: str,
    policy_camel_key: str,
    defaults: tuple[str, ...],
) -> list[str]:
    normalize_mindweft_env(env)
    raw = env.get(env_name)
    if raw is not None:
        return [pattern.strip() for pattern in raw.split(",") if pattern.strip()]

    raw_config = env.get("MINIGENT_TENANT_EXECUTION_CONFIGS")
    if not raw_config:
        return list(defaults)

    payload = json.loads(raw_config)
    if not isinstance(payload, dict):
        raise RuntimeError("MINDWEFT_TENANT_EXECUTION_CONFIGS must be a JSON object")
    tenant = payload.get(tenant_id)
    if not isinstance(tenant, dict):
        return list(defaults)
    tools = tenant.get("tools")
    if not isinstance(tools, dict):
        return list(defaults)
    mcp_servers = tools.get("mcp_servers", tools.get("mcpServers"))
    if not isinstance(mcp_servers, list):
        return list(defaults)

    for server in mcp_servers:
        if not isinstance(server, dict) or server.get("name") != bridge_name:
            continue
        path_policy = server.get("path_policy", server.get("pathPolicy"))
        if path_policy is None:
            return list(defaults)
        if not isinstance(path_policy, dict):
            raise RuntimeError(f"MCP server '{bridge_name}' has invalid path_policy")
        globs = path_policy.get(policy_key, path_policy.get(policy_camel_key))
        if globs is None:
            return []
        if not isinstance(globs, list) or not all(isinstance(item, str) and item for item in globs):
            raise RuntimeError(f"MCP server '{bridge_name}' has invalid path_policy.{policy_key}")
        return list(globs)

    return list(defaults)
