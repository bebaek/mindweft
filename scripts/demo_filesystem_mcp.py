from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BRIDGE_PORT = 8765
DEFAULT_API_PORT = 8000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Start a local filesystem MCP bridge plus Minigent, then drive a mock "
            "thread through filesystem MCP tool calls."
        )
    )
    parser.add_argument(
        "--workspace",
        default=str(Path(__file__).resolve().parents[1]),
        help="Workspace root to expose to the filesystem MCP server.",
    )
    parser.add_argument("--bridge-port", type=int, default=DEFAULT_BRIDGE_PORT)
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--tenant-id", default="demo-tenant")
    parser.add_argument("--user-id", default="demo-user")
    parser.add_argument(
        "--read-file",
        default="README.md",
        help="Workspace-relative file to read after listing the workspace root.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Leave the bridge and Minigent processes running after the demo.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for Minigent to become reachable.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    read_path = (workspace / args.read_file).resolve()
    if not workspace.exists() or not workspace.is_dir():
        print(f"Workspace does not exist or is not a directory: {workspace}", file=sys.stderr)
        return 2
    if not str(read_path).startswith(str(workspace)):
        print(f"--read-file must stay inside workspace: {args.read_file}", file=sys.stderr)
        return 2

    bridge_url = f"http://127.0.0.1:{args.bridge_port}/mcp"
    base_url = f"http://127.0.0.1:{args.api_port}"
    tenant_config = build_tenant_config(args.tenant_id, bridge_url)
    env = {
        **os.environ,
        "MINIGENT_AUTH_MODE": "dev-headers",
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(tenant_config, separators=(",", ":")),
    }

    processes: list[subprocess.Popen[str]] = []
    try:
        bridge = start_process(
            [
                sys.executable,
                "-c",
                "from app.mcp_stdio_bridge import main; main()",
                "--name",
                "fs-workspace",
                "--port",
                str(args.bridge_port),
                "--allowed-tool",
                "list_allowed_directories",
                "--allowed-tool",
                "list_directory",
                "--allowed-tool",
                "read_file",
                "--deny-glob",
                "**/.env*",
                "--deny-glob",
                "**/.git/**",
                "--deny-glob",
                "**/.venv/**",
                "--deny-glob",
                "**/.pytest_cache/**",
                "--deny-glob",
                "**/.ruff_cache/**",
                "--deny-glob",
                "**/.uv-cache/**",
                "--allow-glob",
                "**/.env*.template",
                "--",
                "npx",
                "-y",
                "@modelcontextprotocol/server-filesystem",
                str(workspace),
            ],
            env=env,
            label="filesystem MCP bridge",
        )
        processes.append(bridge)

        api = start_process(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.api_port),
            ],
            env=env,
            label="Minigent API",
        )
        processes.append(api)

        headers = build_headers(args.user_id, args.tenant_id)
        wait_for_minigent(base_url, args.startup_timeout, processes)
        print(f"workspace={workspace}")
        print(f"bridge={bridge_url}")
        print(f"api={base_url}")

        config = request_json("GET", f"{base_url}/config", headers=headers)
        print("config.local_tools=", config.get("local_tools"))
        print("config.mcp_servers=", config.get("mcp_servers"))

        thread_id = create_thread(base_url, headers)
        print(f"thread_id={thread_id}")
        run_tool(
            base_url,
            headers,
            thread_id,
            "fs-workspace.list_directory",
            {"path": str(workspace)},
        )
        if read_path.exists() and read_path.is_file():
            run_tool(
                base_url,
                headers,
                thread_id,
                "fs-workspace.read_file",
                {"path": str(read_path), "head": 20},
            )
        else:
            print(f"Skipping read_file; file does not exist: {read_path}")

        transcript = request_json("GET", f"{base_url}/threads/{thread_id}/messages", headers=headers)
        print("\ntranscript:")
        for message in transcript:
            tool_name = message.get("tool_name")
            suffix = f" ({tool_name})" if tool_name else ""
            content = str(message.get("content", ""))
            if len(content) > 1200:
                content = content[:1200] + "... [truncated]"
            print(f"- {message['role']}{suffix}: {content}")

        if args.keep_running:
            print("\nProcesses left running. Press Ctrl-C in their terminals or kill these PIDs:")
            for process in processes:
                print(f"- pid={process.pid}")
            processes.clear()
        return 0
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 1
    except urllib.error.URLError as exc:
        print(f"HTTP request failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for process in reversed(processes):
            stop_process(process)


def build_tenant_config(tenant_id: str, bridge_url: str) -> dict[str, Any]:
    return {
        tenant_id: {
            "llm": {"provider": "mock"},
            "tools": {
                "allowed_local_tools": ["current_time", "calculator"],
                "mcp_servers": [
                    {
                        "name": "fs-workspace",
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
                    },
                ],
            },
            "skills": {
                "default_skill": "coding-workspace",
                "items": [
                    {
                        "name": "coding-workspace",
                        "system_prompt": (
                            "You are assisting with a code workspace. When the user says current directory, "
                            "workspace, repo, or repository root, use its absolute path. Filesystem MCP tools "
                            "require explicit absolute paths; always pass the path argument for directory and "
                            "file operations."
                        ),
                    }
                ],
            },
            "capability_profiles": {
                "default_profile": "inspect",
                "items": [
                    {
                        "name": "inspect",
                        "allowed_local_tools": ["current_time", "calculator"],
                        "mcp_server_names": ["fs-workspace"],
                    }
                ],
            },
        }
    }


def start_process(command: list[str], *, env: dict[str, str], label: str) -> subprocess.Popen[str]:
    print(f"starting {label}: {' '.join(command)}")
    return subprocess.Popen(command, env=env, text=True)


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_for_minigent(
    base_url: str,
    timeout_seconds: float,
    processes: list[subprocess.Popen[str]],
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"Process exited early: pid={process.pid} code={process.returncode}")
        try:
            request_json("GET", f"{base_url}/config", headers={})
            return
        except Exception as exc:  # noqa: BLE001 - startup polling should tolerate transient failures.
            last_error = exc
            time.sleep(0.5)
    raise TimeoutError(f"Minigent did not become reachable at {base_url}: {last_error}")


def build_headers(user_id: str, tenant_id: str) -> dict[str, str]:
    return {
        "X-Minigent-User-Id": user_id,
        "X-Minigent-Tenant-Id": tenant_id,
        "X-Minigent-Admin": "false",
    }


def create_thread(base_url: str, headers: dict[str, str]) -> str:
    response = request_json(
        "POST",
        f"{base_url}/threads",
        {"capability_profile": "inspect"},
        headers=headers,
    )
    return str(response["thread_id"])


def run_tool(
    base_url: str,
    headers: dict[str, str],
    thread_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    message = f"/tool {tool_name} {json.dumps(arguments, separators=(',', ':'))}"
    request_json(
        "POST",
        f"{base_url}/threads/{thread_id}/messages",
        {"content": message},
        headers=headers,
    )
    response = request_json("POST", f"{base_url}/threads/{thread_id}/run", headers=headers)
    print(f"user: {message}")
    print(f"assistant: {response['reply']}")


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> Any:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw_body = response.read().decode("utf-8")
        if not raw_body:
            return None
        return json.loads(raw_body)


def print_http_error(exc: urllib.error.HTTPError) -> None:
    body = exc.read().decode("utf-8", errors="replace")
    print(f"HTTP request failed: {exc.code} {body}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
