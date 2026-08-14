from __future__ import annotations

from app import cli as legacy_cli
from minigent_client import admin_commands, one_shot_cli, one_shot_parser

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


def test_canonical_cli_reexports_admin_command_handlers() -> None:
    for name in _ADMIN_COMMANDS:
        assert getattr(one_shot_cli, name) is getattr(admin_commands, name)


def test_canonical_cli_reexports_parser_builder() -> None:
    assert one_shot_cli.build_parser is one_shot_parser.build_parser


def test_legacy_cli_reexports_canonical_entrypoint_and_urllib_module() -> None:
    assert legacy_cli.main is one_shot_cli.main
    assert legacy_cli.urllib is one_shot_cli.urllib
