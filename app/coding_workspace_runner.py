from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

from app.attachments import ATTACHMENT_DB_PATH_ENV
from app.unified_config import apply_unified_config_to_env
from minigent_client.state import state_dir_path
from minigent_workspace import cli as _cli
from minigent_workspace import launch_commands as _launch_commands
from minigent_workspace import mcp_specs as _mcp_specs
from minigent_workspace import processes as _processes
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

parse_config_args = _cli.parse_config_args
build_coding_config_export_client_argv = _cli.build_coding_config_export_client_argv
load_config_command_env = _cli.load_config_command_env
run_config_command = _cli.run_config_command
parse_args = _cli.parse_args

build_mcp_gateway_command = _launch_commands.build_mcp_gateway_command
build_builtin_mcp_server_specs = _launch_commands.build_builtin_mcp_server_specs
build_mcp_stdio_bridge_command = _launch_commands.build_mcp_stdio_bridge_command
build_bridge_command = _launch_commands.build_bridge_command
shell_allowed_command_prefixes_from_env = _launch_commands.shell_allowed_command_prefixes_from_env
build_shell_mcp_server_command = _launch_commands.build_shell_mcp_server_command
build_shell_bridge_command = _launch_commands.build_shell_bridge_command
build_text_mcp_server_command = _launch_commands.build_text_mcp_server_command
build_text_bridge_command = _launch_commands.build_text_bridge_command

start_process = _processes.start_process
redacted_command_for_log = _processes.redacted_command_for_log
wait_for_managed_http_server = _processes.wait_for_managed_http_server
wait_for_processes = _processes.wait_for_processes
stop_process = _processes.stop_process


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
