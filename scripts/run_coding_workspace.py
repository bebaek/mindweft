from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 8765
DEFAULT_BRIDGE_NAME = "fs-workspace"
DEFAULT_SHELL_BRIDGE_NAME = "shell-workspace"
DEFAULT_SHELL_BRIDGE_PORT = 8766
DEFAULT_TENANT_ID = "demo-tenant"
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Minigent as a coding assistant with a workspace-scoped filesystem MCP server. "
            "Loads .env.coding by default."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env.coding",
        help="dotenv file to load before starting processes. Defaults to .env.coding.",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        default=None,
        help=(
            "Workspace root to expose. Repeat for multiple roots. Defaults to "
            "MINIGENT_CODING_WORKSPACE or cwd."
        ),
    )
    parser.add_argument("--tenant-id", default=None, help="Tenant ID for generated default config.")
    parser.add_argument(
        "--api-host", default=None, help="API host. Defaults to MINIGENT_HOST or 127.0.0.1."
    )
    parser.add_argument(
        "--api-port", type=int, default=None, help="API port. Defaults to MINIGENT_PORT or 8000."
    )
    parser.add_argument(
        "--bridge-host",
        default=None,
        help="Filesystem MCP bridge host. Defaults to MINIGENT_CODING_BRIDGE_HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--bridge-port",
        type=int,
        default=None,
        help="Filesystem MCP bridge port. Defaults to MINIGENT_CODING_BRIDGE_PORT or 8765.",
    )
    parser.add_argument(
        "--bridge-name",
        default=None,
        help="MCP server name. Defaults to MINIGENT_CODING_BRIDGE_NAME or fs-workspace.",
    )
    parser.add_argument(
        "--enable-shell",
        action="store_true",
        help="Also start a workspace-scoped shell MCP server and add a non-default test profile when generating config.",
    )
    parser.add_argument(
        "--shell-bridge-port",
        type=int,
        default=None,
        help="Shell MCP bridge port. Defaults to MINIGENT_CODING_SHELL_BRIDGE_PORT or 8766.",
    )
    parser.add_argument(
        "--shell-bridge-name",
        default=None,
        help="Shell MCP server name. Defaults to MINIGENT_CODING_SHELL_BRIDGE_NAME or shell-workspace.",
    )
    parser.add_argument(
        "--skip-api",
        action="store_true",
        help="Only run the filesystem MCP bridge; do not start Minigent API.",
    )
    parser.add_argument(
        "--skip-bridge",
        action="store_true",
        help="Only run Minigent API; assumes the filesystem MCP bridge is already running.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = load_env_file(args.env_file)

    workspace_roots = resolve_workspace_roots(args.workspace, env.get("MINIGENT_CODING_WORKSPACE"))
    for workspace in workspace_roots:
        if not workspace.exists() or not workspace.is_dir():
            print(f"Workspace does not exist or is not a directory: {workspace}", file=sys.stderr)
            return 2

    tenant_id = args.tenant_id or env.get("MINIGENT_CODING_TENANT_ID") or DEFAULT_TENANT_ID
    api_host = args.api_host or env.get("MINIGENT_HOST") or DEFAULT_API_HOST
    api_port = args.api_port or int(env.get("MINIGENT_PORT") or DEFAULT_API_PORT)
    bridge_host = args.bridge_host or env.get("MINIGENT_CODING_BRIDGE_HOST") or DEFAULT_BRIDGE_HOST
    bridge_port = args.bridge_port or int(
        env.get("MINIGENT_CODING_BRIDGE_PORT") or DEFAULT_BRIDGE_PORT
    )
    bridge_name = args.bridge_name or env.get("MINIGENT_CODING_BRIDGE_NAME") or DEFAULT_BRIDGE_NAME
    bridge_url = f"http://{bridge_host}:{bridge_port}/mcp"
    shell_enabled = args.enable_shell or env_flag_enabled(env.get("MINIGENT_CODING_SHELL_ENABLED"))
    shell_bridge_name = (
        args.shell_bridge_name
        or env.get("MINIGENT_CODING_SHELL_BRIDGE_NAME")
        or DEFAULT_SHELL_BRIDGE_NAME
    )
    shell_bridge_port = args.shell_bridge_port or int(
        env.get("MINIGENT_CODING_SHELL_BRIDGE_PORT") or DEFAULT_SHELL_BRIDGE_PORT
    )
    shell_bridge_url = f"http://{bridge_host}:{shell_bridge_port}/mcp"

    env.setdefault("MINIGENT_AUTH_MODE", "dev-headers")
    env.setdefault("MINIGENT_LLM_PROVIDER", "mock")
    if "MINIGENT_TENANT_EXECUTION_CONFIGS" not in env:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = json.dumps(
            default_tenant_config(
                tenant_id,
                bridge_name,
                bridge_url,
                shell_enabled=shell_enabled,
                shell_bridge_name=shell_bridge_name,
                shell_bridge_url=shell_bridge_url,
            ),
            separators=(",", ":"),
        )
    elif env.get("MINIGENT_CODING_INJECT_WORKSPACE_SKILL", "true").lower() not in {
        "0",
        "false",
        "no",
    }:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = inject_coding_workspace_skill(
            env["MINIGENT_TENANT_EXECUTION_CONFIGS"], tenant_id
        )

    processes: list[subprocess.Popen[str]] = []
    print(f"env_file={args.env_file}")
    print("workspaces=" + ", ".join(str(workspace) for workspace in workspace_roots))
    print(f"tenant_id={tenant_id}")
    print(f"bridge={bridge_url}")
    if shell_enabled:
        print(f"shell_bridge={shell_bridge_url}")
    print(f"api=http://{api_host}:{api_port}")

    try:
        if not args.skip_bridge:
            processes.append(
                start_process(
                    build_bridge_command(
                        env, tenant_id, bridge_name, bridge_host, bridge_port, workspace_roots
                    ),
                    env=env,
                    label="filesystem MCP bridge",
                )
            )
            if shell_enabled:
                processes.append(
                    start_process(
                        build_shell_bridge_command(
                            shell_bridge_name,
                            bridge_host,
                            shell_bridge_port,
                            workspace_roots,
                            allowed_command_prefixes=shell_allowed_command_prefixes_from_env(env),
                        ),
                        env=env,
                        label="shell MCP bridge",
                    )
                )
        if not args.skip_api:
            processes.append(
                start_process(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        api_host,
                        "--port",
                        str(api_port),
                    ],
                    env=env,
                    label="Minigent API",
                )
            )
            print_demo_commands(
                api_host,
                api_port,
                tenant_id,
                workspace_roots[0],
                bridge_name,
                shell_bridge_name if shell_enabled else None,
            )

        return wait_for_processes(processes)
    except KeyboardInterrupt:
        print("\nStopping coding workspace processes...")
        return 0
    finally:
        for process in reversed(processes):
            stop_process(process)


