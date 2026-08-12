from __future__ import annotations

import shlex
import sys
from pathlib import Path

from minigent_workspace.mcp_specs import CodingMCPServerSpec
from minigent_workspace.tenant_config import (
    DEFAULT_BRIDGE_ALLOW_GLOBS,
    DEFAULT_BRIDGE_DENY_GLOBS,
    bridge_allowed_tools_from_config,
    bridge_path_globs,
)


def build_mcp_gateway_command(config_path: Path, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from minigent_workspace.bridge.gateway import main; main()",
        "--config",
        str(config_path),
        "--host",
        host,
        "--port",
        str(port),
    ]


def build_builtin_mcp_server_specs(
    env: dict[str, str],
    tenant_id: str,
    *,
    bridge_name: str,
    bridge_host: str,
    bridge_port: int,
    bridge_url: str,
    workspace_roots: list[Path],
    text_enabled: bool,
    text_bridge_name: str,
    text_bridge_port: int,
    text_bridge_url: str,
    shell_enabled: bool,
    shell_bridge_name: str,
    shell_bridge_port: int,
    shell_bridge_url: str,
) -> list[CodingMCPServerSpec]:
    filesystem_command = env.get("MINIGENT_CODING_FILESYSTEM_COMMAND")
    fs_command = (
        shlex.split(filesystem_command)
        if filesystem_command
        else [
            "npx",
            "-y",
            "@modelcontextprotocol/server-filesystem",
            *(str(workspace) for workspace in workspace_roots),
        ]
    )
    specs = [
        CodingMCPServerSpec(
            name=bridge_name,
            url=bridge_url,
            command=fs_command,
            host=bridge_host,
            port=bridge_port,
            profiles=["inspect"],
            allowed_tools=bridge_allowed_tools_from_config(env, tenant_id, bridge_name),
            path_policy={
                "deny_globs": bridge_path_globs(
                    env,
                    tenant_id,
                    bridge_name,
                    env_name="MINIGENT_CODING_BRIDGE_DENY_GLOBS",
                    policy_key="deny_globs",
                    policy_camel_key="denyGlobs",
                    defaults=DEFAULT_BRIDGE_DENY_GLOBS,
                ),
                "allow_globs": bridge_path_globs(
                    env,
                    tenant_id,
                    bridge_name,
                    env_name="MINIGENT_CODING_BRIDGE_ALLOW_GLOBS",
                    policy_key="allow_globs",
                    policy_camel_key="allowGlobs",
                    defaults=DEFAULT_BRIDGE_ALLOW_GLOBS,
                ),
            },
        )
    ]
    if text_enabled:
        specs.append(
            CodingMCPServerSpec(
                name=text_bridge_name,
                url=text_bridge_url,
                command=build_text_mcp_server_command(workspace_roots),
                host=bridge_host,
                port=text_bridge_port,
                profiles=["inspect"],
                allowed_tools=[
                    "read_text_file_lines",
                    "read_text_file_around",
                    "search_text_file",
                ],
                path_policy={
                    "deny_globs": list(DEFAULT_BRIDGE_DENY_GLOBS),
                    "allow_globs": list(DEFAULT_BRIDGE_ALLOW_GLOBS),
                },
            )
        )
    if shell_enabled:
        specs.append(
            CodingMCPServerSpec(
                name=shell_bridge_name,
                url=shell_bridge_url,
                command=build_shell_mcp_server_command(
                    workspace_roots,
                    allowed_command_prefixes=shell_allowed_command_prefixes_from_env(env),
                ),
                host=bridge_host,
                port=shell_bridge_port,
                profiles=["test"],
                allowed_tools=["run_command"],
            )
        )
    return specs


def build_mcp_stdio_bridge_command(spec: CodingMCPServerSpec) -> list[str]:
    if spec.command is None:
        raise RuntimeError(f"MCP server '{spec.name}' requires a command")
    allowed_tool_args: list[str] = []
    if spec.allowed_tools is not None:
        for tool_name in spec.allowed_tools:
            allowed_tool_args.extend(["--allowed-tool", tool_name])
    deny_glob_args: list[str] = []
    for pattern in spec.path_policy.get("deny_globs", spec.path_policy.get("denyGlobs", [])):
        deny_glob_args.extend(["--deny-glob", pattern])
    allow_glob_args: list[str] = []
    for pattern in spec.path_policy.get("allow_globs", spec.path_policy.get("allowGlobs", [])):
        allow_glob_args.extend(["--allow-glob", pattern])
    return [
        sys.executable,
        "-c",
        "from minigent_workspace.bridge.stdio import main; main()",
        "--name",
        spec.name,
        "--host",
        spec.host,
        "--port",
        str(spec.port),
        "--path",
        spec.path,
        "--request-timeout",
        str(spec.request_timeout),
        *allowed_tool_args,
        *deny_glob_args,
        *allow_glob_args,
        "--",
        *spec.command,
    ]


