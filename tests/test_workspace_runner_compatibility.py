from __future__ import annotations

import pytest

from app import coding_workspace_config as legacy_config
from app import coding_workspace_runner as legacy_runner
from minigent_workspace import (
    application,
    cli,
    config_export,
    environment,
    launch_commands,
    mcp_resolution,
    mcp_specs,
    orchestration,
    output,
    processes,
    runtime_plan,
    runtime_settings,
    scopes,
    tenant_config,
)

_EXPORTS = [
    (application, ["main", "run_workspace_command"]),
    (
        cli,
        [
            "parse_config_args",
            "build_coding_config_export_client_argv",
            "load_config_command_env",
            "run_config_command",
            "parse_args",
        ],
    ),
    (
        environment,
        ["apply_coding_workspace_state_defaults", "load_env_file", "apply_file_env_values"],
    ),
    (
        launch_commands,
        [
            "build_mcp_gateway_command",
            "build_builtin_mcp_server_specs",
            "build_mcp_stdio_bridge_command",
            "build_bridge_command",
            "shell_allowed_command_prefixes_from_env",
            "build_shell_mcp_server_command",
            "build_shell_bridge_command",
            "build_text_mcp_server_command",
            "build_text_bridge_command",
        ],
    ),
    (mcp_resolution, ["ResolvedMCPServers", "resolve_workspace_mcp_servers"]),
    (
        mcp_specs,
        [
            "CodingMCPServerSpec",
            "env_flag_enabled",
            "interpolate_config_string",
            "normalize_path_prefix",
            "mcp_server_specs_for_gateway",
            "mcp_gateway_config_from_specs",
            "write_mcp_gateway_config",
            "resolve_mcp_servers_file",
            "load_coding_mcp_server_specs",
            "load_coding_mcp_server_specs_from_json",
            "coding_mcp_server_spec_from_mapping",
            "expand_coding_mcp_command",
        ],
    ),
    (orchestration, ["run_workspace_processes"]),
    (output, ["print_workspace_summary", "print_demo_commands"]),
    (
        processes,
        [
            "start_process",
            "redacted_command_for_log",
            "wait_for_managed_http_server",
            "wait_for_processes",
            "stop_process",
        ],
    ),
    (runtime_plan, ["WorkspaceRuntimePlan", "prepare_workspace_runtime"]),
    (
        runtime_settings,
        ["WorkspaceRuntimeSettings", "resolve_workspace_runtime_settings"],
    ),
    (
        scopes,
        [
            "WorkspaceScope",
            "resolve_workspace_roots",
            "load_workspace_scopes_from_env",
            "skill_workspace_scope_from_env",
            "resolve_active_workspace_scope",
            "resolve_workspace_selection",
        ],
    ),
    (
        tenant_config,
        [
            "apply_tenant_runtime_environment",
            "tenant_mcp_server_from_spec",
            "capability_profiles_from_specs",
            "default_tenant_config_from_servers",
            "default_tenant_config",
            "inject_coding_mcp_servers",
            "inject_coding_workspace_skill",
            "coding_workspace_skill",
            "enrich_coding_workspace_skill",
            "append_workspace_roots_to_prompt",
            "tenant_gateway_mcp_server_mismatches",
            "bridge_allowed_tools_from_config",
            "bridge_path_globs",
        ],
    ),
]


@pytest.mark.parametrize(
    ("canonical_module", "name"),
    [(canonical_module, name) for canonical_module, names in _EXPORTS for name in names],
)
def test_legacy_runner_reexports_canonical_workspace_api(canonical_module, name: str) -> None:
    assert getattr(legacy_runner, name) is getattr(canonical_module, name)


def test_legacy_config_export_facade_reexports_canonical_helpers() -> None:
    assert legacy_config.export_local_coding_config is config_export.export_local_coding_config
    assert (
        legacy_config.load_coding_workspace_export_env
        is config_export.load_coding_workspace_export_env
    )
