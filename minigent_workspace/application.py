from __future__ import annotations

import sys

from minigent_workspace.cli import parse_args
from minigent_workspace.environment import apply_coding_workspace_state_defaults, load_env_file
from minigent_workspace.orchestration import run_workspace_processes
from minigent_workspace.output import print_workspace_summary
from minigent_workspace.runtime_plan import prepare_workspace_runtime


def run_workspace_command(raw_argv: list[str]) -> int:
    args = parse_args(raw_argv)
    env_file_explicit = any(
        item == "--env-file" or item.startswith("--env-file=") for item in raw_argv
    )
    env = load_env_file(
        None if args.no_env_file else args.env_file,
        warn_if_missing=env_file_explicit,
    )
    apply_coding_workspace_state_defaults(env)

    try:
        plan = prepare_workspace_runtime(args, env)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for missing_name in plan.gateway_mcp_server_mismatches:
        print(
            "WARNING: tenant MCP server "
            f"'{missing_name}' points at the coding MCP gateway but no matching "
            "coding.mcp_server_specs entry was loaded; calls may return 404.",
            file=sys.stderr,
        )

    settings = plan.settings
    mcp_servers = plan.mcp_servers
    workspace_scope = plan.active_workspace_scope.name if plan.active_workspace_scope else None
    print_workspace_summary(
        env_file=args.env_file,
        no_env_file=args.no_env_file,
        env_file_explicit=env_file_explicit,
        workspace_roots=plan.workspace_roots,
        workspace_scope=workspace_scope,
        tenant_id=plan.tenant_id,
        mcp_servers_file=mcp_servers.source_file,
        mcp_server_specs=mcp_servers.process_specs,
        tenant_mcp_server_specs=mcp_servers.tenant_specs,
        gateway_url_prefix=settings.gateway_url_prefix if settings.gateway_enabled else None,
        api_host=settings.api_host,
        api_port=settings.api_port,
    )

    return run_workspace_processes(
        env=env,
        mcp_server_specs=mcp_servers.process_specs,
        skip_bridge=args.skip_bridge,
        gateway_enabled=settings.gateway_enabled,
        bridge_host=settings.bridge_host,
        gateway_port=settings.gateway_port,
        skip_api=args.skip_api,
        api_host=settings.api_host,
        api_port=settings.api_port,
        tenant_id=plan.tenant_id,
        workspace=plan.workspace_roots[0],
        bridge_name=settings.bridge_name,
        text_bridge_name=settings.text_bridge_name if settings.text_enabled else None,
        shell_bridge_name=settings.shell_bridge_name if settings.shell_enabled else None,
    )