def build_bridge_command(
    env: dict[str, str],
    tenant_id: str,
    bridge_name: str,
    bridge_host: str,
    bridge_port: int,
    workspaces: Path | list[Path],
) -> list[str]:
    workspace_roots = [workspaces] if isinstance(workspaces, Path) else list(workspaces)
    filesystem_command = env.get("MINIGENT_CODING_FILESYSTEM_COMMAND")
    command = (
        shlex.split(filesystem_command)
        if filesystem_command
        else [
            "npx",
            "-y",
            "@modelcontextprotocol/server-filesystem",
            *(str(workspace) for workspace in workspace_roots),
        ]
    )
    allowed_tool_args: list[str] = []
    for tool_name in bridge_allowed_tools_from_config(env, tenant_id, bridge_name):
        allowed_tool_args.extend(["--allowed-tool", tool_name])
    deny_glob_args: list[str] = []
    for pattern in bridge_path_globs(
        env,
        tenant_id,
        bridge_name,
        env_name="MINIGENT_CODING_BRIDGE_DENY_GLOBS",
        policy_key="deny_globs",
        policy_camel_key="denyGlobs",
        defaults=DEFAULT_BRIDGE_DENY_GLOBS,
    ):
        deny_glob_args.extend(["--deny-glob", pattern])
    allow_glob_args: list[str] = []
    for pattern in bridge_path_globs(
        env,
        tenant_id,
        bridge_name,
        env_name="MINIGENT_CODING_BRIDGE_ALLOW_GLOBS",
        policy_key="allow_globs",
        policy_camel_key="allowGlobs",
        defaults=DEFAULT_BRIDGE_ALLOW_GLOBS,
    ):
        allow_glob_args.extend(["--allow-glob", pattern])
    return [
        sys.executable,
        "-c",
        "from minigent_workspace.bridge.stdio import main; main()",
        "--name",
        bridge_name,
        "--host",
        bridge_host,
        "--port",
        str(bridge_port),
        *allowed_tool_args,
        *deny_glob_args,
        *allow_glob_args,
        "--",
        *command,
    ]


def shell_allowed_command_prefixes_from_env(env: dict[str, str]) -> list[str]:
    raw = env.get("MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES", "")
    return [prefix.strip() for prefix in raw.split(",") if prefix.strip()]


def build_shell_mcp_server_command(
    workspaces: Path | list[Path],
    *,
    allowed_command_prefixes: list[str] | None = None,
) -> list[str]:
    workspace_roots = [workspaces] if isinstance(workspaces, Path) else list(workspaces)
    workspace_args: list[str] = []
    for workspace in workspace_roots:
        workspace_args.extend(["--workspace", str(workspace)])
    allowed_command_args: list[str] = []
    for prefix in allowed_command_prefixes or []:
        allowed_command_args.extend(["--allowed-command-prefix", prefix])
    return [
        sys.executable,
        "-c",
        "from minigent_workspace.servers.shell import main; raise SystemExit(main())",
        *workspace_args,
        *allowed_command_args,
    ]


def build_shell_bridge_command(
    shell_bridge_name: str,
    bridge_host: str,
    shell_bridge_port: int,
    workspaces: Path | list[Path],
    *,
    allowed_command_prefixes: list[str] | None = None,
) -> list[str]:
    server_command = build_shell_mcp_server_command(
        workspaces, allowed_command_prefixes=allowed_command_prefixes
    )
    return [
        sys.executable,
        "-c",
        "from minigent_workspace.bridge.stdio import main; main()",
        "--name",
        shell_bridge_name,
        "--host",
        bridge_host,
        "--port",
        str(shell_bridge_port),
        "--allowed-tool",
        "run_command",
        "--",
        *server_command,
    ]


def build_text_mcp_server_command(workspaces: Path | list[Path]) -> list[str]:
    workspace_roots = [workspaces] if isinstance(workspaces, Path) else list(workspaces)
    workspace_args: list[str] = []
    for workspace in workspace_roots:
        workspace_args.extend(["--workspace", str(workspace)])
    return [
        sys.executable,
        "-c",
        "from minigent_workspace.servers.text import main; raise SystemExit(main())",
        *workspace_args,
    ]


def build_text_bridge_command(
    text_bridge_name: str,
    bridge_host: str,
    text_bridge_port: int,
    workspaces: Path | list[Path],
) -> list[str]:
    server_command = build_text_mcp_server_command(workspaces)
    return [
        sys.executable,
        "-c",
        "from minigent_workspace.bridge.stdio import main; main()",
        "--name",
        text_bridge_name,
        "--host",
        bridge_host,
        "--port",
        str(text_bridge_port),
        "--allowed-tool",
        "read_text_file_lines",
        "--allowed-tool",
        "read_text_file_around",
        "--allowed-tool",
        "search_text_file",
        "--",
        *server_command,
    ]
