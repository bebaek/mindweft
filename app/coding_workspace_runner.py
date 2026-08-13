from __future__ import annotations

import json
import sys
from pathlib import Path

from minigent_workspace import cli as _cli
from minigent_workspace import environment as _environment
from minigent_workspace import launch_commands as _launch_commands
from minigent_workspace import mcp_specs as _mcp_specs
from minigent_workspace import orchestration as _orchestration
from minigent_workspace import output as _output
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
parse_config_args = _cli.parse_config_args
build_coding_config_export_client_argv = _cli.build_coding_config_export_client_argv
load_config_command_env = _cli.load_config_command_env
run_config_command = _cli.run_config_command
parse_args = _cli.parse_args

apply_coding_workspace_state_defaults = _environment.apply_coding_workspace_state_defaults
load_env_file = _environment.load_env_file
apply_file_env_values = _environment.apply_file_env_values

print_workspace_summary = _output.print_workspace_summary
print_demo_commands = _output.print_demo_commands

run_workspace_processes = _orchestration.run_workspace_processes

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

    return run_workspace_processes(
        env=env,
        mcp_server_specs=mcp_server_specs,
        skip_bridge=args.skip_bridge,
        gateway_enabled=gateway_enabled,
        bridge_host=bridge_host,
        gateway_port=gateway_port,
        skip_api=args.skip_api,
        api_host=api_host,
        api_port=api_port,
        tenant_id=tenant_id,
        workspace=workspace_roots[0],
        bridge_name=bridge_name,
        text_bridge_name=text_bridge_name if text_enabled else None,
        shell_bridge_name=shell_bridge_name if shell_enabled else None,
    )


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


if __name__ == "__main__":
    raise SystemExit(main())
