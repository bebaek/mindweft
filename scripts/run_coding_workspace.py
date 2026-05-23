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
DEFAULT_TENANT_ID = "demo-tenant"


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
        default=None,
        help="Workspace root to expose. Defaults to MINIGENT_CODING_WORKSPACE or cwd.",
    )
    parser.add_argument("--tenant-id", default=None, help="Tenant ID for generated default config.")
    parser.add_argument("--api-host", default=None, help="API host. Defaults to MINIGENT_HOST or 127.0.0.1.")
    parser.add_argument("--api-port", type=int, default=None, help="API port. Defaults to MINIGENT_PORT or 8000.")
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

    workspace = Path(
        args.workspace or env.get("MINIGENT_CODING_WORKSPACE") or Path.cwd()
    ).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        print(f"Workspace does not exist or is not a directory: {workspace}", file=sys.stderr)
        return 2

    tenant_id = args.tenant_id or env.get("MINIGENT_CODING_TENANT_ID") or DEFAULT_TENANT_ID
    api_host = args.api_host or env.get("MINIGENT_HOST") or DEFAULT_API_HOST
    api_port = args.api_port or int(env.get("MINIGENT_PORT") or DEFAULT_API_PORT)
    bridge_host = args.bridge_host or env.get("MINIGENT_CODING_BRIDGE_HOST") or DEFAULT_BRIDGE_HOST
    bridge_port = args.bridge_port or int(env.get("MINIGENT_CODING_BRIDGE_PORT") or DEFAULT_BRIDGE_PORT)
    bridge_name = args.bridge_name or env.get("MINIGENT_CODING_BRIDGE_NAME") or DEFAULT_BRIDGE_NAME
    bridge_url = f"http://{bridge_host}:{bridge_port}/mcp"

    env.setdefault("MINIGENT_AUTH_MODE", "dev-headers")
    env.setdefault("MINIGENT_LLM_PROVIDER", "mock")
    if "MINIGENT_TENANT_EXECUTION_CONFIGS" not in env:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = json.dumps(
            default_tenant_config(tenant_id, bridge_name, bridge_url), separators=(",", ":")
        )
    elif env.get("MINIGENT_CODING_INJECT_WORKSPACE_SKILL", "true").lower() not in {"0", "false", "no"}:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = inject_coding_workspace_skill(
            env["MINIGENT_TENANT_EXECUTION_CONFIGS"], tenant_id
        )

    processes: list[subprocess.Popen[str]] = []
    print(f"env_file={args.env_file}")
    print(f"workspace={workspace}")
    print(f"tenant_id={tenant_id}")
    print(f"bridge={bridge_url}")
    print(f"api=http://{api_host}:{api_port}")

    try:
        if not args.skip_bridge:
            processes.append(
                start_process(
                    build_bridge_command(env, bridge_name, bridge_host, bridge_port, workspace),
                    env=env,
                    label="filesystem MCP bridge",
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
            print_demo_commands(api_host, api_port, tenant_id, workspace, bridge_name)

        return wait_for_processes(processes)
    except KeyboardInterrupt:
        print("\nStopping coding workspace processes...")
        return 0
    finally:
        for process in reversed(processes):
            stop_process(process)


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
) -> dict[str, Any]:
    return {
        tenant_id: {
            "llm": {"provider": "mock"},
            "tools": {
                "allowed_local_tools": ["current_time", "calculator"],
                "mcp_servers": [
                    {
                        "name": bridge_name,
                        "url": bridge_url,
                        "headers": {},
                        "allowed_tools": [
                            "list_allowed_directories",
                            "list_directory",
                            "read_file",
                        ],
                    }
                ],
            },
            "skills": {
                "default_skill": "coding-workspace",
                "items": [coding_workspace_skill()],
            },
            "capability_profiles": {
                "default_profile": "inspect",
                "items": [
                    {
                        "name": "inspect",
                        "allowed_local_tools": ["current_time", "calculator"],
                        "mcp_server_names": [bridge_name],
                    }
                ],
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
            "file operations."
        ),
    }


def build_bridge_command(
    env: dict[str, str],
    bridge_name: str,
    bridge_host: str,
    bridge_port: int,
    workspace: Path,
) -> list[str]:
    filesystem_command = env.get("MINIGENT_CODING_FILESYSTEM_COMMAND")
    command = shlex.split(filesystem_command) if filesystem_command else [
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem",
        str(workspace),
    ]
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
        "--",
        *command,
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
) -> None:
    base_url = f"http://{api_host}:{api_port}"
    print("\nTry it from another shell:")
    tool_message = f"/tool {bridge_name}.list_directory {json.dumps({'path': str(workspace)}, separators=(',', ':'))}"
    print(
        "uv run python scripts/demo_client.py "
        f"--base-url {base_url} --tenant-id {tenant_id} --capability-profile inspect "
        f"{shlex.quote(tool_message)}"
    )
    print("\nPress Ctrl-C to stop.")


if __name__ == "__main__":
    raise SystemExit(main())
