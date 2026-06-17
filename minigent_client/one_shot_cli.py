from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import platform
import secrets
import sys
import urllib.parse
import urllib.request  # noqa: F401 - exposed for existing CLI tests that monkeypatch urlopen.
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TextIO, cast

from minigent_client.api_client import MinigentAPIClient
from minigent_client.config import ClientConfig, build_client_config
from minigent_client.config_commands import (
    CONFIG_INIT_PROFILES,
    DEFAULT_CONFIG_PROFILE,
    DiagnosticCheck,
    collect_connection_checks,
    format_check,
    mask_secrets,
    mask_value,
    package_version,
    run_config,
    run_config_doctor,
    run_config_export,
    run_config_init,
    run_config_print,
    server_summary,
)
from minigent_client.errors import MinigentAPIError
from minigent_client.output import (
    StreamProgressRenderer,
    TokenMode,
    format_message,
    print_json,
    style_assistant_markdown,
    token_usage_from_event,
)
from minigent_client.state import ClientState, ThreadHistoryItem
from minigent_client.state import state_scope_key as build_state_scope_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Command-line client for a running Minigent API.")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL for the running API service.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Dotenv file to load for this command. Also sets MINIGENT_DOTENV_FILE.",
    )
    parser.add_argument(
        "--api-token",
        default=None,
        help="Bearer token sent via Authorization header.",
    )
    parser.add_argument(
        "--user-id",
        default="demo-user",
        help="Authenticated user ID sent as trusted headers when --api-token is not used.",
    )
    parser.add_argument(
        "--tenant-id",
        default="demo-tenant",
        help="Authenticated tenant ID sent as trusted headers when --api-token is not used.",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Mark the trusted-header principal as an admin when --api-token is not used.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Send a W3C traceparent header and print the trace ID.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra run progress metadata in streaming text mode.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    chat_parser = subparsers.add_parser(
        "chat", help="Send a user message and print the assistant reply."
    )
    chat_parser.add_argument("message", help="User message content to send.")
    chat_target_group = chat_parser.add_mutually_exclusive_group()
    chat_target_group.add_argument("--thread", default=None, help="Existing thread ID to continue.")
    chat_target_group.add_argument(
        "--resume-last",
        action="store_true",
        help="Resume the last locally remembered thread for this server and principal.",
    )
    chat_parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Image file to attach to the message. Can be specified multiple times.",
    )
    chat_parser.add_argument(
        "--image-detail",
        choices=["auto", "low", "high"],
        default="auto",
        help="Vision detail hint for attached images.",
    )
    chat_parser.add_argument(
        "--skill", default=None, help="Skill to apply when creating a new thread."
    )
    chat_parser.add_argument(
        "--skills",
        nargs="+",
        default=None,
        help="Ordered list of prompt-overlay skills to apply when creating a new thread.",
    )
    chat_parser.add_argument(
        "--capability-profile",
        default=None,
        help="Capability profile to apply when creating a new thread.",
    )
    chat_parser.add_argument(
        "--print-thread-id",
        action="store_true",
        help="Print the thread ID before the reply in text mode.",
    )
    chat_parser.add_argument(
        "--transcript",
        action="store_true",
        help="Print the full thread transcript after the reply in text mode.",
    )
    chat_parser.add_argument(
        "--stream",
        action="store_true",
        help="Use the NDJSON streaming run endpoint and print progress in text mode.",
    )
    chat_parser.add_argument(
        "--show-tool-results",
        action="store_true",
        help="In streaming text mode, print expanded tool result bodies to stderr.",
    )
    chat_parser.add_argument(
        "--show-reasoning",
        action="store_true",
        help="Show model reasoning/thinking content when available.",
    )
    chat_parser.add_argument(
        "--tokens",
        choices=["auto", "live", "off"],
        default="auto",
        help="Token display mode for streaming progress and JSON output.",
    )

    run_parser = subparsers.add_parser(
        "run", help="Run one non-interactive prompt, reading stdin when no prompt is provided."
    )
    run_parser.add_argument(
        "message",
        nargs="?",
        default=None,
        help="Prompt text. If omitted, prompt text is read from stdin.",
    )
    run_target_group = run_parser.add_mutually_exclusive_group()
    run_target_group.add_argument("--thread", default=None, help="Existing thread ID to continue.")
    run_target_group.add_argument(
        "--resume-last",
        action="store_true",
        help="Resume the last locally remembered thread for this server and principal.",
    )
    run_parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Image file to attach to the prompt. Can be specified multiple times.",
    )
    run_parser.add_argument(
        "--image-detail",
        choices=["auto", "low", "high"],
        default="auto",
        help="Vision detail hint for attached images.",
    )
    run_parser.add_argument("--skill", default=None, help="Skill to apply when creating a thread.")
    run_parser.add_argument(
        "--skills",
        nargs="+",
        default=None,
        help="Ordered list of prompt-overlay skills to apply when creating a thread.",
    )
    run_parser.add_argument(
        "--capability-profile",
        default=None,
        help="Capability profile to apply when creating a thread.",
    )
    run_parser.add_argument(
        "--plain",
        action="store_true",
        help="Print only the assistant reply to stdout (default for text output).",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output for this run.",
    )
    run_parser.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        default=False,
        help="Use the non-streaming run endpoint (default; explicit for scripts).",
    )
    run_parser.add_argument(
        "--stream",
        dest="stream",
        action="store_true",
        help="Use the streaming run endpoint; progress is written to stderr unless --quiet is set.",
    )
    run_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress non-essential stderr progress for this run.",
    )
    run_parser.add_argument(
        "--show-tool-results",
        action="store_true",
        help="With --stream, print expanded tool result bodies to stderr unless --quiet is set.",
    )
    run_parser.add_argument(
        "--show-reasoning",
        action="store_true",
        help="Show model reasoning/thinking content when available.",
    )
    run_parser.add_argument(
        "--tokens",
        choices=["auto", "live", "off"],
        default="auto",
        help="Token display mode for streaming progress and JSON output.",
    )
    run_parser.set_defaults(
        print_thread_id=False,
        transcript=False,
        quiet=False,
    )

    resume_parser = subparsers.add_parser(
        "resume", help="Show and remember the latest or selected local thread."
    )
    resume_parser.add_argument(
        "thread_id",
        nargs="?",
        default=None,
        help="Thread ID to resume. Defaults to the latest locally remembered thread.",
    )
    resume_parser.add_argument(
        "--print-thread-id",
        action="store_true",
        help="Print the selected thread ID before the transcript in text mode.",
    )
    resume_parser.add_argument(
        "--no-picker",
        dest="thread_picker",
        action="store_false",
        default=True,
        help="When resuming without an ID, skip the interactive thread picker and use the latest thread.",
    )

    export_parser = subparsers.add_parser("export", help="Export a thread transcript.")
    export_parser.add_argument(
        "thread_id",
        nargs="?",
        default=None,
        help="Thread ID to export. Defaults to the latest locally remembered thread.",
    )
    export_parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Transcript output format.",
    )

    threads_parser = subparsers.add_parser("threads", help="Manage conversation threads.")
    threads_subparsers = threads_parser.add_subparsers(dest="threads_command")
    threads_subparsers.add_parser("list", help="List locally remembered threads.")

    threads_create_parser = threads_subparsers.add_parser("create", help="Create a new thread.")
    threads_create_parser.add_argument(
        "--skill",
        default=None,
        help="Skill to apply when creating the thread.",
    )
    threads_create_parser.add_argument(
        "--skills",
        nargs="+",
        default=None,
        help="Ordered list of prompt-overlay skills to apply when creating the thread.",
    )
    threads_create_parser.add_argument(
        "--capability-profile",
        default=None,
        help="Capability profile to apply when creating the thread.",
    )

    threads_show_parser = threads_subparsers.add_parser("show", help="Show thread messages.")
    threads_show_parser.add_argument("thread_id", help="Thread ID to display.")

    threads_delete_parser = threads_subparsers.add_parser("delete", help="Delete a thread.")
    threads_delete_parser.add_argument("thread_id", help="Thread ID to delete.")

    admin_parser = subparsers.add_parser("admin", help="Admin inspection commands.")
    admin_subparsers = admin_parser.add_subparsers(dest="admin_command", required=True)

    admin_tenants_parser = admin_subparsers.add_parser("tenants", help="Manage tenants.")
    admin_tenants_subparsers = admin_tenants_parser.add_subparsers(
        dest="admin_tenants_command", required=True
    )
    admin_tenants_list_parser = admin_tenants_subparsers.add_parser(
        "list", help="List registry tenants."
    )
    admin_tenants_list_parser.add_argument("--limit", type=int, default=None)
    admin_tenants_list_parser.add_argument("--offset", type=int, default=None)
    admin_tenants_list_parser.add_argument(
        "--status",
        choices=["active", "provisioning", "suspended", "archived", "deleted"],
        default=None,
    )
    admin_tenants_list_parser.add_argument("--plan", default=None)
    admin_tenants_list_parser.add_argument("--slug", default=None)

    admin_tenants_create_parser = admin_tenants_subparsers.add_parser(
        "create", help="Create a tenant."
    )
    admin_tenants_create_parser.add_argument("--id", dest="new_tenant_id", default=None)
    admin_tenants_create_parser.add_argument("--slug", required=True)
    admin_tenants_create_parser.add_argument("--name", required=True)
    admin_tenants_create_parser.add_argument(
        "--status",
        choices=["active", "provisioning", "suspended", "archived", "deleted"],
        default=None,
    )
    admin_tenants_create_parser.add_argument("--plan", default=None)
    admin_tenants_create_parser.add_argument("--region", default=None)
    admin_tenants_create_parser.add_argument(
        "--metadata-json",
        default=None,
        help="Tenant metadata as a JSON object.",
    )

    admin_tenants_show_parser = admin_tenants_subparsers.add_parser("show", help="Show one tenant.")
    admin_tenants_show_parser.add_argument("tenant_id")

    admin_tenants_update_parser = admin_tenants_subparsers.add_parser(
        "update", help="Update tenant fields."
    )
    admin_tenants_update_parser.add_argument("tenant_id")
    admin_tenants_update_parser.add_argument("--slug", default=None)
    admin_tenants_update_parser.add_argument("--name", default=None)
    admin_tenants_update_parser.add_argument("--plan", default=None)
    admin_tenants_update_parser.add_argument("--region", default=None)
    admin_tenants_update_parser.add_argument(
        "--metadata-json",
        default=None,
        help="Replace tenant metadata with this JSON object.",
    )

    for command_name in ["activate", "suspend", "archive", "delete"]:
        transition_parser = admin_tenants_subparsers.add_parser(
            command_name, help=f"{command_name.title()} a tenant."
        )
        transition_parser.add_argument("tenant_id")

    admin_tenants_seed_parser = admin_tenants_subparsers.add_parser(
        "seed", help="Seed registry tenants from an existing source."
    )
    admin_tenants_seed_parser.add_argument(
        "--from",
        dest="seed_source",
        choices=["execution-configs"],
        default="execution-configs",
    )
    admin_tenants_seed_parser.add_argument(
        "--status",
        choices=["active", "provisioning", "suspended", "archived", "deleted"],
        default="active",
    )
    admin_tenants_seed_parser.add_argument("--plan", default=None)
    admin_tenants_seed_parser.add_argument("--region", default=None)
    admin_tenants_seed_parser.add_argument("--dry-run", action="store_true")

    admin_execution_config_parser = admin_subparsers.add_parser(
        "execution-config", help="Import, export, and validate tenant execution configs."
    )
    admin_execution_config_subparsers = admin_execution_config_parser.add_subparsers(
        dest="admin_execution_config_command", required=True
    )
    admin_execution_config_import_parser = admin_execution_config_subparsers.add_parser(
        "import", help="Import tenant execution configs from a JSON file into the admin store."
    )
    admin_execution_config_import_parser.add_argument("file", help="JSON file to import.")
    admin_execution_config_import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report changes without writing configs.",
    )
    admin_execution_config_import_parser.add_argument(
        "--upsert",
        action="store_true",
        help="Write valid configs to the admin store. Required unless --dry-run is used.",
    )
    admin_execution_config_import_parser.add_argument(
        "--tenant",
        dest="import_tenant_id",
        default=None,
        help="Import only one tenant from the JSON file.",
    )
    admin_execution_config_import_parser.add_argument(
        "--seed-tenants",
        action="store_true",
        help="Seed missing tenant registry records from imported execution-config tenant IDs.",
    )
    admin_execution_config_import_parser.add_argument(
        "--status",
        choices=["active", "provisioning", "suspended", "archived", "deleted"],
        default="active",
        help="Tenant status to use with --seed-tenants.",
    )
    admin_execution_config_import_parser.add_argument("--plan", default=None)
    admin_execution_config_import_parser.add_argument("--region", default=None)

    admin_execution_config_export_parser = admin_execution_config_subparsers.add_parser(
        "export", help="Export stored tenant execution configs as JSON."
    )
    admin_execution_config_export_parser.add_argument(
        "--tenant",
        dest="export_tenant_id",
        default=None,
        help="Export only one tenant.",
    )
    admin_execution_config_export_parser.add_argument(
        "--out",
        dest="output",
        default=None,
        help="Write JSON to this file instead of stdout.",
    )
    admin_execution_config_export_parser.add_argument(
        "--redacted",
        action="store_true",
        default=True,
        help="Export redacted configs returned by the admin API (default).",
    )

    admin_execution_config_validate_parser = admin_execution_config_subparsers.add_parser(
        "validate-file", help="Validate a tenant execution-config JSON file without importing."
    )
    admin_execution_config_validate_parser.add_argument("file", help="JSON file to validate.")
    admin_execution_config_validate_parser.add_argument(
        "--tenant",
        dest="validate_tenant_id",
        default=None,
        help="Validate only one tenant from the JSON file.",
    )

    admin_tenants_users_parser = admin_tenants_subparsers.add_parser(
        "users", help="Manage tenant users."
    )
    admin_tenants_users_subparsers = admin_tenants_users_parser.add_subparsers(
        dest="admin_tenant_users_command", required=True
    )
    admin_tenant_users_list_parser = admin_tenants_users_subparsers.add_parser(
        "list", help="List tenant users."
    )
    admin_tenant_users_list_parser.add_argument("tenant_id")
    admin_tenant_users_list_parser.add_argument("--limit", type=int, default=None)
    admin_tenant_users_list_parser.add_argument("--offset", type=int, default=None)
    admin_tenant_users_list_parser.add_argument(
        "--status",
        choices=["invited", "active", "suspended", "deleted"],
        default=None,
    )
    admin_tenant_users_list_parser.add_argument(
        "--role",
        choices=["owner", "admin", "member", "viewer"],
        default=None,
    )
    admin_tenant_users_list_parser.add_argument("--email", default=None)

    admin_tenant_users_create_parser = admin_tenants_users_subparsers.add_parser(
        "create", help="Create a tenant user."
    )
    admin_tenant_users_create_parser.add_argument("tenant_id")
    admin_tenant_users_create_parser.add_argument("--user-id", required=True)
    admin_tenant_users_create_parser.add_argument("--email", default=None)
    admin_tenant_users_create_parser.add_argument("--display-name", default=None)
    admin_tenant_users_create_parser.add_argument(
        "--role",
        choices=["owner", "admin", "member", "viewer"],
        default=None,
    )
    admin_tenant_users_create_parser.add_argument(
        "--status",
        choices=["invited", "active", "suspended", "deleted"],
        default=None,
    )
    admin_tenant_users_create_parser.add_argument(
        "--metadata-json",
        default=None,
        help="Tenant user metadata as a JSON object.",
    )

    admin_tenant_users_show_parser = admin_tenants_users_subparsers.add_parser(
        "show", help="Show one tenant user."
    )
    admin_tenant_users_show_parser.add_argument("tenant_id")
    admin_tenant_users_show_parser.add_argument("user_record_id")

    admin_tenant_users_update_parser = admin_tenants_users_subparsers.add_parser(
        "update", help="Update tenant user fields."
    )
    admin_tenant_users_update_parser.add_argument("tenant_id")
    admin_tenant_users_update_parser.add_argument("user_record_id")
    admin_tenant_users_update_parser.add_argument("--email", default=None)
    admin_tenant_users_update_parser.add_argument("--display-name", default=None)
    admin_tenant_users_update_parser.add_argument(
        "--role",
        choices=["owner", "admin", "member", "viewer"],
        default=None,
    )
    admin_tenant_users_update_parser.add_argument(
        "--status",
        choices=["invited", "active", "suspended", "deleted"],
        default=None,
    )
    admin_tenant_users_update_parser.add_argument(
        "--metadata-json",
        default=None,
        help="Replace tenant user metadata with this JSON object.",
    )

    for command_name in ["activate", "suspend", "delete"]:
        user_transition_parser = admin_tenants_users_subparsers.add_parser(
            command_name, help=f"{command_name.title()} a tenant user."
        )
        user_transition_parser.add_argument("tenant_id")
        user_transition_parser.add_argument("user_record_id")

    admin_tenants_entitlements_parser = admin_tenants_subparsers.add_parser(
        "entitlements", help="Manage tenant entitlements."
    )
    admin_tenants_entitlements_subparsers = admin_tenants_entitlements_parser.add_subparsers(
        dest="admin_tenant_entitlements_command", required=True
    )
    admin_tenant_entitlements_show_parser = admin_tenants_entitlements_subparsers.add_parser(
        "show", help="Show tenant entitlements."
    )
    admin_tenant_entitlements_show_parser.add_argument("tenant_id")
    for command_name in ["set", "validate"]:
        entitlements_parser = admin_tenants_entitlements_subparsers.add_parser(
            command_name, help=f"{command_name.title()} tenant entitlements."
        )
        entitlements_parser.add_argument("tenant_id")
        entitlements_parser.add_argument("--features-json", default="{}")
        entitlements_parser.add_argument("--limits-json", default="{}")
    admin_tenant_entitlements_delete_parser = admin_tenants_entitlements_subparsers.add_parser(
        "delete", help="Delete tenant entitlements."
    )
    admin_tenant_entitlements_delete_parser.add_argument("tenant_id")

    admin_threads_parser = admin_subparsers.add_parser("threads", help="Inspect tenant threads.")
    admin_threads_subparsers = admin_threads_parser.add_subparsers(
        dest="admin_threads_command", required=True
    )

    admin_threads_list_parser = admin_threads_subparsers.add_parser(
        "list", help="List threads for a tenant."
    )
    admin_threads_list_parser.add_argument(
        "--tenant",
        required=True,
        dest="admin_tenant_id",
        help="Tenant ID whose threads should be listed.",
    )
    admin_threads_list_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of threads to return (server default: 50, max: 500).",
    )
    admin_threads_list_parser.add_argument(
        "--offset",
        type=int,
        default=None,
        help="Zero-based result offset for pagination.",
    )
    admin_threads_list_parser.add_argument(
        "--status",
        choices=["idle", "running", "error"],
        default=None,
        help="Filter by thread status.",
    )
    admin_threads_list_parser.add_argument(
        "--profile",
        default=None,
        help="Filter by capability profile.",
    )
    admin_threads_list_parser.add_argument(
        "--skill",
        default=None,
        help="Filter by skill name.",
    )
    admin_threads_list_parser.add_argument(
        "--created-after",
        default=None,
        help="Filter to threads created after this ISO-8601 timestamp.",
    )
    admin_threads_list_parser.add_argument(
        "--updated-after",
        default=None,
        help="Filter to threads updated after this ISO-8601 timestamp.",
    )

    admin_threads_show_parser = admin_threads_subparsers.add_parser(
        "show", help="Show admin thread metadata, context, and messages."
    )
    admin_threads_show_parser.add_argument("thread_id", help="Thread ID to inspect.")
    admin_threads_show_parser.add_argument(
        "--tenant",
        required=True,
        dest="admin_tenant_id",
        help="Tenant ID that owns the thread.",
    )

    admin_threads_delete_parser = admin_threads_subparsers.add_parser(
        "delete", help="Delete a tenant thread as an admin."
    )
    admin_threads_delete_parser.add_argument("thread_id", help="Thread ID to delete.")
    admin_threads_delete_parser.add_argument(
        "--tenant",
        required=True,
        dest="admin_tenant_id",
        help="Tenant ID that owns the thread.",
    )

    admin_threads_prune_parser = admin_threads_subparsers.add_parser(
        "prune", help="Delete tenant threads older than a timestamp."
    )
    admin_threads_prune_parser.add_argument(
        "--tenant",
        required=True,
        dest="admin_tenant_id",
        help="Tenant ID whose threads should be pruned.",
    )
    admin_threads_prune_parser.add_argument(
        "--updated-before",
        required=True,
        help="Delete threads updated before this ISO-8601 timestamp.",
    )
    admin_threads_prune_parser.add_argument(
        "--status",
        choices=["idle", "running", "error"],
        default=None,
        help="Restrict pruning to threads with this status.",
    )
    admin_threads_prune_parser.add_argument(
        "--profile",
        default=None,
        help="Restrict pruning to a capability profile.",
    )
    admin_threads_prune_parser.add_argument(
        "--skill",
        default=None,
        help="Restrict pruning to a skill name.",
    )
    admin_threads_prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview matching threads without deleting them or writing audit records.",
    )

    admin_audit_parser = admin_subparsers.add_parser("audit", help="Inspect admin audit records.")
    admin_audit_subparsers = admin_audit_parser.add_subparsers(
        dest="admin_audit_command", required=True
    )
    admin_audit_list_parser = admin_audit_subparsers.add_parser(
        "list", help="List audit records for a tenant."
    )
    admin_audit_list_parser.add_argument(
        "--tenant",
        required=True,
        dest="admin_tenant_id",
        help="Tenant ID whose audit records should be listed.",
    )
    admin_audit_list_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of audit records to return (server default: 50, max: 500).",
    )
    admin_audit_list_parser.add_argument(
        "--offset",
        type=int,
        default=None,
        help="Zero-based result offset for pagination.",
    )
    admin_audit_list_parser.add_argument(
        "--action",
        default=None,
        help="Filter audit records by action, such as threads.prune.",
    )
    admin_audit_list_parser.add_argument(
        "--actor",
        default=None,
        help="Filter audit records by actor user ID.",
    )
    admin_audit_list_parser.add_argument(
        "--created-after",
        default=None,
        help="Filter audit records created after this ISO-8601 timestamp.",
    )
    admin_audit_list_parser.add_argument(
        "--created-before",
        default=None,
        help="Filter audit records created before this ISO-8601 timestamp.",
    )

    subparsers.add_parser("health", help="Check API health.")
    subparsers.add_parser("ping", help="Check API reachability and basic server config.")
    subparsers.add_parser("options", help="List available skills and capability profiles.")
    subparsers.add_parser("skills", help="List available skills for the current tenant.")
    subparsers.add_parser(
        "capabilities", help="List available capability profiles for the current tenant."
    )
    debug_bundle_parser = subparsers.add_parser(
        "debug-bundle", help="Collect masked local/server diagnostics for bug reports."
    )
    debug_bundle_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the debug bundle as structured JSON.",
    )
    debug_bundle_parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write the debug bundle instead of stdout.",
    )

    config_parser = subparsers.add_parser(
        "config", help="Show or inspect resolved API configuration."
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("show", help="Show resolved API configuration as JSON.")
    config_init_parser = config_subparsers.add_parser(
        "init", help="Create a starter minigent.toml in the current directory."
    )
    config_init_parser.add_argument(
        "--output",
        default="minigent.toml",
        help="Path to write. Defaults to ./minigent.toml.",
    )
    config_init_parser.add_argument(
        "--profile",
        choices=CONFIG_INIT_PROFILES,
        default=DEFAULT_CONFIG_PROFILE,
        help="Starter config profile to write. Defaults to local-coding.",
    )
    config_init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    config_print_parser = config_subparsers.add_parser(
        "print", help="Print local unified config information."
    )
    config_print_parser.add_argument(
        "--resolved",
        action="store_true",
        help="Print the env vars resolved from minigent.toml with secrets masked.",
    )
    config_export_parser = config_subparsers.add_parser(
        "export", help="Export a best-effort minigent.toml from a running server."
    )
    config_export_parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write instead of stdout.",
    )
    config_export_parser.add_argument(
        "--include-runtime",
        action="store_true",
        help="Include informational runtime status/tool snapshots in the export.",
    )
    config_export_parser.add_argument(
        "--local-coding",
        action="store_true",
        help="Merge locally resolved coding workspace runner config into the export.",
    )
    config_export_parser.add_argument(
        "--coding-env-file",
        default=None,
        help="Dotenv file for --local-coding. Defaults to --env-file or .env.coding.",
    )
    config_subparsers.add_parser("doctor", help="Check common CLI/API configuration issues.")

    return parser


