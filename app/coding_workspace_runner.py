from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import dotenv_values

from app.attachments import ATTACHMENT_DB_PATH_ENV
from app.unified_config import DEFAULT_CODING_DOTENV_FILE, apply_unified_config_to_env
from minigent_client.state import state_dir_path
from minigent_workspace import mcp_specs as _mcp_specs
from minigent_workspace import scopes as _workspace_scopes
from minigent_workspace import tenant_config as _tenant_config

WorkspaceScope = _workspace_scopes.WorkspaceScope
resolve_workspace_roots = _workspace_scopes.resolve_workspace_roots
load_workspace_scopes_from_env = _workspace_scopes.load_workspace_scopes_from_env
skill_workspace_scope_from_env = _workspace_scopes.skill_workspace_scope_from_env
resolve_active_workspace_scope = _workspace_scopes.resolve_active_workspace_scope

CodingMCPServerSpec = _mcp_specs.CodingMCPServerSpec
env_flag_enabled = _mcp_specs.env_flag_enabled
interpolate_config_string = _mcp_specs.interpolate_config_string
normalize_path_prefix = _mcp_specs.normalize_path_prefix
mcp_server_specs_for_gateway = _mcp_specs.mcp_server_specs_for_gateway
mcp_gateway_config_from_specs = _mcp_specs.mcp_gateway_config_from_specs
write_mcp_gateway_config = _mcp_specs.write_mcp_gateway_config
resolve_mcp_servers_file = _mcp_specs.resolve_mcp_servers_file
load_coding_mcp_server_specs = _mcp_specs.load_coding_mcp_server_specs
load_coding_mcp_server_specs_from_json = _mcp_specs.load_coding_mcp_server_specs_from_json
coding_mcp_server_spec_from_mapping = _mcp_specs.coding_mcp_server_spec_from_mapping
expand_coding_mcp_command = _mcp_specs.expand_coding_mcp_command

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_BRIDGE_HOST = _mcp_specs.DEFAULT_BRIDGE_HOST
DEFAULT_BRIDGE_PORT = _mcp_specs.DEFAULT_BRIDGE_PORT
DEFAULT_BRIDGE_NAME = "fs-workspace"
DEFAULT_SHELL_BRIDGE_NAME = _tenant_config.DEFAULT_SHELL_BRIDGE_NAME
DEFAULT_SHELL_BRIDGE_PORT = _tenant_config.DEFAULT_SHELL_BRIDGE_PORT
DEFAULT_TEXT_BRIDGE_NAME = _tenant_config.DEFAULT_TEXT_BRIDGE_NAME
DEFAULT_TEXT_BRIDGE_PORT = _tenant_config.DEFAULT_TEXT_BRIDGE_PORT
DEFAULT_BRIDGE_ALLOWED_TOOLS = _tenant_config.DEFAULT_BRIDGE_ALLOWED_TOOLS
DEFAULT_BRIDGE_DENY_GLOBS = _tenant_config.DEFAULT_BRIDGE_DENY_GLOBS
DEFAULT_BRIDGE_ALLOW_GLOBS = _tenant_config.DEFAULT_BRIDGE_ALLOW_GLOBS
DEFAULT_MCP_GATEWAY_PATH_PREFIX = _mcp_specs.DEFAULT_MCP_GATEWAY_PATH_PREFIX
DEFAULT_TENANT_ID = "demo-tenant"
DEFAULT_ATTACHMENT_DB_FILE = "attachments.db"


def parse_config_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coding workspace configuration commands.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    config_parser = subparsers.add_parser("config", help="Configuration helpers.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    export_parser = config_subparsers.add_parser(
        "export",
        help="Export a restartable local coding workspace config by merging API and runner config.",
    )
    env_file_group = export_parser.add_mutually_exclusive_group()
    env_file_group.add_argument(
        "--env-file",
        default=DEFAULT_CODING_DOTENV_FILE,
        help=f"Coding runner dotenv file. Defaults to {DEFAULT_CODING_DOTENV_FILE}.",
    )
    env_file_group.add_argument(
        "--no-env-file",
        action="store_true",
        help="Do not load a coding runner dotenv file for this command.",
    )
    export_parser.add_argument(
        "--base-url",
        default=None,
        help="Running Minigent API URL. Defaults to MINIGENT_BASE_URL or http://127.0.0.1:8000.",
    )
    export_parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write instead of stdout.",
    )
    export_parser.add_argument(
        "--include-runtime",
        action="store_true",
        help="Include informational runtime status/tool snapshots in the export.",
    )
    export_parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON instead of TOML.",
    )
    return parser.parse_args(argv)