def env_flag_enabled(value: str | None) -> bool:
    return value is not None and value.lower() not in {"", "0", "false", "no"}


def resolve_workspace_roots(cli_workspaces: list[str] | None, env_workspace: str | None) -> list[Path]:
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


def load_env_file(env_file: str) -> dict[str, str]:
    env = dict(os.environ)
    path = Path(env_file)
    if path.exists():
        values = dotenv_values(path)
        for key, value in values.items():
            if value is not None:
                env[key] = value
    else:
        print(f"env file not found; continuing with current environment: {env_file}")
    return env


def default_tenant_config(
    tenant_id: str,
    bridge_name: str,
    bridge_url: str,
    *,
    shell_enabled: bool = False,
    shell_bridge_name: str = DEFAULT_SHELL_BRIDGE_NAME,
    shell_bridge_url: str | None = None,
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
            "llm": {"provider": "mock"},
            "tools": {
                "allowed_local_tools": ["current_time", "calculator"],
                "mcp_servers": mcp_servers,
            },
            "skills": {
                "default_skill": "coding-workspace",
                "items": [coding_workspace_skill()],
            },
            "capability_profiles": {
                "default_profile": "inspect",
                "items": profiles,
            },
        }
    }


def inject_coding_workspace_skill(raw_config: str, tenant_id: str) -> str:
    payload = json.loads(raw_config)
    if not isinstance(payload, dict):
        raise RuntimeError("MINIGENT_TENANT_EXECUTION_CONFIGS must be a JSON object")
    tenant = payload.get(tenant_id)
    if not isinstance(tenant, dict):
        return raw_config
    skills = tenant.setdefault("skills", {})
    if not isinstance(skills, dict):
        return raw_config
    items = skills.setdefault("items", [])
    if not isinstance(items, list):
        return raw_config
    if not any(isinstance(item, dict) and item.get("name") == "coding-workspace" for item in items):
        items.append(coding_workspace_skill())
    skills.setdefault("default_skill", "coding-workspace")
    return json.dumps(payload, separators=(",", ":"))


