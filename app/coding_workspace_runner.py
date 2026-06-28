from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

from dotenv import dotenv_values

from app.unified_config import DEFAULT_CODING_DOTENV_FILE, apply_unified_config_to_env

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 8765
DEFAULT_BRIDGE_NAME = "fs-workspace"
DEFAULT_SHELL_BRIDGE_NAME = "shell-workspace"
DEFAULT_SHELL_BRIDGE_PORT = 8766
DEFAULT_TEXT_BRIDGE_NAME = "text-workspace"
DEFAULT_TEXT_BRIDGE_PORT = 8767
DEFAULT_MCP_GATEWAY_PATH_PREFIX = "/mcp"
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


class WorkspaceScope(NamedTuple):
    name: str
    roots: list[Path]
    description: str | None = None


class CodingMCPServerSpec:
    """Declarative MCP server entry for the coding workspace runner.

    Stdio servers are launched behind Minigent's stdio-to-HTTP bridge or gateway. HTTP
    servers are registered in tenant config; when ``managed`` is true, the runner also
    starts their ``command`` as a child process and can wait on ``health_url`` before
    starting the Minigent API.

    For stdio servers, ``host``/``port``/``path`` describe the compatibility mode where
    the runner starts one HTTP bridge per stdio server. They are not used by the shared
    stdio gateway; gateway tenant URLs are derived from the gateway bind address and the
    server name.
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        transport: str = "stdio",
        command: list[str] | None = None,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        path: str = "/mcp",
        profiles: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        path_policy: dict[str, list[str]] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        managed: bool = False,
        health_url: str | None = None,
        startup_timeout_seconds: float = 30.0,
        request_timeout: float = 30.0,
        timeout_seconds: float = 30.0,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.url = url
        self.transport = transport
        self.command = command
        self.host = host
        self.port = port
        self.path = path
        self.profiles = profiles or ["inspect"]
        self.allowed_tools = allowed_tools
        self.path_policy = path_policy or {}
        self.env = env or {}
        self.headers = headers or {}
        self.managed = managed
        self.health_url = health_url
        self.startup_timeout_seconds = startup_timeout_seconds
        self.request_timeout = request_timeout
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled


def parse_config_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coding workspace configuration commands.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    config_parser = subparsers.add_parser("config", help="Configuration helpers.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    export_parser = config_subparsers.add_parser(
        "export",
        help="Export a restartable local coding workspace config by merging API and runner config.",
    )
    export_parser.add_argument(
        "--env-file",
        default=DEFAULT_CODING_DOTENV_FILE,
        help=f"Coding runner dotenv file. Defaults to {DEFAULT_CODING_DOTENV_FILE}.",
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
    argv.extend(["config", "export", "--local-coding", "--coding-env-file", args.env_file])
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
    parser.add_argument(
        "--env-file",
        default=DEFAULT_CODING_DOTENV_FILE,
        help=f"dotenv file to load before starting processes. Defaults to {DEFAULT_CODING_DOTENV_FILE}.",
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
    env = load_env_file(args.env_file)

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
    print(f"env_file={args.env_file}")
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


def env_flag_enabled(value: str | None) -> bool:
    return value is not None and value.lower() not in {"", "0", "false", "no"}


_ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate_config_string(value: str, env: dict[str, str]) -> str:
    """Replace ${NAME} placeholders in declarative MCP config strings."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return env.get(name, "")

    return _ENV_PLACEHOLDER_PATTERN.sub(replace, value)


def normalize_path_prefix(path_prefix: str) -> str:
    if not path_prefix.startswith("/"):
        path_prefix = f"/{path_prefix}"
    return path_prefix.rstrip("/") or DEFAULT_MCP_GATEWAY_PATH_PREFIX


def mcp_server_specs_for_gateway(
    specs: list[CodingMCPServerSpec], gateway_url_prefix: str
) -> list[CodingMCPServerSpec]:
    normalized_prefix = gateway_url_prefix.rstrip("/")
    transformed: list[CodingMCPServerSpec] = []
    for spec in specs:
        if spec.transport == "stdio":
            transformed.append(
                CodingMCPServerSpec(
                    name=spec.name,
                    url=f"{normalized_prefix}/{spec.name}",
                    transport=spec.transport,
                    command=spec.command,
                    host=spec.host,
                    port=spec.port,
                    path=spec.path,
                    profiles=list(spec.profiles),
                    allowed_tools=list(spec.allowed_tools)
                    if spec.allowed_tools is not None
                    else None,
                    path_policy={key: list(value) for key, value in spec.path_policy.items()},
                    env=dict(spec.env),
                    headers=dict(spec.headers),
                    managed=spec.managed,
                    health_url=spec.health_url,
                    startup_timeout_seconds=spec.startup_timeout_seconds,
                    request_timeout=spec.request_timeout,
                    timeout_seconds=spec.timeout_seconds,
                    enabled=spec.enabled,
                )
            )
            continue
        transformed.append(spec)
    return transformed


