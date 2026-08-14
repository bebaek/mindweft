from __future__ import annotations

import importlib.metadata

from app import cli as legacy_cli
from minigent_client import (
    admin_commands,
    application,
    chat_commands,
    command_router,
    config_commands,
    config_diagnostics,
    config_export,
    config_masking,
    diagnostic_commands,
    one_shot_cli,
    one_shot_parser,
)

_DIAGNOSTIC_COMMANDS = [
    "collect_debug_bundle",
    "run_debug_bundle",
    "run_execution_options",
    "run_health",
    "run_ping",
]

_CHAT_COMMANDS = [
    "build_client",
    "build_config",
    "build_trace_headers",
    "ensure_thread",
    "forget_thread",
    "list_remembered_threads",
    "load_remembered_thread",
    "pick_thread_from_history",
    "remember_thread",
    "run_chat",
    "run_export",
    "run_resume",
    "run_threads_create",
    "run_threads_delete",
    "run_threads_list",
    "run_threads_show",
    "state_scope_key",
    "validate_thread_create_options",
]

_ADMIN_COMMANDS = [
    "run_admin_audit_list",
    "run_admin_execution_config_export",
    "run_admin_execution_config_import",
    "run_admin_execution_config_validate_file",
    "run_admin_tenant_entitlements",
    "run_admin_tenant_users",
    "run_admin_tenants_create",
    "run_admin_tenants_list",
    "run_admin_tenants_seed",
    "run_admin_tenants_show",
    "run_admin_tenants_transition",
    "run_admin_tenants_update",
    "run_admin_threads_delete",
    "run_admin_threads_list",
    "run_admin_threads_prune",
    "run_admin_threads_show",
]


def test_config_commands_reexport_extracted_diagnostic_helpers() -> None:
    assert config_commands.DiagnosticCheck is config_diagnostics.DiagnosticCheck
    assert config_commands.run_config_doctor is config_diagnostics.run_config_doctor
    assert config_commands.collect_connection_checks is config_diagnostics.collect_connection_checks
    assert config_commands.server_config_checks is config_diagnostics.server_config_checks
    assert config_commands.server_summary is config_diagnostics.server_summary


def test_config_commands_reexport_extracted_serialization_helpers() -> None:
    assert (
        config_commands.export_unified_config_from_server
        is config_export.export_unified_config_from_server
    )
    assert config_commands.render_unified_config_toml is config_export.render_unified_config_toml
    assert config_commands.mask_secrets is config_masking.mask_secrets
    assert config_commands.mask_value is config_masking.mask_value


def test_console_script_entry_point_loads_canonical_application() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(script for script in scripts if script.name == "minigent")

    assert entry_point.load() is application.main


def test_one_shot_cli_reexports_canonical_application() -> None:
    assert one_shot_cli.main is application.main
    assert one_shot_cli._abort_detail is application._abort_detail
    assert one_shot_cli._apply_cli_env_file is application._apply_cli_env_file
    assert one_shot_cli._print_abort_message is application._print_abort_message


def test_canonical_cli_uses_extracted_command_router() -> None:
    assert one_shot_cli.dispatch_command is command_router.dispatch_command


def test_canonical_cli_reexports_config_doctor_handler() -> None:
    assert one_shot_cli.run_config_doctor is config_diagnostics.run_config_doctor


def test_canonical_cli_reexports_diagnostic_command_handlers() -> None:
    for name in _DIAGNOSTIC_COMMANDS:
        assert getattr(one_shot_cli, name) is getattr(diagnostic_commands, name)
    assert one_shot_cli._format_execution_options is diagnostic_commands._format_execution_options


def test_canonical_cli_reexports_chat_command_handlers() -> None:
    for name in _CHAT_COMMANDS:
        assert getattr(one_shot_cli, name) is getattr(chat_commands, name)
    assert one_shot_cli._format_markdown_transcript is chat_commands._format_markdown_transcript


def test_canonical_cli_reexports_admin_command_handlers() -> None:
    for name in _ADMIN_COMMANDS:
        assert getattr(one_shot_cli, name) is getattr(admin_commands, name)


def test_canonical_cli_reexports_parser_builder() -> None:
    assert one_shot_cli.build_parser is one_shot_parser.build_parser


def test_legacy_cli_reexports_canonical_entrypoint_and_urllib_module() -> None:
    assert legacy_cli.main is one_shot_cli.main
    assert legacy_cli.urllib is one_shot_cli.urllib