def coding_workspace_skill() -> dict[str, str]:
    return {
        "name": "coding-workspace",
        "system_prompt": (
            "You are assisting with a code workspace. When the user says current directory, "
            "workspace, repo, or repository root, use its absolute path. Filesystem MCP tools "
            "require explicit absolute paths; always pass the path argument for directory and "
            "file operations. Prefer working with git-tracked source files; use git status "
            "or git ls-files when needed to distinguish tracked, untracked, ignored, and "
            "generated files. Do not read or write secrets such as .env files unless the user "
            "explicitly asks and the active tool policy permits it."
        ),
    }


def bridge_allowed_tools_from_config(
    env: dict[str, str],
    tenant_id: str,
    bridge_name: str,
) -> list[str]:
    raw_config = env.get("MINIGENT_TENANT_EXECUTION_CONFIGS")
    if not raw_config:
        return list(DEFAULT_BRIDGE_ALLOWED_TOOLS)

    payload = json.loads(raw_config)
    if not isinstance(payload, dict):
        raise RuntimeError("MINIGENT_TENANT_EXECUTION_CONFIGS must be a JSON object")
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
    raw = env.get(env_name)
    if raw is not None:
        return [pattern.strip() for pattern in raw.split(",") if pattern.strip()]

    raw_config = env.get("MINIGENT_TENANT_EXECUTION_CONFIGS")
    if not raw_config:
        return list(defaults)

    payload = json.loads(raw_config)
    if not isinstance(payload, dict):
        raise RuntimeError("MINIGENT_TENANT_EXECUTION_CONFIGS must be a JSON object")
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
        "from app.mcp_stdio_bridge import main; main()",
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


def build_shell_bridge_command(
    shell_bridge_name: str,
    bridge_host: str,
    shell_bridge_port: int,
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
        "from app.mcp_stdio_bridge import main; main()",
        "--name",
        shell_bridge_name,
        "--host",
        bridge_host,
        "--port",
        str(shell_bridge_port),
        "--allowed-tool",
        "run_command",
        "--",
        sys.executable,
        "-c",
        "from app.shell_mcp_server import main; raise SystemExit(main())",
        *workspace_args,
        *allowed_command_args,
    ]


def start_process(command: list[str], *, env: dict[str, str], label: str) -> subprocess.Popen[str]:
    print(f"starting {label}: {' '.join(shlex.quote(part) for part in command)}")
    return subprocess.Popen(command, env=env, text=True)


def wait_for_processes(processes: list[subprocess.Popen[str]]) -> int:
    while processes:
        time.sleep(0.5)
        for process in list(processes):
            return_code = process.poll()
            if return_code is not None:
                print(f"process exited: pid={process.pid} code={return_code}", file=sys.stderr)
                return return_code or 0
    return 0


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def print_demo_commands(
    api_host: str,
    api_port: int,
    tenant_id: str,
    workspace: Path,
    bridge_name: str,
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


if __name__ == "__main__":
    raise SystemExit(main())