def mcp_gateway_config_from_specs(specs: list[CodingMCPServerSpec]) -> dict[str, Any]:
    servers: list[dict[str, Any]] = []
    for spec in specs:
        if spec.transport != "stdio":
            continue
        if spec.command is None:
            raise RuntimeError(f"MCP server '{spec.name}' requires a command")
        server: dict[str, Any] = {
            "name": spec.name,
            "command": spec.command,
            "request_timeout": spec.request_timeout,
        }
        if spec.allowed_tools is not None:
            server["allowed_tools"] = spec.allowed_tools
        if spec.path_policy:
            server["path_policy"] = spec.path_policy
        if spec.env:
            server["env"] = spec.env
        servers.append(server)
    return {"servers": servers}


def write_mcp_gateway_config(specs: list[CodingMCPServerSpec]) -> Path:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="minigent-mcp-gateway-",
        suffix=".json",
        delete=False,
    ) as file:
        json.dump(mcp_gateway_config_from_specs(specs), file, separators=(",", ":"))
        file.write("\n")
        return Path(file.name)


def build_mcp_gateway_command(config_path: Path, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-c",
        "from app.mcp_stdio_gateway import main; main()",
        "--config",
        str(config_path),
        "--host",
        host,
        "--port",
        str(port),
    ]


def resolve_mcp_servers_file(
    cli_path: str | None, env: dict[str, str], *, base_dir: Path | None = None
) -> Path | None:
    raw_path = cli_path or env.get("MINIGENT_CODING_MCP_SERVERS_FILE")
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()


def load_coding_mcp_server_specs(
    path: Path,
    *,
    bridge_host: str,
    workspace_roots: list[Path],
    env: dict[str, str] | None = None,
) -> list[CodingMCPServerSpec]:
    return load_coding_mcp_server_specs_from_json(
        path.read_text(encoding="utf-8"),
        bridge_host=bridge_host,
        workspace_roots=workspace_roots,
        env=env,
    )


def load_coding_mcp_server_specs_from_json(
    raw_json: str,
    *,
    bridge_host: str,
    workspace_roots: list[Path],
    env: dict[str, str] | None = None,
) -> list[CodingMCPServerSpec]:
    payload = json.loads(raw_json)
    raw_servers = payload.get("servers") if isinstance(payload, dict) else payload
    if not isinstance(raw_servers, list):
        raise RuntimeError('coding.mcp_server_specs must be a JSON array or {"servers": [...]}')

    specs: list[CodingMCPServerSpec] = []
    interpolation_env = dict(env or os.environ)
    for index, raw_server in enumerate(raw_servers):
        if not isinstance(raw_server, dict):
            raise RuntimeError("each coding MCP server entry must be an object")
        specs.append(
            coding_mcp_server_spec_from_mapping(
                raw_server,
                default_host=bridge_host,
                default_port=DEFAULT_BRIDGE_PORT + index,
                workspace_roots=workspace_roots,
                env=interpolation_env,
            )
        )
    return [spec for spec in specs if spec.enabled]


