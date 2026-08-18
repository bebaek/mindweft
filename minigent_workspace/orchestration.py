from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from minigent_workspace.launch_commands import (
    build_mcp_gateway_command,
    build_mcp_stdio_bridge_command,
)
from minigent_workspace.mcp_specs import CodingMCPServerSpec, write_mcp_gateway_config
from minigent_workspace.output import print_demo_commands
from minigent_workspace.processes import (
    start_process,
    stop_process,
    wait_for_managed_http_server,
    wait_for_processes,
)


def run_workspace_processes(
    *,
    env: dict[str, str],
    mcp_server_specs: list[CodingMCPServerSpec],
    skip_bridge: bool,
    gateway_enabled: bool,
    bridge_host: str,
    gateway_port: int,
    skip_api: bool,
    api_host: str,
    api_port: int,
    tenant_id: str,
    workspace: Path,
    bridge_name: str,
    text_bridge_name: str | None,
    shell_bridge_name: str | None,
) -> int:
    processes: list[subprocess.Popen[str]] = []
    managed_http_processes: list[tuple[CodingMCPServerSpec, subprocess.Popen[str]]] = []
    generated_files: list[Path] = []
    try:
        if not skip_bridge:
            if gateway_enabled:
                gateway_config_path = write_mcp_gateway_config(mcp_server_specs)
                generated_files.append(gateway_config_path)
                processes.append(
                    start_process(
                        build_mcp_gateway_command(
                            gateway_config_path,
                            bridge_host,
                            gateway_port,
                        ),
                        env=env,
                        label="MCP stdio gateway",
                    )
                )
            else:
                for spec in mcp_server_specs:
                    if spec.transport != "stdio":
                        continue
                    process_env = {**env, **spec.env}
                    processes.append(
                        start_process(
                            build_mcp_stdio_bridge_command(spec),
                            env=process_env,
                            label=f"{spec.name} MCP bridge",
                        )
                    )
            for spec in mcp_server_specs:
                if spec.transport != "http" or not spec.managed:
                    continue
                if spec.command is None:
                    raise RuntimeError(f"managed HTTP MCP server '{spec.name}' requires a command")
                process_env = {**env, **spec.env}
                process = start_process(
                    spec.command,
                    env=process_env,
                    label=f"{spec.name} MCP HTTP server",
                )
                processes.append(process)
                managed_http_processes.append((spec, process))
            for spec, process in managed_http_processes:
                wait_for_managed_http_server(spec, process)
        if not skip_api:
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
                    label="Mindweft API",
                )
            )
            print_demo_commands(
                api_host,
                api_port,
                tenant_id,
                workspace,
                bridge_name,
                text_bridge_name,
                shell_bridge_name,
            )

        return wait_for_processes(processes)
    except KeyboardInterrupt:
        print("\nStopping coding workspace processes...")
        return 0
    finally:
        for process in reversed(processes):
            stop_process(process)
        for path in generated_files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
