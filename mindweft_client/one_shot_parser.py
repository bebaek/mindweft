from __future__ import annotations

import argparse

from mindweft_client.config_commands import CONFIG_INIT_PROFILES, DEFAULT_CONFIG_PROFILE
from mindweft_config.unified_config import DEFAULT_CODING_DOTENV_FILE, MINDWEFT_CONFIG_FILE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Command-line client for a running Mindweft API.")
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
        "--audio",
        action="append",
        default=[],
        help="PCM WAV audio file to attach to the message. Can be specified multiple times.",
    )
    chat_parser.add_argument(
        "--document",
        action="append",
        default=[],
        help="PDF or UTF-8 text document to attach to the message. Can be specified multiple times.",
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
        "--llm",
        dest="llm_profile",
        default=None,
        help="Named LLM profile to bind to a new thread.",
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
        "--audio",
        action="append",
        default=[],
        help="PCM WAV audio file to attach to the prompt. Can be specified multiple times.",
    )
    run_parser.add_argument(
        "--document",
        action="append",
        default=[],
        help="PDF or UTF-8 text document to attach to the prompt. Can be specified multiple times.",
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
        "--llm",
        dest="llm_profile",
        default=None,
        help="Named LLM profile to bind to the thread.",
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

    export_parser = subparsers.add_parser(
        "export",
        help="Export a thread transcript.",
        description=(
            "Export a readable transcript or versioned thread archive. Transcript formats are not "
            "portable archives and may contain private values rendered for the authenticated user."
        ),
    )
    export_parser.add_argument(
        "thread_id",
        nargs="?",
        default=None,
        help="Thread ID to export. Defaults to the latest locally remembered thread.",
    )
    export_parser.add_argument(
        "--format",
        choices=["markdown", "json", "archive"],
        default="markdown",
        help=(
            "Output format. Markdown and JSON are readable transcripts; archive is a versioned "
            "portable JSON document."
        ),
    )
    export_parser.add_argument(
        "--output",
        default=None,
        help="Write the export to this file instead of stdout.",
    )

    import_parser = subparsers.add_parser(
        "import",
        help="Import a versioned thread archive.",
        description=(
            "Import a Mindweft thread archive as a new thread. Source execution options, "
            "organization state, and thread timestamps are mapped through independent destination "
            "policies."
        ),
    )
    import_parser.add_argument("archive", help="Path to a Mindweft thread archive JSON file.")
    import_parser.add_argument(
        "--profile-policy",
        choices=["available", "defaults", "strict"],
        default="available",
        help=(
            "How to map source skills, capability profile, and LLM profile. The default restores "
            "available selections and substitutes destination defaults for missing selections."
        ),
    )

    import_parser.add_argument(
        "--organization-policy",
        choices=("reset", "preserve"),
        default="reset",
        help=(
            "How to map source pin and archive state. The default resets destination organization; "
            "preserve restores state recorded by version 3 archives."
        ),
    )

    import_parser.add_argument(
        "--timestamp-policy",
        choices=("reset", "preserve"),
        default="reset",
        help=(
            "How to map source thread created/updated timestamps. The default uses destination "
            "timestamps; preserve restores the source values."
        ),
    )

    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run full destination validation, including attachment checks, then remove all imported "
            "state instead of creating a thread."
        ),
    )

    threads_parser = subparsers.add_parser("threads", help="Manage conversation threads.")
    threads_subparsers = threads_parser.add_subparsers(dest="threads_command")
    threads_list_parser = threads_subparsers.add_parser(
        "list", help="List server-side conversation threads."
    )
    threads_list_parser.add_argument("--search", default=None, help="Search thread titles.")
    threads_list_parser.add_argument(
        "--archived", action="store_true", help="List archived threads instead of active threads."
    )
    threads_list_parser.add_argument(
        "--pinned", action="store_true", help="List only pinned threads."
    )
    threads_search_parser = threads_subparsers.add_parser(
        "search", help="Search conversation titles and messages."
    )
    threads_search_parser.add_argument("query", help="Search query.")
    threads_search_parser.add_argument(
        "--scope", choices=["title", "messages", "all"], default="all"
    )
    threads_search_parser.add_argument(
        "--archived", action="store_true", help="Search archived threads instead of active threads."
    )

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
    threads_create_parser.add_argument(
        "--llm",
        dest="llm_profile",
        default=None,
        help="Named LLM profile to bind to the thread.",
    )

    threads_retitle_parser = threads_subparsers.add_parser(
        "retitle", help="Generate semantic titles for existing threads."
    )
    threads_retitle_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List eligible threads without making LLM requests.",
    )
    threads_retitle_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum threads to inspect (default: 50).",
    )
    threads_retitle_parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Maximum concurrent title requests (default: 2, max: 8).",
    )

    threads_show_parser = threads_subparsers.add_parser("show", help="Show thread messages.")
    threads_show_parser.add_argument("thread_id", help="Thread ID to display.")

    for command_name in ["pin", "unpin", "archive", "restore"]:
        organization_parser = threads_subparsers.add_parser(
            command_name, help=f"{command_name.title()} a thread."
        )
        organization_parser.add_argument("thread_id", help="Thread ID to update.")

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
        "--provisioning-profile",
        choices=["none", "generic-v1"],
        default=None,
        help="Optionally create a starter execution configuration with the tenant.",
    )
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
    admin_tenants_seed_parser.add_argument(
        "--conflict-policy",
        choices=["suffix", "skip", "fail"],
        default="suffix",
        help="How to handle a derived slug that is already in use.",
    )
    admin_tenants_seed_parser.add_argument(
        "--tenant",
        dest="seed_tenants",
        action="append",
        default=None,
        help="Restrict seeding to this tenant ID; repeat for multiple IDs.",
    )
    admin_tenants_seed_parser.add_argument(
        "--slug-override",
        dest="seed_slug_overrides",
        action="append",
        default=None,
        metavar="TENANT_ID=SLUG",
        help="Override a derived slug; repeat for multiple tenant IDs.",
    )

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
        "init", help=f"Create a starter {MINDWEFT_CONFIG_FILE} in the current directory."
    )
    config_init_parser.add_argument(
        "--output",
        default=MINDWEFT_CONFIG_FILE,
        help=f"Path to write. Defaults to ./{MINDWEFT_CONFIG_FILE}.",
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
        help=(
            "Dotenv file for --local-coding. Defaults to --env-file or "
            f"{DEFAULT_CODING_DOTENV_FILE}."
        ),
    )
    config_export_parser.add_argument(
        "--no-coding-env-file",
        action="store_true",
        help="With --local-coding, do not load a coding runner dotenv file.",
    )
    config_subparsers.add_parser("doctor", help="Check common CLI/API configuration issues.")

    return parser
