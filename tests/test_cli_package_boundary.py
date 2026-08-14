from __future__ import annotations

from app import cli as legacy_cli
from minigent_client import (
    admin_commands,
    chat_commands,
    command_router,
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


def test_canonical_cli_uses_extracted_command_router() -> None:
    assert one_shot_cli.dispatch_command is command_router.dispatch_command


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
