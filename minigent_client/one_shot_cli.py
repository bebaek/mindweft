from __future__ import annotations

import argparse
import os
import secrets
import sys
import urllib.parse
import urllib.request  # noqa: F401 - exposed for existing CLI tests that monkeypatch urlopen.
from pathlib import Path
from typing import Sequence

from minigent_client.admin_commands import (
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
    run_admin_threads_list,
    run_admin_threads_prune,
    run_admin_threads_show,
)
from minigent_client.chat_commands import (  # noqa: F401 - preserve helper import surface.
    _format_markdown_transcript,
    _read_run_message,
    build_client,
    build_config,
    build_trace_headers,
    ensure_thread,
    forget_thread,
    list_remembered_threads,
    load_remembered_thread,
    pick_thread_from_history,
    remember_thread,
    run_chat,
    run_export,
    run_resume,
    run_threads_create,
    run_threads_delete,
    run_threads_list,
    run_threads_show,
    state_scope_key,
    validate_thread_create_options,
)
from minigent_client.config_commands import (
    run_config,
    run_config_doctor,
    run_config_export,
    run_config_init,
    run_config_print,
)
from minigent_client.diagnostic_commands import (  # noqa: F401 - preserve helper surface.
    _client_config_summary,
    _debug_dict,
    _format_debug_bundle,
    _format_execution_agent_section,
    _format_execution_option_section,
    _format_execution_options,
    collect_debug_bundle,
    run_debug_bundle,
    run_execution_options,
    run_health,
    run_ping,
)
from minigent_client.errors import MinigentAPIError
from minigent_client.one_shot_parser import build_parser
from minigent_client.output import (
    print_json,
)


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