def build_trace_headers(trace_id: str | None) -> dict[str, str]:
    if trace_id is None:
        return {}
    parent_id = secrets.token_hex(8)
    return {"traceparent": f"00-{trace_id}-{parent_id}-01"}


def state_scope_key(base_url: str, args: argparse.Namespace) -> str:
    return build_state_scope_key(
        base_url,
        api_token=args.api_token,
        user_id=args.user_id,
        tenant_id=args.tenant_id,
        is_admin=args.admin,
    )


def remember_thread(
    base_url: str,
    args: argparse.Namespace,
    thread_id: str,
    *,
    title: str | None = None,
    message_count: int | None = None,
) -> None:
    state = ClientState.load()
    state.set_last_thread(
        state_scope_key(base_url, args), thread_id, title=title, message_count=message_count
    )
    state.save()


def load_remembered_thread(base_url: str, args: argparse.Namespace) -> str:
    state = ClientState.load()
    threads = state.list_threads(state_scope_key(base_url, args))
    if (
        getattr(args, "thread_picker", False)
        and len(threads) > 1
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        selected_thread_id = pick_thread_from_history(threads)
        if selected_thread_id is not None:
            return selected_thread_id
    thread_id = state.get_last_thread(state_scope_key(base_url, args))
    if thread_id is None:
        raise SystemExit("No remembered thread for this server and principal. Start a chat first.")
    return thread_id


def pick_thread_from_history(threads: list[ThreadHistoryItem]) -> str | None:
    try:
        prompt_toolkit_module = __import__("prompt_toolkit")
        completion_module = __import__("prompt_toolkit.completion", fromlist=["WordCompleter"])
    except ImportError:
        return _pick_thread_from_numbered_list(threads)
    PromptSession = prompt_toolkit_module.PromptSession
    WordCompleter = completion_module.WordCompleter
    print("Select a thread by number, ID, or search text; blank cancels.")
    _print_numbered_thread_history(threads)
    session = PromptSession(
        completer=WordCompleter([item.thread_id for item in threads], ignore_case=True)
    )
    try:
        selection = session.prompt("thread> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nThread selection cancelled.")
        return None
    return _resolve_thread_selection(selection, threads)


def _pick_thread_from_numbered_list(threads: list[ThreadHistoryItem]) -> str | None:
    print("Select a thread by number, ID, or search text; blank cancels.")
    _print_numbered_thread_history(threads)
    return _resolve_thread_selection(input("thread> ").strip(), threads)


def _print_numbered_thread_history(threads: list[ThreadHistoryItem]) -> None:
    for index, item in enumerate(threads, start=1):
        message_count = "?" if item.message_count is None else str(item.message_count)
        print(
            f"{index}. {item.updated_at or 'unknown'}  {item.title or 'Untitled thread'}  "
            f"messages={message_count}  {item.thread_id}"
        )


def _resolve_thread_selection(selection: str, threads: list[ThreadHistoryItem]) -> str | None:
    if not selection:
        return None
    if selection.isdigit():
        index = int(selection) - 1
        if 0 <= index < len(threads):
            return threads[index].thread_id
    for item in threads:
        if item.thread_id == selection:
            return item.thread_id
    normalized = selection.casefold()
    matches = [
        item
        for item in threads
        if normalized in item.thread_id.casefold()
        or normalized in (item.title or "").casefold()
        or normalized in (item.updated_at or "").casefold()
    ]
    if len(matches) == 1:
        return matches[0].thread_id
    return None


def list_remembered_threads(base_url: str, args: argparse.Namespace) -> list[ThreadHistoryItem]:
    state = ClientState.load()
    return state.list_threads(state_scope_key(base_url, args))


def forget_thread(base_url: str, args: argparse.Namespace, thread_id: str) -> None:
    state = ClientState.load()
    if state.forget_last_thread(state_scope_key(base_url, args), thread_id):
        state.save()


def build_config(args: argparse.Namespace, trace_id: str | None) -> ClientConfig:
    return build_client_config(
        base_url=args.base_url,
        api_token=args.api_token,
        user_id=args.user_id,
        tenant_id=args.tenant_id,
        admin=args.admin,
        stream_runs=getattr(args, "stream", False),
        extra_headers=build_trace_headers(trace_id),
        wake_phrase="hey minigent",
        env_config=ClientConfig(base_url=args.base_url.rstrip("/"), wake_phrase="hey minigent"),
    )


class _QuietProgressStream:
    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        return None


def build_client(args: argparse.Namespace, trace_id: str | None) -> MinigentAPIClient:
    progress_stream: TextIO = (
        cast(TextIO, _QuietProgressStream()) if getattr(args, "quiet", False) else sys.stderr
    )
    token_mode = cast(
        TokenMode,
        "off" if getattr(args, "quiet", False) else getattr(args, "tokens", "auto"),
    )
    return MinigentAPIClient(
        build_config(args, trace_id),
        progress_stream=progress_stream,
        progress_verbose=args.verbose and not getattr(args, "quiet", False),
        show_tool_results=getattr(args, "show_tool_results", False)
        and not getattr(args, "quiet", False),
        show_reasoning=getattr(args, "show_reasoning", False) and not getattr(args, "quiet", False),
        token_mode=token_mode,
    )


def validate_thread_create_options(args: argparse.Namespace) -> None:
    if args.skill is not None and args.skills is not None:
        raise SystemExit("Provide either --skill or --skills, not both.")


def ensure_thread(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    base_url: str,
) -> tuple[str, bool]:
    if args.thread:
        return args.thread, False
    if args.resume_last:
        return load_remembered_thread(base_url, args), False
    validate_thread_create_options(args)
    response = client.create_thread(
        skill_name=args.skill,
        skills=args.skills,
        capability_profile=args.capability_profile,
    )
    thread_id = response["thread_id"]
    if not isinstance(thread_id, str):
        raise SystemExit("Create-thread response did not include a thread_id.")
    return thread_id, True


def _thread_title_from_message(message: str) -> str:
    normalized = " ".join(message.split())
    if len(normalized) <= 60:
        return normalized or "Thread"
    return f"{normalized[:57]}..."


def _title_from_thread_messages(messages: object) -> str | None:
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return _thread_title_from_message(content)
    return None


def _make_stream_progress_printer(
    *, verbose: bool = False, show_tool_results: bool = False, token_mode: str = "auto"
) -> Any:
    renderer = StreamProgressRenderer(
        sys.stderr,
        verbose=verbose,
        show_tool_results=show_tool_results,
        token_mode=cast(TokenMode, token_mode),
    )
    return renderer.render


def _usage_from_run_stream(events: list[dict[str, Any]]) -> dict[str, int] | None:
    usage: dict[str, int] | None = None
    for event in events:
        event_usage = token_usage_from_event(event)
        if event_usage is not None:
            usage = event_usage
    return usage


def _reply_from_run_stream(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") == "run.error":
            raise SystemExit(f"run failed: {event.get('status_code')} {event.get('detail')}")
    for event in reversed(events):
        if event.get("type") == "assistant.message":
            content = event.get("content")
            if isinstance(content, str):
                return content
    raise SystemExit("run stream ended without an assistant.message event")


def _read_run_message(args: argparse.Namespace) -> str:
    if args.message is not None:
        return str(args.message)
    if sys.stdin.isatty():
        raise SystemExit("Provide a prompt argument or pipe prompt text on stdin.")
    message = sys.stdin.read()
    if not message.strip():
        raise SystemExit("No prompt text received on stdin.")
    return message.rstrip("\n")


def _image_parts_from_paths(
    paths: Sequence[str] | None, *, detail: str = "auto"
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for raw_path in paths or []:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise SystemExit(f"image file not found: {raw_path}")
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None or not mime_type.startswith("image/"):
            raise SystemExit(f"could not determine image MIME type: {raw_path}")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append(
            {
                "type": "image",
                "mime_type": mime_type,
                "data": data,
                "detail": detail,
            }
        )
    return parts


def _message_parts(
    content: str, image_paths: Sequence[str] | None, *, detail: str
) -> list[dict[str, Any]] | None:
    image_parts = _image_parts_from_paths(image_paths, detail=detail)
    if not image_parts:
        return None
    parts: list[dict[str, Any]] = []
    if content:
        parts.append({"type": "text", "text": content})
    parts.extend(image_parts)
    return parts


def run_chat(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    thread_id, created_thread = ensure_thread(args, client, base_url)
    message_parts = _message_parts(
        args.message,
        getattr(args, "image", None),
        detail=getattr(args, "image_detail", "auto"),
    )
    client.add_message(thread_id, args.message, parts=message_parts)
    events: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None
    if args.stream and args.json:
        events = list(
            client.request_ndjson_events("POST", f"{base_url}/threads/{thread_id}/run/stream")
        )
        reply = _reply_from_run_stream(events)
    else:
        reply, metadata = client.run_thread(thread_id, stream=args.stream)
    remember_thread(base_url, args, thread_id, title=_thread_title_from_message(args.message))

    if args.json:
        output: dict[str, Any] = {
            "thread_id": thread_id,
            "created_thread": created_thread,
            "reply": reply,
        }
        if events is not None:
            output["events"] = events
            usage = _usage_from_run_stream(events)
            if usage is not None:
                output["usage"] = usage
        if metadata:
            output["metadata"] = metadata
        if trace_id is not None:
            output["trace_id"] = trace_id
        if args.transcript:
            output["messages"] = client.get_thread(thread_id)["messages"]
        print_json(output)
        return 0

    if trace_id is not None:
        print(f"trace_id={trace_id}")
    if args.print_thread_id:
        print(f"thread_id={thread_id}")
    # Display reasoning content if present and enabled (only for non-streaming mode)
    # In streaming mode, reasoning is displayed via 'reasoning' events
    if getattr(args, "show_reasoning", False) and not getattr(args, "stream", False):
        from minigent_client.output import extract_reasoning_content, format_reasoning_block

        reasoning = extract_reasoning_content(metadata)
        if reasoning:
            print(format_reasoning_block(reasoning, stream=sys.stdout))
    _print_assistant_reply(reply)
    client.flush_pending_token_summary()
    if args.transcript:
        print("")
        for message in client.get_thread(thread_id)["messages"]:
            print(format_message(message))
    return 0


def _print_assistant_reply(reply: str) -> None:
    print(style_assistant_markdown(reply, stream=sys.stdout))


def run_threads_list(
    args: argparse.Namespace,
    base_url: str,
    trace_id: str | None,
) -> int:
    threads = list_remembered_threads(base_url, args)
    if args.json:
        output: dict[str, Any] = {"threads": [item.to_dict() for item in threads]}
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    if not threads:
        print("No locally remembered threads.")
        return 0
    print("Recent threads")
    print("")
    for index, item in enumerate(threads, start=1):
        title = item.title or "Untitled thread"
        updated_at = item.updated_at or "unknown"
        message_count = "?" if item.message_count is None else str(item.message_count)
        print(f"{index}. {updated_at}  {title}  messages={message_count}  {item.thread_id}")
    return 0


def run_resume(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    thread_id = args.thread_id or load_remembered_thread(base_url, args)
    thread = client.get_thread(thread_id)
    messages = thread["messages"]
    remember_thread(
        base_url,
        args,
        thread_id,
        title=_title_from_thread_messages(messages),
        message_count=len(messages) if isinstance(messages, list) else None,
    )
    if args.json:
        output: dict[str, Any] = {"thread_id": thread_id, "messages": messages}
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    if args.print_thread_id:
        print(f"thread_id={thread_id}")
    for message in messages:
        print(format_message(message))
    return 0


def _format_markdown_transcript(thread_id: str, messages: list[dict[str, Any]]) -> str:
    lines = ["# Minigent transcript", "", f"Thread: `{thread_id}`", ""]
    for message in messages:
        role = str(message.get("role") or "message").replace("_", " ").title()
        tool_name = message.get("tool_name")
        heading = role if not tool_name else f"{role} ({tool_name})"
        content = str(message.get("content") or "")
        lines.extend([f"## {heading}", "", content, ""])
    return "\n".join(lines).rstrip() + "\n"


def run_export(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    thread_id = args.thread_id or load_remembered_thread(base_url, args)
    thread = client.get_thread(thread_id)
    messages = thread["messages"]
    remember_thread(
        base_url,
        args,
        thread_id,
        title=_title_from_thread_messages(messages),
        message_count=len(messages) if isinstance(messages, list) else None,
    )
    if args.format == "json":
        output: dict[str, Any] = {"thread_id": thread_id, "messages": messages}
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"<!-- trace_id={trace_id} -->")
    print(_format_markdown_transcript(thread_id, messages), end="")
    return 0


def run_threads_create(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    validate_thread_create_options(args)
    response = client.create_thread(
        skill_name=args.skill,
        skills=args.skills,
        capability_profile=args.capability_profile,
    )
    thread_id = response["thread_id"]
    if not isinstance(thread_id, str):
        raise SystemExit("Create-thread response did not include a thread_id.")
    remember_thread(base_url, args, thread_id, title="New thread")
    if args.json:
        output: dict[str, Any] = {"thread_id": thread_id}
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(thread_id)
    return 0


def run_threads_show(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    thread = client.get_thread(args.thread_id)
    messages = thread["messages"]
    if args.json:
        output: dict[str, Any] = {"thread_id": args.thread_id, "messages": messages}
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    for message in messages:
        print(format_message(message))
    return 0


def run_threads_delete(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    client.delete_thread(args.thread_id)
    forget_thread(base_url, args, args.thread_id)
    if args.json:
        output: dict[str, Any] = {"deleted": True, "thread_id": args.thread_id}
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(args.thread_id)
    return 0


def _json_object_from_arg(raw: str | None, label: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MinigentAPIError(
            f"{label} must be valid JSON.",
            category="invalid_request",
            detail=str(exc),
        ) from exc
    if not isinstance(parsed, dict):
        raise MinigentAPIError(
            f"{label} must be a JSON object.",
            category="invalid_request",
            detail=raw,
        )
    return cast(dict[str, Any], parsed)


def _metadata_from_arg(raw: str | None) -> dict[str, Any] | None:
    return _json_object_from_arg(raw, "--metadata-json")


def _tenant_payload(args: argparse.Namespace, *, create: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if create and args.new_tenant_id is not None:
        payload["id"] = args.new_tenant_id
    for key in ["slug", "name", "status", "plan", "region"]:
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    metadata = _metadata_from_arg(args.metadata_json)
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def run_admin_tenants_list(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    response = client.list_admin_tenants(
        limit=args.limit,
        offset=args.offset,
        status=args.status,
        plan=args.plan,
        slug=args.slug,
    )
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"total={response.get('total')}",
                f"limit={response.get('limit')}",
                f"offset={response.get('offset')}",
                f"next_offset={response.get('next_offset')}",
            ]
        )
    )
    for tenant in response.get("tenants", []):
        if not isinstance(tenant, dict):
            continue
        print(_format_tenant_line(tenant))
    return 0


def run_admin_tenants_create(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    response = client.create_admin_tenant(_tenant_payload(args, create=True))
    return _print_admin_tenant_response(args, response, trace_id)


def run_admin_tenants_show(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    response = client.get_admin_tenant(args.tenant_id)
    return _print_admin_tenant_response(args, response, trace_id)


def run_admin_tenants_update(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    payload = _tenant_payload(args, create=False)
    response = client.update_admin_tenant(args.tenant_id, payload)
    return _print_admin_tenant_response(args, response, trace_id)


def run_admin_tenants_transition(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    command = args.admin_tenants_command
    if command == "delete":
        response = client.delete_admin_tenant(args.tenant_id)
    else:
        response = client.transition_admin_tenant(args.tenant_id, command)
    return _print_admin_tenant_response(args, response, trace_id)


def run_admin_tenants_seed(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    payload: dict[str, Any] = {
        "source": args.seed_source,
        "status": args.status,
        "dry_run": args.dry_run,
    }
    if args.plan is not None:
        payload["plan"] = args.plan
    if args.region is not None:
        payload["region"] = args.region
    response = client.seed_admin_tenants(payload)
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"source={response.get('source')}",
                f"discovered={response.get('discovered')}",
                f"existing={response.get('existing')}",
                f"created={response.get('created')}",
                f"conflicts={response.get('conflicts')}",
                f"dry_run={response.get('dry_run')}",
            ]
        )
    )
    for tenant in response.get("tenants", []):
        if not isinstance(tenant, dict):
            continue
        print(
            " ".join(
                [
                    str(tenant.get("id", "")),
                    f"slug={tenant.get('slug')}",
                    f"status={tenant.get('status')}",
                    f"action={tenant.get('action')}",
                ]
            )
        )
    return 0


def _load_execution_config_file(path_text: str) -> dict[str, dict[str, Any]]:
    path = Path(path_text).expanduser()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MinigentAPIError(
            f"Execution-config file not found: {path_text}",
            category="invalid_request",
            detail=str(exc),
        ) from exc
    except json.JSONDecodeError as exc:
        raise MinigentAPIError(
            "Execution-config file must be valid JSON.",
            category="invalid_request",
            detail=str(exc),
        ) from exc
    if not isinstance(parsed, dict):
        raise MinigentAPIError(
            "Execution-config file must contain a JSON object.",
            category="invalid_request",
        )
    raw_configs = parsed.get("execution_configs") if "execution_configs" in parsed else parsed
    if not isinstance(raw_configs, dict):
        raise MinigentAPIError(
            "execution_configs must be a JSON object when present.",
            category="invalid_request",
        )
    configs: dict[str, dict[str, Any]] = {}
    for tenant_id, config in raw_configs.items():
        if not isinstance(tenant_id, str) or not tenant_id:
            raise MinigentAPIError(
                "Execution-config tenant IDs must be non-empty strings.",
                category="invalid_request",
            )
        if not isinstance(config, dict):
            raise MinigentAPIError(
                f"Execution config for tenant '{tenant_id}' must be a JSON object.",
                category="invalid_request",
            )
        configs[tenant_id] = cast(dict[str, Any], config)
    return configs


def _select_execution_configs(
    configs: dict[str, dict[str, Any]], tenant_id: str | None
) -> dict[str, dict[str, Any]]:
    if tenant_id is None:
        return configs
    config = configs.get(tenant_id)
    if config is None:
        raise MinigentAPIError(
            f"Execution-config file has no tenant '{tenant_id}'.",
            category="invalid_request",
        )
    return {tenant_id: config}


def _validation_ok(report: dict[str, Any]) -> bool:
    valid = report.get("valid")
    if isinstance(valid, bool):
        return valid
    status = report.get("status")
    if isinstance(status, str):
        return status.lower() in {"ok", "valid"}
    errors = report.get("errors")
    return not (isinstance(errors, list) and errors)


def _validation_summary(report: dict[str, Any]) -> str:
    if _validation_ok(report):
        return "valid"
    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        return f"invalid errors={len(errors)}"
    return "invalid"


def _validate_execution_configs(
    client: MinigentAPIClient, configs: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {
        tenant_id: client.validate_admin_tenant_execution_config(tenant_id, config)
        for tenant_id, config in configs.items()
    }


def run_admin_execution_config_validate_file(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    configs = _select_execution_configs(
        _load_execution_config_file(args.file), args.validate_tenant_id
    )
    reports = _validate_execution_configs(client, configs)
    ok = all(_validation_ok(report) for report in reports.values())
    output: dict[str, Any] = {
        "valid": ok,
        "tenant_count": len(configs),
        "tenants": [
            {
                "tenant_id": tenant_id,
                "valid": _validation_ok(report),
                "report": report,
            }
            for tenant_id, report in reports.items()
        ],
    }
    if trace_id is not None:
        output["trace_id"] = trace_id
    if args.json:
        print_json(output)
        return 0 if ok else 1
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(f"tenant_count={len(configs)} valid={ok}")
    for tenant_id, report in reports.items():
        print(f"{tenant_id} {_validation_summary(report)}")
    return 0 if ok else 1


def run_admin_execution_config_import(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    if not args.dry_run and not args.upsert:
        raise MinigentAPIError(
            "Import requires --dry-run or --upsert.",
            category="invalid_request",
        )
    configs = _select_execution_configs(
        _load_execution_config_file(args.file), args.import_tenant_id
    )
    reports = _validate_execution_configs(client, configs)
    valid_tenant_ids = [
        tenant_id for tenant_id, report in reports.items() if _validation_ok(report)
    ]
    invalid_tenant_ids = [
        tenant_id for tenant_id, report in reports.items() if not _validation_ok(report)
    ]
    written: list[str] = []
    if not args.dry_run:
        if invalid_tenant_ids:
            raise MinigentAPIError(
                "Import validation failed; no configs were written.",
                category="invalid_request",
                detail=", ".join(invalid_tenant_ids),
            )
        for tenant_id in valid_tenant_ids:
            client.put_admin_tenant_execution_config(tenant_id, configs[tenant_id])
            written.append(tenant_id)
    seed_response: dict[str, Any] | None = None
    if args.seed_tenants and not args.dry_run:
        payload: dict[str, Any] = {
            "source": "execution-configs",
            "status": args.status,
            "dry_run": False,
        }
        if args.plan is not None:
            payload["plan"] = args.plan
        if args.region is not None:
            payload["region"] = args.region
        seed_response = client.seed_admin_tenants(payload)
    output: dict[str, Any] = {
        "dry_run": args.dry_run,
        "tenant_count": len(configs),
        "valid": len(valid_tenant_ids),
        "invalid": len(invalid_tenant_ids),
        "written": written,
        "tenants": [
            {
                "tenant_id": tenant_id,
                "valid": _validation_ok(report),
                "written": tenant_id in written,
                "report": report,
            }
            for tenant_id, report in reports.items()
        ],
    }
    if seed_response is not None:
        output["seed"] = seed_response
    if trace_id is not None:
        output["trace_id"] = trace_id
    if args.json:
        print_json(output)
        return 0 if not invalid_tenant_ids else 1
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"tenant_count={len(configs)}",
                f"valid={len(valid_tenant_ids)}",
                f"invalid={len(invalid_tenant_ids)}",
                f"written={len(written)}",
                f"dry_run={args.dry_run}",
            ]
        )
    )
    for tenant in output["tenants"]:
        if not isinstance(tenant, dict):
            continue
        print(
            f"{tenant.get('tenant_id')} valid={tenant.get('valid')} written={tenant.get('written')}"
        )
    if seed_response is not None:
        print(
            "seed "
            + " ".join(
                [
                    f"created={seed_response.get('created')}",
                    f"existing={seed_response.get('existing')}",
                    f"conflicts={seed_response.get('conflicts')}",
                ]
            )
        )
    return 0 if not invalid_tenant_ids else 1


def run_admin_execution_config_export(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    tenant_ids = (
        [args.export_tenant_id]
        if args.export_tenant_id is not None
        else client.list_admin_execution_config_tenants()
    )
    configs: dict[str, Any] = {}
    for tenant_id in tenant_ids:
        response = client.get_admin_tenant_execution_config(tenant_id)
        config = response.get("config")
        if not isinstance(config, dict):
            raise RuntimeError(
                f"Minigent admin execution-config response for tenant '{tenant_id}' must include config"
            )
        configs[tenant_id] = config
    output: dict[str, Any] = configs
    if trace_id is not None and args.json:
        output = {"execution_configs": configs, "trace_id": trace_id}
    text = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        if not args.json:
            print(f"Wrote execution configs for {len(configs)} tenant(s) to {args.output}")
    else:
        print(text, end="")
    return 0


def _tenant_user_payload(args: argparse.Namespace, *, create: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if create:
        payload["user_id"] = args.user_id
    for key in ["email", "display_name", "role", "status"]:
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    metadata = _metadata_from_arg(args.metadata_json)
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def _format_tenant_user_line(user: dict[str, Any]) -> str:
    return " ".join(
        [
            str(user.get("id") or ""),
            f"tenant_id={user.get('tenant_id')}",
            f"user_id={user.get('user_id')}",
            f"email={user.get('email')}",
            f"display_name={user.get('display_name')}",
            f"role={user.get('role')}",
            f"status={user.get('status')}",
            f"updated_at={user.get('updated_at')}",
        ]
    )


def _print_admin_tenant_user_response(
    args: argparse.Namespace,
    response: dict[str, Any],
    trace_id: str | None,
) -> int:
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(_format_tenant_user_line(response))
    metadata = response.get("metadata")
    if isinstance(metadata, dict) and metadata:
        print("metadata=" + json.dumps(metadata, ensure_ascii=True, sort_keys=True))
    return 0


def run_admin_tenant_users(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    command = args.admin_tenant_users_command
    if command == "list":
        response = client.list_admin_tenant_users(
            args.tenant_id,
            limit=args.limit,
            offset=args.offset,
            status=args.status,
            role=args.role,
            email=args.email,
        )
        if args.json:
            output: dict[str, Any] = dict(response)
            if trace_id is not None:
                output["trace_id"] = trace_id
            print_json(output)
            return 0
        if trace_id is not None:
            print(f"trace_id={trace_id}")
        print(
            " ".join(
                [
                    f"tenant_id={response.get('tenant_id')}",
                    f"total={response.get('total')}",
                    f"limit={response.get('limit')}",
                    f"offset={response.get('offset')}",
                    f"next_offset={response.get('next_offset')}",
                ]
            )
        )
        for user in response.get("users", []):
            if isinstance(user, dict):
                print(_format_tenant_user_line(user))
        return 0
    if command == "create":
        response = client.create_admin_tenant_user(
            args.tenant_id,
            _tenant_user_payload(args, create=True),
        )
        return _print_admin_tenant_user_response(args, response, trace_id)
    if command == "show":
        response = client.get_admin_tenant_user(args.tenant_id, args.user_record_id)
        return _print_admin_tenant_user_response(args, response, trace_id)
    if command == "update":
        response = client.update_admin_tenant_user(
            args.tenant_id,
            args.user_record_id,
            _tenant_user_payload(args, create=False),
        )
        return _print_admin_tenant_user_response(args, response, trace_id)
    if command in {"activate", "suspend"}:
        response = client.transition_admin_tenant_user(
            args.tenant_id,
            args.user_record_id,
            command,
        )
        return _print_admin_tenant_user_response(args, response, trace_id)
    if command == "delete":
        response = client.delete_admin_tenant_user(args.tenant_id, args.user_record_id)
        if args.json:
            output = dict(response)
            if trace_id is not None:
                output["trace_id"] = trace_id
            print_json(output)
            return 0
        if trace_id is not None:
            print(f"trace_id={trace_id}")
        print(
            " ".join(
                [
                    "deleted=True",
                    f"tenant_id={response.get('tenant_id')}",
                    f"id={response.get('id')}",
                    f"status={response.get('status')}",
                ]
            )
        )
        return 0
    raise RuntimeError(f"Unhandled tenant users command: {command}")


def _entitlements_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "features": _json_object_from_arg(args.features_json, "--features-json") or {},
        "limits": _json_object_from_arg(args.limits_json, "--limits-json") or {},
    }


def run_admin_tenant_entitlements(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    command = args.admin_tenant_entitlements_command
    if command == "show":
        response = client.get_admin_tenant_entitlements(args.tenant_id)
    elif command == "set":
        response = client.put_admin_tenant_entitlements(args.tenant_id, _entitlements_payload(args))
    elif command == "validate":
        response = client.validate_admin_tenant_entitlements(
            args.tenant_id, _entitlements_payload(args)
        )
    elif command == "delete":
        client.delete_admin_tenant_entitlements(args.tenant_id)
        response = {"deleted": True, "tenant_id": args.tenant_id}
    else:  # pragma: no cover - argparse prevents this
        raise RuntimeError(f"Unhandled entitlements command: {command}")
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    if command == "delete":
        print(f"deleted tenant_id={args.tenant_id}")
        return 0
    print(
        " ".join(
            [
                f"tenant_id={response.get('tenant_id')}",
                f"version={response.get('version')}",
                f"updated_at={response.get('updated_at')}",
            ]
        )
    )
    if "valid" in response:
        print(f"valid={response.get('valid')}")
    features = response.get("features")
    limits = response.get("limits")
    if isinstance(features, dict):
        print("features=" + json.dumps(features, ensure_ascii=True, sort_keys=True))
    if isinstance(limits, dict):
        print("limits=" + json.dumps(limits, ensure_ascii=True, sort_keys=True))
    return 0


def _print_admin_tenant_response(
    args: argparse.Namespace,
    response: dict[str, Any],
    trace_id: str | None,
) -> int:
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(_format_tenant_line(response))
    metadata = response.get("metadata")
    if isinstance(metadata, dict) and metadata:
        print("metadata=" + json.dumps(metadata, ensure_ascii=True, sort_keys=True))
    return 0


def _format_tenant_line(tenant: dict[str, Any]) -> str:
    return " ".join(
        [
            str(tenant.get("id") or tenant.get("tenant_id") or ""),
            f"slug={tenant.get('slug')}",
            f"name={tenant.get('name')}",
            f"status={tenant.get('status')}",
            f"plan={tenant.get('plan')}",
            f"region={tenant.get('region')}",
            f"updated_at={tenant.get('updated_at')}",
        ]
    )


def run_admin_threads_list(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    response = client.list_admin_threads(
        args.admin_tenant_id,
        limit=args.limit,
        offset=args.offset,
        status=args.status,
        profile=args.profile,
        skill=args.skill,
        created_after=args.created_after,
        updated_after=args.updated_after,
    )
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"tenant_id={response.get('tenant_id')}",
                f"total={response.get('total')}",
                f"limit={response.get('limit')}",
                f"offset={response.get('offset')}",
                f"next_offset={response.get('next_offset')}",
            ]
        )
    )
    for thread in response.get("threads", []):
        if not isinstance(thread, dict):
            continue
        print(
            " ".join(
                [
                    str(thread.get("thread_id", "")),
                    f"status={thread.get('status')}",
                    f"messages={thread.get('message_count')}",
                    f"updated_at={thread.get('updated_at')}",
                ]
            )
        )
    return 0


def run_admin_threads_delete(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    response = client.delete_admin_thread(args.admin_tenant_id, args.thread_id)
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(f"deleted thread_id={response.get('thread_id')} tenant_id={response.get('tenant_id')}")
    return 0


def run_admin_threads_prune(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    response = client.prune_admin_threads(
        args.admin_tenant_id,
        updated_before=args.updated_before,
        status=args.status,
        profile=args.profile,
        skill=args.skill,
        dry_run=args.dry_run,
    )
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"tenant_id={response.get('tenant_id')}",
                f"deleted_count={response.get('deleted_count')}",
                f"dry_run={response.get('dry_run')}",
                f"candidate_count={len(response.get('candidate_thread_ids', []))}",
                f"updated_before={response.get('updated_before')}",
            ]
        )
    )
    return 0


def run_admin_audit_list(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    response = client.list_admin_audit_records(
        args.admin_tenant_id,
        limit=args.limit,
        offset=args.offset,
        action=args.action,
        actor=args.actor,
        created_after=args.created_after,
        created_before=args.created_before,
    )
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        " ".join(
            [
                f"tenant_id={response.get('tenant_id')}",
                f"limit={response.get('limit')}",
                f"offset={response.get('offset')}",
                f"total={response.get('total')}",
                f"next_offset={response.get('next_offset')}",
            ]
        )
    )
    for record in response.get("audit_records", []):
        if not isinstance(record, dict):
            continue
        print(
            " ".join(
                [
                    str(record.get("audit_id", "")),
                    f"action={record.get('action')}",
                    f"actor={record.get('actor_user_id')}",
                    f"affected_count={record.get('affected_count')}",
                    f"created_at={record.get('created_at')}",
                ]
            )
        )
    return 0


def run_admin_threads_show(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    response = client.get_admin_thread(args.admin_tenant_id, args.thread_id)
    if args.json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(f"thread_id={response.get('thread_id')}")
    print(f"tenant_id={response.get('tenant_id')}")
    print(f"status={response.get('status')}")
    print(f"message_count={response.get('message_count')}")
    print(f"created_at={response.get('created_at')}")
    print(f"updated_at={response.get('updated_at')}")
    skill_name = response.get("skill_name")
    if skill_name is not None:
        print(f"skill_name={skill_name}")
    skill_names = response.get("skill_names")
    if skill_names is not None:
        print(f"skill_names={skill_names}")
    capability_profile = response.get("capability_profile")
    if capability_profile is not None:
        print(f"capability_profile={capability_profile}")
    context = response.get("context")
    if isinstance(context, dict):
        print(
            "context="
            f"summarized_message_count={context.get('summarized_message_count')} "
            f"updated_at={context.get('updated_at')}"
        )
        summary = context.get("summary")
        if isinstance(summary, str) and summary:
            print(f"summary={summary}")
    messages = response.get("messages", [])
    if isinstance(messages, list) and messages:
        print("messages:")
        for message in messages:
            if isinstance(message, dict):
                print(format_message(message))
    return 0


def run_health(client: MinigentAPIClient, as_json: bool, trace_id: str | None) -> int:
    response = client.health()
    if as_json:
        output: dict[str, Any] = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(response["status"])
    return 0


def run_execution_options(
    client: MinigentAPIClient,
    trace_id: str | None,
    *,
    section: str | None = None,
    as_json: bool = False,
) -> int:
    response = client.execution_options()
    if as_json:
        output = response if section is None else response.get(section, {})
        if trace_id is not None and isinstance(output, dict):
            output = {**output, "trace_id": trace_id}
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(_format_execution_options(response, section=section), end="")
    return 0


def _format_execution_options(response: dict[str, Any], *, section: str | None = None) -> str:
    sections: list[str] = []
    if section in {None, "skills"}:
        sections.append(_format_execution_option_section("Skills", response.get("skills")))
    if section in {None, "capability_profiles"}:
        sections.append(
            _format_execution_option_section(
                "Capability profiles", response.get("capability_profiles")
            )
        )
    return "\n".join(part for part in sections if part) + ("\n" if sections else "")


def _format_execution_option_section(title: str, payload: object) -> str:
    if not isinstance(payload, dict):
        return f"{title}:\n  none reported\n"
    default = payload.get("default")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return f"{title}:\n  none configured\n"
    lines = [f"{title}:"]
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = item.get("description")
        suffix_parts: list[str] = []
        if name == default:
            suffix_parts.append("default")
        if isinstance(description, str) and description:
            suffix_parts.append(description)
        suffix = " — " + " · ".join(suffix_parts) if suffix_parts else ""
        lines.append(f"  {name}{suffix}")
    if len(lines) == 1:
        lines.append("  none configured")
    return "\n".join(lines) + "\n"


def run_ping(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    checks, config_response = collect_connection_checks(args, client)
    success = not any(check.blocking for check in checks)
    if args.json:
        output: dict[str, object] = {
            "ok": success,
            "checks": [check.to_dict() for check in checks],
        }
        if config_response is not None:
            output["server"] = server_summary(config_response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0 if success else 1
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    for check in checks:
        print(format_check(check))
    if config_response is not None:
        summary = server_summary(config_response)
        backend = summary.get("backend")
        model = summary.get("model")
        if backend:
            print(f"✓ Backend mode: {backend}")
        if model:
            print(f"✓ Default model configured: {model}")
    return 0 if success else 1


def _client_config_summary(args: argparse.Namespace, config: ClientConfig) -> dict[str, object]:
    principal = config.principal
    return {
        "base_url": config.base_url,
        "auth": {
            "mode": "bearer_token" if principal.api_token else "trusted_headers",
            "api_token": mask_value(principal.api_token) if principal.api_token else None,
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "admin": principal.is_admin,
        },
        "flags": {
            "json": args.json,
            "verbose": args.verbose,
            "trace": args.trace,
        },
        "environment": mask_secrets(
            {
                name: os.environ.get(name)
                for name in (
                    "MINIGENT_BASE_URL",
                    "MINIGENT_API_TOKEN",
                    "MINIGENT_VOICE_API_TOKEN",
                    "MINIGENT_VOICE_USER_ID",
                    "MINIGENT_VOICE_TENANT_ID",
                    "MINIGENT_CLIENT_STREAM_RUNS",
                    "MINIGENT_CLIENT_SHOW_TOOL_RESULTS",
                )
                if os.environ.get(name) is not None
            }
        ),
    }


def collect_debug_bundle(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    config: ClientConfig,
    trace_id: str | None,
) -> tuple[dict[str, object], bool]:
    checks, config_response = collect_connection_checks(args, client)
    success = not any(check.blocking for check in checks)
    threads = list_remembered_threads(config.base_url, args)
    server_config = mask_secrets(config_response) if config_response is not None else None
    server_summary_payload = server_summary(config_response) if config_response is not None else {}
    agent_backend = (
        config_response.get("agent_backend") if isinstance(config_response, dict) else None
    )
    mcp_servers = config_response.get("mcp_servers") if isinstance(config_response, dict) else None
    bundle: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": {
            "minigent": package_version(),
            "python": platform.python_version(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "client": _client_config_summary(args, config),
        "checks": [check.to_dict() for check in checks],
        "server": {
            "summary": server_summary_payload,
            "config": server_config,
        },
        "mcp": {
            "broker_enabled": (
                agent_backend.get("mcp_broker_enabled") if isinstance(agent_backend, dict) else None
            ),
            "server_count": len(mcp_servers) if isinstance(mcp_servers, list) else None,
        },
        "threads": {
            "last_thread_id": ClientState.load().get_last_thread(
                state_scope_key(config.base_url, args)
            ),
            "recent": [item.to_dict() for item in threads[:10]],
        },
        "recent_events": "not collected by the local CLI; rerun the failing command with --verbose or --stream",
    }
    if trace_id is not None:
        bundle["trace_id"] = trace_id
    return bundle, success


def _debug_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _format_debug_bundle(bundle: dict[str, object]) -> str:
    lines = ["Minigent debug bundle", ""]
    version_info = _debug_dict(bundle.get("version"))
    platform_info = _debug_dict(bundle.get("platform"))
    client_info = _debug_dict(bundle.get("client"))
    server_info = _debug_dict(bundle.get("server"))
    mcp_info = _debug_dict(bundle.get("mcp"))
    threads_info = _debug_dict(bundle.get("threads"))

    lines.append(f"generated_at: {bundle.get('generated_at')}")
    lines.append(f"minigent: {version_info.get('minigent', 'unknown')}")
    lines.append(f"python: {version_info.get('python', 'unknown')}")
    lines.append(
        "platform: "
        f"{platform_info.get('system', 'unknown')} {platform_info.get('release', '')} "
        f"{platform_info.get('machine', '')}".strip()
    )
    lines.append("")
    lines.append("Client")
    lines.append(f"base_url: {client_info.get('base_url')}")
    auth = _debug_dict(client_info.get("auth"))
    lines.append(
        "auth: "
        f"mode={auth.get('mode')} user={auth.get('user_id')} tenant={auth.get('tenant_id')} "
        f"admin={auth.get('admin')} token={auth.get('api_token') or '<not set>'}"
    )
    lines.append("")
    lines.append("Checks")
    checks = bundle.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict):
                lines.append(format_check(DiagnosticCheck(**check)))
    lines.append("")
    summary = _debug_dict(server_info.get("summary"))
    lines.append("Server")
    lines.append(f"backend: {summary.get('backend', 'unknown')}")
    lines.append(f"provider: {summary.get('provider', 'unknown')}")
    lines.append(f"model: {summary.get('model', 'unknown')}")
    lines.append("")
    lines.append("MCP")
    lines.append(f"broker_enabled: {mcp_info.get('broker_enabled')}")
    lines.append(f"server_count: {mcp_info.get('server_count')}")
    lines.append("")
    lines.append("Threads")
    lines.append(f"last_thread_id: {threads_info.get('last_thread_id')}")
    recent = threads_info.get("recent")
    if isinstance(recent, list) and recent:
        for item in recent:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('thread_id')}  {item.get('title') or 'Untitled thread'}  "
                    f"{item.get('updated_at') or 'unknown'}  messages={item.get('message_count')}"
                )
    else:
        lines.append("- none")
    lines.append("")
    lines.append(f"recent_events: {bundle.get('recent_events')}")
    return "\n".join(lines) + "\n"


def run_debug_bundle(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    config: ClientConfig,
    trace_id: str | None,
) -> int:
    bundle, success = collect_debug_bundle(args, client, config, trace_id)
    text = (
        json.dumps(bundle, indent=2, sort_keys=True) + "\n"
        if args.json
        else _format_debug_bundle(bundle)
    )
    output_path = getattr(args, "output", None)
    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
        if not args.json:
            print(f"Wrote debug bundle to {output_path}")
    else:
        print(text, end="")
    return 0 if success else 1


def _abort_detail(args: argparse.Namespace) -> tuple[str, bool]:
    server_cancelled = bool(getattr(args, "stream", False))
    if server_cancelled:
        return "server cancellation requested", server_cancelled
    return "server cancellation unavailable for non-streaming runs", server_cancelled


def _print_abort_message(args: argparse.Namespace) -> None:
    detail, server_cancelled = _abort_detail(args)
    if getattr(args, "json", False):
        print_json(
            {
                "error": {
                    "message": "Run aborted locally.",
                    "category": "aborted",
                    "server_cancelled": server_cancelled,
                    "detail": detail,
                }
            }
        )
        return
    print(f"[idle] locally aborted current run; {detail}.", file=sys.stderr)


def _apply_cli_env_file(args: argparse.Namespace) -> None:
    env_file = getattr(args, "env_file", None)
    if not env_file:
        return
    path = Path(env_file).expanduser()
    os.environ["MINIGENT_DOTENV_FILE"] = str(path)
    if not path.exists():
        return
    from dotenv import dotenv_values

    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)
    if args.base_url == "http://127.0.0.1:8000" and os.environ.get("MINIGENT_BASE_URL"):
        args.base_url = os.environ["MINIGENT_BASE_URL"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _apply_cli_env_file(args)

    trace_id = secrets.token_hex(16) if args.trace else None
    if args.command == "run":
        args.message = _read_run_message(args)
    config = build_config(args, trace_id)
    base_url = config.base_url
    client = build_client(args, trace_id)

    try:
        if args.command in {"chat", "run"}:
            return run_chat(args, client, base_url, trace_id)
        if args.command == "resume":
            return run_resume(args, client, base_url, trace_id)
        if args.command == "export":
            return run_export(args, client, base_url, trace_id)
        if args.command == "threads":
            if args.threads_command in {None, "list"}:
                return run_threads_list(args, base_url, trace_id)
            if args.threads_command == "create":
                return run_threads_create(args, client, base_url, trace_id)
            if args.threads_command == "show":
                return run_threads_show(args, client, trace_id)
            if args.threads_command == "delete":
                return run_threads_delete(args, client, base_url, trace_id)
        if args.command == "admin":
            if args.admin_command == "tenants":
                if args.admin_tenants_command == "list":
                    return run_admin_tenants_list(args, client, trace_id)
                if args.admin_tenants_command == "create":
                    return run_admin_tenants_create(args, client, trace_id)
                if args.admin_tenants_command == "show":
                    return run_admin_tenants_show(args, client, trace_id)
                if args.admin_tenants_command == "update":
                    return run_admin_tenants_update(args, client, trace_id)
                if args.admin_tenants_command in {"activate", "suspend", "archive", "delete"}:
                    return run_admin_tenants_transition(args, client, trace_id)
                if args.admin_tenants_command == "seed":
                    return run_admin_tenants_seed(args, client, trace_id)
                if args.admin_tenants_command == "users":
                    return run_admin_tenant_users(args, client, trace_id)
                if args.admin_tenants_command == "entitlements":
                    return run_admin_tenant_entitlements(args, client, trace_id)
            if args.admin_command == "execution-config":
                if args.admin_execution_config_command == "import":
                    return run_admin_execution_config_import(args, client, trace_id)
                if args.admin_execution_config_command == "export":
                    return run_admin_execution_config_export(args, client, trace_id)
                if args.admin_execution_config_command == "validate-file":
                    return run_admin_execution_config_validate_file(args, client, trace_id)
            if args.admin_command == "threads":
                if args.admin_threads_command == "list":
                    return run_admin_threads_list(args, client, trace_id)
                if args.admin_threads_command == "show":
                    return run_admin_threads_show(args, client, trace_id)
                if args.admin_threads_command == "delete":
                    return run_admin_threads_delete(args, client, trace_id)
                if args.admin_threads_command == "prune":
                    return run_admin_threads_prune(args, client, trace_id)
            if args.admin_command == "audit":
                if args.admin_audit_command == "list":
                    return run_admin_audit_list(args, client, trace_id)
        if args.command == "health":
            return run_health(client, args.json, trace_id)
        if args.command == "ping":
            return run_ping(args, client, trace_id)
        if args.command == "options":
            return run_execution_options(client, trace_id, as_json=args.json)
        if args.command == "skills":
            return run_execution_options(client, trace_id, section="skills", as_json=args.json)
        if args.command == "capabilities":
            return run_execution_options(
                client,
                trace_id,
                section="capability_profiles",
                as_json=args.json,
            )
        if args.command == "debug-bundle":
            return run_debug_bundle(args, client, config, trace_id)
        if args.command == "config":
            if args.config_command in {None, "show"}:
                return run_config(client, trace_id)
            if args.config_command == "init":
                return run_config_init(args)
            if args.config_command == "print":
                return run_config_print(args)
            if args.config_command == "export":
                return run_config_export(args, client, trace_id)
            if args.config_command == "doctor":
                return run_config_doctor(args, client, trace_id)
    except KeyboardInterrupt:
        try:
            client.cancel_current_run()
        except Exception:
            pass
        _print_abort_message(args)
        return 130
    except MinigentAPIError as exc:
        if args.json:
            print_json({"error": exc.to_dict(include_detail=args.verbose)})
        else:
            print(f"Error: {exc.message}", file=sys.stderr)
            if args.verbose and exc.detail:
                print(f"Detail: {exc.detail}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unhandled command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
