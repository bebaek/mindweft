from __future__ import annotations

import argparse

from mindweft_client.admin_commands import (
    run_admin_audit_list,
    run_admin_execution_config_export,
    run_admin_execution_config_import,
    run_admin_execution_config_validate_file,
    run_admin_tenant_entitlements,
    run_admin_tenant_users,
    run_admin_tenants_create,
    run_admin_tenants_list,
    run_admin_tenants_seed,
    run_admin_tenants_show,
    run_admin_tenants_transition,
    run_admin_tenants_update,
    run_admin_threads_delete,
    run_admin_threads_imported_lineage,
    run_admin_threads_list,
    run_admin_threads_prune,
    run_admin_threads_show,
)
from mindweft_client.api_client import MindweftAPIClient
from mindweft_client.chat_commands import (
    run_chat,
    run_export,
    run_import_thread_archive,
    run_resume,
    run_threads_create,
    run_threads_delete,
    run_threads_imported_lineage,
    run_threads_list,
    run_threads_organization,
    run_threads_retitle,
    run_threads_search,
    run_threads_show,
)
from mindweft_client.config import ClientConfig
from mindweft_client.config_commands import (
    run_config,
    run_config_export,
    run_config_init,
    run_config_print,
)
from mindweft_client.config_diagnostics import run_config_doctor
from mindweft_client.diagnostic_commands import (
    run_debug_bundle,
    run_execution_options,
    run_health,
    run_ping,
)


def dispatch_command(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    config: ClientConfig,
    trace_id: str | None,
) -> int | None:
    """Route parsed one-shot CLI arguments to their command handler."""
    base_url = config.base_url
    if args.command in {"chat", "run"}:
        return run_chat(args, client, base_url, trace_id)
    if args.command == "resume":
        return run_resume(args, client, base_url, trace_id)
    if args.command == "export":
        return run_export(args, client, base_url, trace_id)
    if args.command == "import":
        return run_import_thread_archive(args, client, base_url, trace_id)
    if args.command == "threads":
        if args.threads_command in {None, "list"}:
            return run_threads_list(args, client, base_url, trace_id)
        if args.threads_command == "create":
            return run_threads_create(args, client, base_url, trace_id)
        if args.threads_command == "retitle":
            return run_threads_retitle(args, client, trace_id)
        if args.threads_command == "search":
            return run_threads_search(args, client, trace_id)
        if args.threads_command == "show":
            return run_threads_show(args, client, trace_id)
        if args.threads_command in {"pin", "unpin", "archive", "restore"}:
            return run_threads_organization(args, client, trace_id)
        if args.threads_command == "imported-lineage":
            return run_threads_imported_lineage(args, client, trace_id)
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
            if args.admin_threads_command == "imported-lineage":
                return run_admin_threads_imported_lineage(args, client, trace_id)
            if args.admin_threads_command == "delete":
                return run_admin_threads_delete(args, client, trace_id)
            if args.admin_threads_command == "prune":
                return run_admin_threads_prune(args, client, trace_id)
        if args.admin_command == "audit" and args.admin_audit_command == "list":
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
    return None