def coding_mcp_server_spec_from_mapping(
    raw_server: dict[str, Any],
    *,
    default_host: str,
    default_port: int,
    workspace_roots: list[Path],
    env: dict[str, str],
) -> CodingMCPServerSpec:
    name = raw_server.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("coding MCP server entry requires a non-empty name")

    transport = raw_server.get("transport", "stdio")
    if transport not in {"stdio", "http"}:
        raise RuntimeError(f"coding MCP server '{name}' has unsupported transport '{transport}'")

    managed = env_flag_enabled(str(raw_server.get("managed", "false")))
    if transport == "stdio":
        managed = False

    host = raw_server.get("host", default_host)
    if not isinstance(host, str) or not host:
        raise RuntimeError(f"coding MCP server '{name}' has invalid host")
    port = raw_server.get("port")
    if port is None:
        port = default_port
    if not isinstance(port, int):
        raise RuntimeError(f"coding MCP server '{name}' has invalid port")
    path = raw_server.get("path", "/mcp")
    if not isinstance(path, str) or not path.startswith("/"):
        raise RuntimeError(f"coding MCP server '{name}' has invalid path")
    url = raw_server.get("url") or f"http://{host}:{port}{path}"
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"coding MCP server '{name}' has invalid url")
    url = interpolate_config_string(url, env)

    command = raw_server.get("command")
    if command is not None:
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise RuntimeError(f"coding MCP server '{name}' command must be a string array")
        command = expand_coding_mcp_command(
            [interpolate_config_string(item, env) for item in command], workspace_roots
        )
    elif transport == "stdio" or managed:
        raise RuntimeError(
            f"coding MCP server '{name}' requires command for managed or stdio transport"
        )

    allowed_tools = raw_server.get("allowed_tools", raw_server.get("allowedTools"))
    if allowed_tools is not None and (
        not isinstance(allowed_tools, list)
        or not all(isinstance(item, str) for item in allowed_tools)
    ):
        raise RuntimeError(
            f"coding MCP server '{name}' allowed_tools must be a string array or null"
        )

    path_policy = raw_server.get("path_policy", raw_server.get("pathPolicy", {}))
    if not isinstance(path_policy, dict):
        raise RuntimeError(f"coding MCP server '{name}' path_policy must be an object")

    profiles = raw_server.get("profiles", ["inspect"])
    if not isinstance(profiles, list) or not all(
        isinstance(item, str) and item for item in profiles
    ):
        raise RuntimeError(f"coding MCP server '{name}' profiles must be a non-empty string array")

    extra_env = raw_server.get("env", {})
    if not isinstance(extra_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extra_env.items()
    ):
        raise RuntimeError(f"coding MCP server '{name}' env must be an object of string values")
    extra_env = {key: interpolate_config_string(value, env) for key, value in extra_env.items()}

    headers = raw_server.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise RuntimeError(f"coding MCP server '{name}' headers must be an object of string values")
    headers = {key: interpolate_config_string(value, env) for key, value in headers.items()}

    health_url = raw_server.get("health_url", raw_server.get("healthUrl"))
    if health_url is not None:
        if not isinstance(health_url, str) or not health_url:
            raise RuntimeError(f"coding MCP server '{name}' health_url must be a non-empty string")
        health_url = interpolate_config_string(health_url, env)

    startup_timeout_seconds = raw_server.get(
        "startup_timeout_seconds", raw_server.get("startupTimeoutSeconds", 30.0)
    )
    if not isinstance(startup_timeout_seconds, int | float) or startup_timeout_seconds < 0:
        raise RuntimeError(
            f"coding MCP server '{name}' startup_timeout_seconds must be a non-negative number"
        )
    request_timeout = raw_server.get("request_timeout", raw_server.get("requestTimeout", 30.0))
    if not isinstance(request_timeout, int | float) or request_timeout <= 0:
        raise RuntimeError(f"coding MCP server '{name}' request_timeout must be a positive number")
    timeout_seconds = raw_server.get(
        "timeout_seconds", raw_server.get("timeoutSeconds", request_timeout)
    )
    if not isinstance(timeout_seconds, int | float) or timeout_seconds <= 0:
        raise RuntimeError(f"coding MCP server '{name}' timeout_seconds must be a positive number")

    return CodingMCPServerSpec(
        name=name,
        url=url,
        transport=transport,
        command=command,
        host=host,
        port=port,
        path=path,
        profiles=list(profiles),
        allowed_tools=list(allowed_tools) if allowed_tools is not None else None,
        path_policy={
            key: list(value) for key, value in path_policy.items() if isinstance(value, list)
        },
        env=dict(extra_env),
        headers=dict(headers),
        managed=managed,
        health_url=health_url,
        startup_timeout_seconds=float(startup_timeout_seconds),
        request_timeout=float(request_timeout),
        timeout_seconds=float(timeout_seconds),
        enabled=env_flag_enabled(str(raw_server.get("enabled", "true"))),
    )


def expand_coding_mcp_command(command: list[str], workspace_roots: list[Path]) -> list[str]:
    expanded: list[str] = []
    first_workspace = str(workspace_roots[0])
    workspace_roots_csv = ",".join(str(workspace) for workspace in workspace_roots)
    index = 0
    while index < len(command):
        item = command[index]
        if item == "{workspace_roots}":
            expanded.extend(str(workspace) for workspace in workspace_roots)
            index += 1
            continue
        if item == "{workspace_args}":
            for workspace in workspace_roots:
                expanded.extend(["--workspace", str(workspace)])
            index += 1
            continue
        if (
            item == "--workspace"
            and index + 1 < len(command)
            and command[index + 1] == "{workspace}"
        ):
            for workspace in workspace_roots:
                expanded.extend(["--workspace", str(workspace)])
            index += 2
            continue
        expanded.append(
            item.replace("{workspace}", first_workspace).replace(
                "{workspace_roots_csv}", workspace_roots_csv
            )
        )
        index += 1
    return expanded


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


def load_env_file(env_file: str) -> dict[str, str]:
    env = dict(os.environ)
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
        raise RuntimeError("MINIGENT_TENANT_EXECUTION_CONFIGS must be a JSON object")
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
        "from app.mcp_stdio_bridge import main; main()",
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
        "from app.shell_mcp_server import main; raise SystemExit(main())",
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
        "from app.text_mcp_server import main; raise SystemExit(main())",
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
        "from app.mcp_stdio_bridge import main; main()",
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
