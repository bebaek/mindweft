from __future__ import annotations

import argparse
import secrets
import sys
import urllib.parse
import urllib.request  # noqa: F401 - exposed for existing CLI tests that monkeypatch urlopen.
from dataclasses import dataclass
from typing import Any, Sequence

from minigent_client.api_client import MinigentAPIClient
from minigent_client.config import ClientConfig, build_client_config
from minigent_client.errors import MinigentAPIError
from minigent_client.output import StreamProgressRenderer, format_message, print_json
from minigent_client.state import ClientState
from minigent_client.state import state_scope_key as build_state_scope_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Command-line client for a running Minigent API.")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL for the running API service.",
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

    chat_parser = subparsers.add_parser("chat", help="Send a user message and print the assistant reply.")
    chat_parser.add_argument("message", help="User message content to send.")
    chat_target_group = chat_parser.add_mutually_exclusive_group()
    chat_target_group.add_argument("--thread", default=None, help="Existing thread ID to continue.")
    chat_target_group.add_argument(
        "--resume-last",
        action="store_true",
        help="Resume the last locally remembered thread for this server and principal.",
    )
    chat_parser.add_argument("--skill", default=None, help="Skill to apply when creating a new thread.")
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

    threads_parser = subparsers.add_parser("threads", help="Manage conversation threads.")
    threads_subparsers = threads_parser.add_subparsers(dest="threads_command", required=True)

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

    config_parser = subparsers.add_parser("config", help="Show or inspect resolved API configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("show", help="Show resolved API configuration as JSON.")
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


def remember_thread(base_url: str, args: argparse.Namespace, thread_id: str) -> None:
    state = ClientState.load()
    state.set_last_thread(state_scope_key(base_url, args), thread_id)
    state.save()


def load_remembered_thread(base_url: str, args: argparse.Namespace) -> str:
    state = ClientState.load()
    thread_id = state.get_last_thread(state_scope_key(base_url, args))
    if thread_id is None:
        raise SystemExit("No remembered thread for this server and principal. Start a chat first.")
    return thread_id


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


def build_client(args: argparse.Namespace, trace_id: str | None) -> MinigentAPIClient:
    return MinigentAPIClient(
        build_config(args, trace_id),
        progress_stream=sys.stderr,
        progress_verbose=args.verbose,
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


def _make_stream_progress_printer(*, verbose: bool = False) -> Any:
    renderer = StreamProgressRenderer(sys.stderr, verbose=verbose)
    return renderer.render


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


def run_chat(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    thread_id, created_thread = ensure_thread(args, client, base_url)
    client.add_message(thread_id, args.message)
    events: list[dict[str, Any]] | None = None
    if args.stream and args.json:
        events = list(
            client.request_ndjson_events("POST", f"{base_url}/threads/{thread_id}/run/stream")
        )
        reply = _reply_from_run_stream(events)
    else:
        reply = client.run_thread(thread_id, stream=args.stream)
    remember_thread(base_url, args, thread_id)

    if args.json:
        output: dict[str, Any] = {
            "thread_id": thread_id,
            "created_thread": created_thread,
            "reply": reply,
        }
        if events is not None:
            output["events"] = events
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
    print(reply)
    if args.transcript:
        print("")
        for message in client.get_thread(thread_id)["messages"]:
            print(format_message(message))
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
    remember_thread(base_url, args, thread_id)
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


def run_health(
    client: MinigentAPIClient, as_json: bool, trace_id: str | None
) -> int:
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


@dataclass(frozen=True)
class DiagnosticCheck:
    status: str
    label: str
    detail: str | None = None
    blocking: bool = False

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "label": self.label,
            "blocking": self.blocking,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def run_config(client: MinigentAPIClient, trace_id: str | None) -> int:
    response = client.config()
    if trace_id is not None:
        response = {**response, "trace_id": trace_id}
    print_json(response)
    return 0


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
            output["server"] = _server_summary(config_response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0 if success else 1
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    for check in checks:
        print(_format_check(check))
    if config_response is not None:
        summary = _server_summary(config_response)
        backend = summary.get("backend")
        model = summary.get("model")
        if backend:
            print(f"✓ Backend mode: {backend}")
        if model:
            print(f"✓ Default model configured: {model}")
    return 0 if success else 1


def run_config_doctor(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    checks: list[DiagnosticCheck] = [*_local_config_checks(args)]
    config_response: dict[str, Any] | None = None
    if not any(check.blocking for check in checks):
        connection_checks, config_response = collect_connection_checks(args, client)
        checks.extend(connection_checks)
    checks.extend(_server_config_checks(config_response))
    success = not any(check.blocking for check in checks)

    if args.json:
        output: dict[str, object] = {
            "ok": success,
            "checks": [check.to_dict() for check in checks],
        }
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0 if success else 1

    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print("Minigent config doctor")
    print("")
    for check in checks:
        print(_format_check(check))
    print("")
    if success:
        print("No blocking issues found.")
    else:
        print("Blocking issues found. Re-run with --verbose for technical details.")
    return 0 if success else 1


def collect_connection_checks(
    args: argparse.Namespace,
    client: MinigentAPIClient,
) -> tuple[list[DiagnosticCheck], dict[str, Any] | None]:
    checks: list[DiagnosticCheck] = []
    config_response: dict[str, Any] | None = None
    try:
        health_response = client.health()
    except MinigentAPIError as exc:
        checks.append(
            DiagnosticCheck(
                "error",
                "API reachable",
                _diagnostic_detail(exc, verbose=args.verbose),
                blocking=True,
            )
        )
        return checks, None
    status = health_response.get("status")
    detail = str(status) if status is not None else None
    checks.append(DiagnosticCheck("ok", "API reachable", detail))

    try:
        config_response = client.config()
    except MinigentAPIError as exc:
        checks.append(
            DiagnosticCheck(
                "error",
                "Server config readable",
                _diagnostic_detail(exc, verbose=args.verbose),
                blocking=True,
            )
        )
        return checks, None
    checks.append(DiagnosticCheck("ok", "Server config readable"))
    return checks, config_response


def _local_config_checks(args: argparse.Namespace) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    parsed = urllib.parse.urlparse(args.base_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        checks.append(DiagnosticCheck("ok", "Base URL configured", args.base_url.rstrip("/")))
    else:
        checks.append(
            DiagnosticCheck(
                "error",
                "Base URL configured",
                "Use an http:// or https:// URL.",
                blocking=True,
            )
        )
    if args.api_token:
        checks.append(DiagnosticCheck("ok", "API token present"))
    else:
        checks.append(
            DiagnosticCheck(
                "ok",
                "Trusted principal headers configured",
                f"user={args.user_id} tenant={args.tenant_id}",
            )
        )
    return checks


def _server_config_checks(config_response: dict[str, Any] | None) -> list[DiagnosticCheck]:
    if config_response is None:
        return []
    checks: list[DiagnosticCheck] = []
    summary = _server_summary(config_response)
    backend = summary.get("backend")
    if backend:
        checks.append(DiagnosticCheck("ok", "Backend mode", backend))
    else:
        checks.append(DiagnosticCheck("warning", "Backend mode", "not reported"))
    model = summary.get("model")
    if model:
        checks.append(DiagnosticCheck("ok", "Default model configured", model))
    else:
        checks.append(DiagnosticCheck("warning", "Default model configured", "not reported"))
    mcp_servers = config_response.get("mcp_servers")
    if isinstance(mcp_servers, list) and mcp_servers:
        checks.append(DiagnosticCheck("ok", "MCP servers configured", str(len(mcp_servers))))
    else:
        checks.append(DiagnosticCheck("warning", "MCP servers configured", "none reported"))
    agent_backend = config_response.get("agent_backend")
    mcp_broker_enabled = (
        agent_backend.get("mcp_broker_enabled") if isinstance(agent_backend, dict) else None
    )
    if mcp_broker_enabled is True:
        checks.append(DiagnosticCheck("ok", "MCP broker enabled"))
    else:
        checks.append(DiagnosticCheck("warning", "MCP broker enabled", "false or not reported"))
    quality = config_response.get("quality")
    quality_enabled = quality.get("enabled") if isinstance(quality, dict) else None
    checks.append(
        DiagnosticCheck(
            "ok" if quality_enabled is True else "warning",
            "Remote quality enhancement",
            "enabled" if quality_enabled is True else "disabled or not reported",
        )
    )
    return checks


def _server_summary(config_response: dict[str, Any]) -> dict[str, str]:
    summary: dict[str, str] = {}
    agent_backend = config_response.get("agent_backend")
    if isinstance(agent_backend, dict):
        backend = agent_backend.get("type")
        if isinstance(backend, str) and backend:
            summary["backend"] = backend
    llm = config_response.get("llm")
    if isinstance(llm, dict):
        model = llm.get("model")
        provider = llm.get("provider")
        if isinstance(model, str) and model:
            summary["model"] = model
        if isinstance(provider, str) and provider:
            summary["provider"] = provider
    return summary


def _diagnostic_detail(exc: MinigentAPIError, *, verbose: bool) -> str:
    if verbose and exc.detail:
        return f"{exc.message} Detail: {exc.detail}"
    return exc.message


def _format_check(check: DiagnosticCheck) -> str:
    marker = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(check.status, "•")
    line = f"{marker} {check.label}"
    if check.detail:
        line = f"{line}: {check.detail}"
    return line


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    trace_id = secrets.token_hex(16) if args.trace else None
    config = build_config(args, trace_id)
    base_url = config.base_url
    client = MinigentAPIClient(config, progress_stream=sys.stderr, progress_verbose=args.verbose)

    try:
        if args.command == "chat":
            return run_chat(args, client, base_url, trace_id)
        if args.command == "threads":
            if args.threads_command == "create":
                return run_threads_create(args, client, base_url, trace_id)
            if args.threads_command == "show":
                return run_threads_show(args, client, trace_id)
            if args.threads_command == "delete":
                return run_threads_delete(args, client, base_url, trace_id)
        if args.command == "admin":
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
        if args.command == "config":
            if args.config_command in {None, "show"}:
                return run_config(client, trace_id)
            if args.config_command == "doctor":
                return run_config_doctor(args, client, trace_id)
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
