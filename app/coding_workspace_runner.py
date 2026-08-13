from __future__ import annotations

import sys

from minigent_workspace import cli as _cli
from minigent_workspace import environment as _environment
from minigent_workspace import launch_commands as _launch_commands
from minigent_workspace import mcp_resolution as _mcp_resolution
from minigent_workspace import mcp_specs as _mcp_specs
from minigent_workspace import orchestration as _orchestration
from minigent_workspace import output as _output
from minigent_workspace import processes as _processes
from minigent_workspace import runtime_settings as _runtime_settings
from minigent_workspace import scopes as _workspace_scopes
from minigent_workspace import tenant_config as _tenant_config

WorkspaceScope = _workspace_scopes.WorkspaceScope
resolve_workspace_roots = _workspace_scopes.resolve_workspace_roots
load_workspace_scopes_from_env = _workspace_scopes.load_workspace_scopes_from_env
skill_workspace_scope_from_env = _workspace_scopes.skill_workspace_scope_from_env
resolve_active_workspace_scope = _workspace_scopes.resolve_active_workspace_scope
resolve_workspace_selection = _workspace_scopes.resolve_workspace_selection

ResolvedMCPServers = _mcp_resolution.ResolvedMCPServers
resolve_workspace_mcp_servers = _mcp_resolution.resolve_workspace_mcp_servers

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
WorkspaceRuntimeSettings = _runtime_settings.WorkspaceRuntimeSettings
resolve_workspace_runtime_settings = _runtime_settings.resolve_workspace_runtime_settings
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

    tenant_id = args.tenant_id or env.get("MINIGENT_CODING_TENANT_ID") or DEFAULT_TENANT_ID
    try:
        workspace_roots, active_workspace_scope = resolve_workspace_selection(
            args.workspace,
            args.workspace_scope,
            env,
            tenant_id=tenant_id,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    settings = resolve_workspace_runtime_settings(args, env)
    resolved_mcp_servers = resolve_workspace_mcp_servers(
        args,
        env,
        tenant_id=tenant_id,
        workspace_roots=workspace_roots,
        settings=settings,
    )
    mcp_servers_file = resolved_mcp_servers.source_file
    mcp_server_specs = resolved_mcp_servers.process_specs
    tenant_mcp_server_specs = resolved_mcp_servers.tenant_specs

    apply_tenant_runtime_environment(
        env,
        tenant_id,
        tenant_mcp_server_specs,
        workspace_roots=workspace_roots,
        workspace_scope=active_workspace_scope.name if active_workspace_scope else None,
    )
    if settings.gateway_enabled:
        for missing_name in tenant_gateway_mcp_server_mismatches(
            env,
            tenant_id,
            gateway_url_prefix=settings.gateway_url_prefix,
            specs=mcp_server_specs,
        ):
            print(
                "WARNING: tenant MCP server "
                f"'{missing_name}' points at the coding MCP gateway but no matching "
                "coding.mcp_server_specs entry was loaded; calls may return 404.",
                file=sys.stderr,
            )

    print_workspace_summary(
        env_file=args.env_file,
        no_env_file=args.no_env_file,
        env_file_explicit=env_file_explicit,
        workspace_roots=workspace_roots,
        workspace_scope=active_workspace_scope.name if active_workspace_scope else None,
        tenant_id=tenant_id,
        mcp_servers_file=mcp_servers_file,
        mcp_server_specs=mcp_server_specs,
        tenant_mcp_server_specs=tenant_mcp_server_specs,
        gateway_url_prefix=settings.gateway_url_prefix if settings.gateway_enabled else None,
        api_host=settings.api_host,
        api_port=settings.api_port,
    )

    return run_workspace_processes(
        env=env,
        mcp_server_specs=mcp_server_specs,
        skip_bridge=args.skip_bridge,
        gateway_enabled=settings.gateway_enabled,
        bridge_host=settings.bridge_host,
        gateway_port=settings.gateway_port,
        skip_api=args.skip_api,
        api_host=settings.api_host,
        api_port=settings.api_port,
        tenant_id=tenant_id,
        workspace=workspace_roots[0],
        bridge_name=settings.bridge_name,
        text_bridge_name=settings.text_bridge_name if settings.text_enabled else None,
        shell_bridge_name=settings.shell_bridge_name if settings.shell_enabled else None,
    )


apply_tenant_runtime_environment = _tenant_config.apply_tenant_runtime_environment
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