def build_coding_config_export_client_argv(args: argparse.Namespace) -> list[str]:
    argv: list[str] = []
    base_url = args.base_url or os.getenv("MINIGENT_BASE_URL")
    if base_url:
        argv.extend(["--base-url", base_url])
    if args.json:
        argv.append("--json")
    argv.extend(["config", "export", "--local-coding"])
    if args.no_env_file:
        argv.append("--no-coding-env-file")
    else:
        argv.extend(["--coding-env-file", args.env_file])
    if args.output:
        argv.extend(["--output", args.output])
    if args.include_runtime:
        argv.append("--include-runtime")
    return argv


def load_config_command_env(env_file: str) -> None:
    from dotenv import dotenv_values

    path = Path(env_file).expanduser()
    os.environ["MINIGENT_DOTENV_FILE"] = str(path)
    if not path.exists():
        return
    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)


def run_config_command(argv: list[str]) -> int:
    args = parse_config_args(argv)
    if args.command == "config" and args.config_command == "export":
        if not args.no_env_file:
            load_config_command_env(args.env_file)
        from minigent_client.one_shot_cli import main as client_main

        return client_main(build_coding_config_export_client_argv(args))
    raise RuntimeError("unsupported coding workspace config command")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Minigent as a coding assistant with a workspace-scoped filesystem MCP server. "
            f"Loads {DEFAULT_CODING_DOTENV_FILE} by default."
        )
    )
    parser.add_argument(
        "--mcp-servers-file",
        default=None,
        help=(
            "Legacy JSON file declaring MCP servers to register/start. Prefer "
            "[[coding.mcp_server_specs]] in minigent.toml for new configs. Defaults to "
            "MINIGENT_CODING_MCP_SERVERS_FILE when set."
        ),
    )
    env_file_group = parser.add_mutually_exclusive_group()
    env_file_group.add_argument(
        "--env-file",
        default=DEFAULT_CODING_DOTENV_FILE,
        help=f"dotenv file to load before starting processes. Defaults to {DEFAULT_CODING_DOTENV_FILE}.",
    )
    env_file_group.add_argument(
        "--no-env-file",
        action="store_true",
        help="Do not load a coding runner dotenv file before starting processes.",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        default=None,
        help=(
            "Workspace root to expose. Repeat for multiple roots. Defaults to "
            "MINIGENT_CODING_WORKSPACES, MINIGENT_CODING_WORKSPACE, or cwd."
        ),
    )
    parser.add_argument(
        "--workspace-scope",
        default=None,
        help=(
            "Named coding workspace scope to activate. Defaults to "
            "MINIGENT_CODING_WORKSPACE_SCOPE, a skill workspace_scope, "
            "MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE, or all configured workspaces."
        ),
    )
    parser.add_argument(
        "--mcp-gateway",
        action="store_true",
        help=(
            "Start stdio MCP servers behind one local gateway instead of one bridge process "
            "per server. Also rewrites generated tenant MCP URLs to /mcp/<server-name>."
        ),
    )
    parser.add_argument(
        "--mcp-gateway-port",
        type=int,
        default=None,
        help=(
            "Gateway port when --mcp-gateway is used. Defaults to "
            "MINIGENT_CODING_MCP_GATEWAY_PORT or the filesystem bridge port."
        ),
    )
    parser.add_argument(
        "--mcp-gateway-path-prefix",
        default=None,
        help=(
            "Gateway path prefix when --mcp-gateway is used. Defaults to "
            "MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX or /mcp."
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
        "--enable-text",
        action="store_true",
        help="Also start a workspace-scoped targeted text-read MCP server and add it to the inspect profile when generating config.",
    )
    parser.add_argument(
        "--text-bridge-port",
        type=int,
        default=None,
        help="Targeted text-read MCP bridge port. Defaults to MINIGENT_CODING_TEXT_BRIDGE_PORT or 8767.",
    )
    parser.add_argument(
        "--text-bridge-name",
        default=None,
        help="Targeted text-read MCP server name. Defaults to MINIGENT_CODING_TEXT_BRIDGE_NAME or text-workspace.",
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
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if raw_argv[:1] == ["config"]:
        return run_config_command(raw_argv)
    args = parse_args(raw_argv)
    env_file_explicit = any(
        item == "--env-file" or item.startswith("--env-file=") for item in raw_argv
    )
    env = load_env_file(
        None if args.no_env_file else args.env_file,
        warn_if_missing=env_file_explicit,
    )
    apply_coding_workspace_state_defaults(env)

    workspace_roots = resolve_workspace_roots(
        args.workspace,
        env.get("MINIGENT_CODING_WORKSPACES") or env.get("MINIGENT_CODING_WORKSPACE"),
    )

    tenant_id = args.tenant_id or env.get("MINIGENT_CODING_TENANT_ID") or DEFAULT_TENANT_ID
    try:
        workspace_roots, active_workspace_scope = resolve_active_workspace_scope(
            workspace_roots,
            env,
            tenant_id=tenant_id,
            explicit_scope=args.workspace_scope,
            validate_under_configured_roots=bool(
                args.workspace
                or env.get("MINIGENT_CODING_WORKSPACES")
                or env.get("MINIGENT_CODING_WORKSPACE")
            ),
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for workspace in workspace_roots:
        if not workspace.exists() or not workspace.is_dir():
            print(f"Workspace does not exist or is not a directory: {workspace}", file=sys.stderr)
            return 2

    api_host = args.api_host or env.get("MINIGENT_HOST") or DEFAULT_API_HOST
    api_port = args.api_port or int(env.get("MINIGENT_PORT") or DEFAULT_API_PORT)
    bridge_host = args.bridge_host or env.get("MINIGENT_CODING_BRIDGE_HOST") or DEFAULT_BRIDGE_HOST
    bridge_port = args.bridge_port or int(
        env.get("MINIGENT_CODING_BRIDGE_PORT") or DEFAULT_BRIDGE_PORT
    )
    bridge_name = args.bridge_name or env.get("MINIGENT_CODING_BRIDGE_NAME") or DEFAULT_BRIDGE_NAME
    bridge_url = f"http://{bridge_host}:{bridge_port}/mcp"
    gateway_enabled = args.mcp_gateway or env_flag_enabled(
        env.get("MINIGENT_CODING_MCP_GATEWAY_ENABLED")
    )
    gateway_port = args.mcp_gateway_port or int(
        env.get("MINIGENT_CODING_MCP_GATEWAY_PORT") or bridge_port
    )
    gateway_path_prefix = (
        args.mcp_gateway_path_prefix
        or env.get("MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX")
        or DEFAULT_MCP_GATEWAY_PATH_PREFIX
    )
    gateway_url_prefix = (
        f"http://{bridge_host}:{gateway_port}{normalize_path_prefix(gateway_path_prefix)}"
    )
    text_enabled = args.enable_text or env_flag_enabled(env.get("MINIGENT_CODING_TEXT_ENABLED"))
    text_bridge_name = (
        args.text_bridge_name
        or env.get("MINIGENT_CODING_TEXT_BRIDGE_NAME")
        or DEFAULT_TEXT_BRIDGE_NAME
    )
    text_bridge_port = args.text_bridge_port or int(
        env.get("MINIGENT_CODING_TEXT_BRIDGE_PORT") or DEFAULT_TEXT_BRIDGE_PORT
    )
    text_bridge_url = f"http://{bridge_host}:{text_bridge_port}/mcp"
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
    mcp_servers_file = resolve_mcp_servers_file(
        args.mcp_servers_file, env, base_dir=Path(args.env_file).expanduser().resolve().parent
    )
    if env.get("MINIGENT_CODING_MCP_SERVER_SPECS"):
        mcp_server_specs = load_coding_mcp_server_specs_from_json(
            env["MINIGENT_CODING_MCP_SERVER_SPECS"],
            bridge_host=bridge_host,
            workspace_roots=workspace_roots,
            env=env,
        )
    elif mcp_servers_file is not None:
        mcp_server_specs = load_coding_mcp_server_specs(
            mcp_servers_file,
            bridge_host=bridge_host,
            workspace_roots=workspace_roots,
            env=env,
        )
    else:
        mcp_server_specs = build_builtin_mcp_server_specs(
            env,
            tenant_id,
            bridge_name=bridge_name,
            bridge_host=bridge_host,
            bridge_port=bridge_port,
            bridge_url=bridge_url,
            workspace_roots=workspace_roots,
            text_enabled=text_enabled,
            text_bridge_name=text_bridge_name,
            text_bridge_port=text_bridge_port,
            text_bridge_url=text_bridge_url,
            shell_enabled=shell_enabled,
            shell_bridge_name=shell_bridge_name,
            shell_bridge_port=shell_bridge_port,
            shell_bridge_url=shell_bridge_url,
        )
    tenant_mcp_server_specs = (
        mcp_server_specs_for_gateway(mcp_server_specs, gateway_url_prefix)
        if gateway_enabled
        else mcp_server_specs
    )

    env.setdefault("MINIGENT_AUTH_MODE", "dev-headers")
    env.setdefault("MINIGENT_LLM_PROVIDER", "mock")
    env["MINIGENT_CODING_TENANT_ID"] = tenant_id
    env.setdefault("MINIGENT_CODING_OAUTH_GLOBAL_FALLBACK", "true")
    if "MINIGENT_TENANT_EXECUTION_CONFIGS" not in env:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = json.dumps(
            default_tenant_config_from_servers(
                tenant_id,
                tenant_mcp_server_specs,
                workspace_roots=workspace_roots,
                workspace_scope=active_workspace_scope.name if active_workspace_scope else None,
            ),
            separators=(",", ":"),
        )
    else:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = inject_coding_mcp_servers(
            env["MINIGENT_TENANT_EXECUTION_CONFIGS"], tenant_id, tenant_mcp_server_specs
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
            workspace_scope=active_workspace_scope.name if active_workspace_scope else None,
        )
    if gateway_enabled:
        for missing_name in tenant_gateway_mcp_server_mismatches(
            env,
            tenant_id,
            gateway_url_prefix=gateway_url_prefix,
            specs=mcp_server_specs,
        ):
            print(
                "WARNING: tenant MCP server "
                f"'{missing_name}' points at the coding MCP gateway but no matching "
                "coding.mcp_server_specs entry was loaded; calls may return 404.",
                file=sys.stderr,
            )

    processes: list[subprocess.Popen[str]] = []
    managed_http_processes: list[tuple[CodingMCPServerSpec, subprocess.Popen[str]]] = []
    generated_files: list[Path] = []
    env_path = Path(args.env_file)
    if not args.no_env_file:
        if env_path.exists():
            print(f"loaded_env_file={args.env_file}")
        elif not env_file_explicit:
            print(f"optional_env_file_not_found={args.env_file}")
    print("workspaces=" + ", ".join(str(workspace) for workspace in workspace_roots))
    if active_workspace_scope is not None:
        print(f"workspace_scope={active_workspace_scope.name}")
    print(f"tenant_id={tenant_id}")
    if mcp_servers_file is not None:
        print(f"mcp_servers_file={mcp_servers_file} (legacy input; export emits inline specs)")
    for spec, tenant_spec in zip(mcp_server_specs, tenant_mcp_server_specs, strict=True):
        if spec.url == tenant_spec.url:
            print(
                f"mcp_server={spec.name} url={spec.url} transport={spec.transport} "
                f"managed={str(spec.managed).lower()}"
            )
        else:
            print(
                f"mcp_server={spec.name} url={tenant_spec.url} "
                f"transport={spec.transport} managed={str(spec.managed).lower()} "
                f"bridge_url={spec.url}"
            )
    if gateway_enabled:
        print(f"mcp_gateway={gateway_url_prefix}")
    print(f"api=http://{api_host}:{api_port}")

    try:
        if not args.skip_bridge:
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
                text_bridge_name if text_enabled else None,
                shell_bridge_name if shell_enabled else None,
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


def apply_coding_workspace_state_defaults(env: dict[str, str]) -> None:
    """Use durable user-local attachment storage unless the deployment overrides it."""
    env.setdefault(
        ATTACHMENT_DB_PATH_ENV,
        str(state_dir_path(env) / DEFAULT_ATTACHMENT_DB_FILE),
    )


def load_env_file(env_file: str | None, *, warn_if_missing: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    if env_file is None:
        source_env = dict(env)
        apply_unified_config_to_env(source_env, base_dir=Path.cwd())
        for key, value in source_env.items():
            env.setdefault(key, value)
        apply_file_env_values(env, base_dir=Path.cwd())
        return env

    path = Path(env_file)
    base_dir = path.parent if path.exists() else Path.cwd()
    values = dotenv_values(path) if path.exists() else {}
    source_env = dict(env)
    source_env.update({key: value for key, value in values.items() if value is not None})
    apply_unified_config_to_env(source_env, base_dir=base_dir)
    for key, value in source_env.items():
        env.setdefault(key, value)
    if path.exists():
        for key, value in values.items():
            if value is not None:
                env[key] = value
        apply_file_env_values(env, base_dir=path.parent)
    else:
        if warn_if_missing:
            print(f"env file not found; continuing with current environment: {env_file}")
        apply_file_env_values(env, base_dir=Path.cwd())
    return env


def apply_file_env_values(env: dict[str, str], *, base_dir: Path) -> None:
    """Expand FOO_FILE=/path/to/value-file entries into FOO=<file contents>.

    Relative file paths are resolved from the dotenv file directory. This is useful for
    long JSON-valued settings that are hard to edit safely on one dotenv line.
    """
    for file_key, raw_path in list(env.items()):
        if not file_key.endswith("_FILE") or not raw_path.strip():
            continue
        if file_key in {
            "MINIGENT_CONFIG_FILE",
            "MINIGENT_DOTENV_FILE",
            "MINIGENT_CODING_MCP_SERVERS_FILE",
        }:
            continue
        target_key = file_key[: -len("_FILE")]
        value_path = Path(raw_path).expanduser()
        if not value_path.is_absolute():
            value_path = base_dir / value_path
        env[target_key] = value_path.read_text(encoding="utf-8").strip()


tenant_mcp_server_from_spec = _tenant_config.tenant_mcp_server_from_spec
capability_profiles_from_specs = _tenant_config.capability_profiles_from_specs
default_tenant_config_from_servers = _tenant_config.default_tenant_config_from_servers
default_tenant_config = _tenant_config.default_tenant_config
inject_coding_mcp_servers = _tenant_config.inject_coding_mcp_servers
inject_coding_workspace_skill = _tenant_config.inject_coding_workspace_skill
coding_workspace_skill = _tenant_config.coding_workspace_skill
enrich_coding_workspace_skill = _tenant_config.enrich_coding_workspace_skill
append_workspace_roots_to_prompt = _tenant_config.append_workspace_roots_to_prompt
tenant_gateway_mcp_server_mismatches = _tenant_config.tenant_gateway_mcp_server_mismatches
bridge_allowed_tools_from_config = _tenant_config.bridge_allowed_tools_from_config
bridge_path_globs = _tenant_config.bridge_path_globs


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


def start_process(command: list[str], *, env: dict[str, str], label: str) -> subprocess.Popen[str]:
    print(f"starting {label}: {redacted_command_for_log(command)}")
    return subprocess.Popen(command, env=env, text=True, start_new_session=True)


_SENSITIVE_ARG_MARKERS = ("key", "token", "secret", "password", "authorization", "credential")


def redacted_command_for_log(command: list[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for part in command:
        lower_part = part.lower()
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if lower_part.startswith("--") and any(
            marker in lower_part for marker in _SENSITIVE_ARG_MARKERS
        ):
            if "=" in part:
                option, _value = part.split("=", 1)
                redacted.append(f"{option}=<redacted>")
            else:
                redacted.append(part)
                redact_next = True
            continue
        if "=" in part:
            key, _value = part.split("=", 1)
            if any(marker in key.lower() for marker in _SENSITIVE_ARG_MARKERS):
                redacted.append(f"{key}=<redacted>")
                continue
        redacted.append(part)
    return " ".join(shlex.quote(part) for part in redacted)


def wait_for_managed_http_server(spec: CodingMCPServerSpec, process: subprocess.Popen[str]) -> None:
    if not spec.health_url:
        return
    deadline = time.monotonic() + spec.startup_timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() <= deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"managed HTTP MCP server '{spec.name}' exited before health check succeeded: "
                f"code={return_code}"
            )
        try:
            with urllib.request.urlopen(spec.health_url, timeout=1.0) as response:
                if 200 <= response.status < 400:
                    print(f"healthy {spec.name} MCP HTTP server: {spec.health_url}")
                    return
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
        time.sleep(0.2)
    detail = f": {last_error}" if last_error is not None else ""
    raise RuntimeError(
        f"managed HTTP MCP server '{spec.name}' did not become healthy within "
        f"{spec.startup_timeout_seconds:g}s at {spec.health_url}{detail}"
    )


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
    text_bridge_name: str | None = None,
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
    if text_bridge_name is not None:
        text_message = f"/tool {text_bridge_name}.read_text_file_around " + json.dumps(
            {"path": str(workspace / "README.md"), "line": 1, "after": 20}, separators=(",", ":")
        )
        print(
            "uv run python scripts/demo_client.py "
            f"--base-url {base_url} --tenant-id {tenant_id} --capability-profile inspect "
            f"{shlex.quote(text_message)}"
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
