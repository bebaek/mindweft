from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import sys
import urllib.parse
import urllib.request  # noqa: F401 - exposed for existing CLI tests that monkeypatch urlopen.
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Sequence

from minigent_client.api_client import MinigentAPIClient
from minigent_client.config import ClientConfig, build_client_config
from minigent_client.errors import MinigentAPIError
from minigent_client.output import (
    StreamProgressRenderer,
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
    if getattr(args, "thread_picker", False) and len(threads) > 1 and sys.stdin.isatty() and sys.stdout.isatty():
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
    session = PromptSession(completer=WordCompleter([item.thread_id for item in threads], ignore_case=True))
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
    return MinigentAPIClient(
        build_config(args, trace_id),
        progress_stream=_QuietProgressStream() if getattr(args, "quiet", False) else sys.stderr,
        progress_verbose=args.verbose and not getattr(args, "quiet", False),
        show_tool_results=getattr(args, "show_tool_results", False)
        and not getattr(args, "quiet", False),
        show_reasoning=getattr(args, "show_reasoning", False)
        and not getattr(args, "quiet", False),
        token_mode="off" if getattr(args, "quiet", False) else getattr(args, "tokens", "auto"),
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
        token_mode=token_mode,
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


def run_chat(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    thread_id, created_thread = ensure_thread(args, client, base_url)
    client.add_message(thread_id, args.message)
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
    if getattr(args, 'show_reasoning', False) and not getattr(args, 'stream', False):
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


_SECRET_KEY_PARTS = ("token", "secret", "key", "authorization", "password")


def _mask_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        return "<set>" if value else ""
    return "<set>"


def _mask_secrets(value: object) -> object:
    if isinstance(value, dict):
        masked: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.casefold() for part in _SECRET_KEY_PARTS):
                masked[key_text] = _mask_value(item)
            else:
                masked[key_text] = _mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value


def _client_config_summary(args: argparse.Namespace, config: ClientConfig) -> dict[str, object]:
    principal = config.principal
    return {
        "base_url": config.base_url,
        "auth": {
            "mode": "bearer_token" if principal.api_token else "trusted_headers",
            "api_token": _mask_value(principal.api_token) if principal.api_token else None,
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "admin": principal.is_admin,
        },
        "flags": {
            "json": args.json,
            "verbose": args.verbose,
            "trace": args.trace,
        },
        "environment": _mask_secrets(
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


def _package_version() -> str:
    try:
        return version("minigent")
    except PackageNotFoundError:
        return "unknown"


def collect_debug_bundle(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    config: ClientConfig,
    trace_id: str | None,
) -> tuple[dict[str, object], bool]:
    checks, config_response = collect_connection_checks(args, client)
    success = not any(check.blocking for check in checks)
    threads = list_remembered_threads(config.base_url, args)
    server_config = _mask_secrets(config_response) if config_response is not None else None
    server_summary = _server_summary(config_response) if config_response is not None else {}
    agent_backend = config_response.get("agent_backend") if isinstance(config_response, dict) else None
    mcp_servers = config_response.get("mcp_servers") if isinstance(config_response, dict) else None
    bundle: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": {
            "minigent": _package_version(),
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
            "summary": server_summary,
            "config": server_config,
        },
        "mcp": {
            "broker_enabled": (
                agent_backend.get("mcp_broker_enabled") if isinstance(agent_backend, dict) else None
            ),
            "server_count": len(mcp_servers) if isinstance(mcp_servers, list) else None,
        },
        "threads": {
            "last_thread_id": ClientState.load().get_last_thread(state_scope_key(config.base_url, args)),
            "recent": [item.to_dict() for item in threads[:10]],
        },
        "recent_events": "not collected by the local CLI; rerun the failing command with --verbose or --stream",
    }
    if trace_id is not None:
        bundle["trace_id"] = trace_id
    return bundle, success


def _format_debug_bundle(bundle: dict[str, object]) -> str:
    lines = ["Minigent debug bundle", ""]
    version_info = bundle.get("version") if isinstance(bundle.get("version"), dict) else {}
    platform_info = bundle.get("platform") if isinstance(bundle.get("platform"), dict) else {}
    client_info = bundle.get("client") if isinstance(bundle.get("client"), dict) else {}
    server_info = bundle.get("server") if isinstance(bundle.get("server"), dict) else {}
    mcp_info = bundle.get("mcp") if isinstance(bundle.get("mcp"), dict) else {}
    threads_info = bundle.get("threads") if isinstance(bundle.get("threads"), dict) else {}

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
    auth = client_info.get("auth") if isinstance(client_info.get("auth"), dict) else {}
    lines.append(
        "auth: "
        f"mode={auth.get('mode')} user={auth.get('user_id')} tenant={auth.get('tenant_id')} "
        f"admin={auth.get('admin')} token={auth.get('api_token') or '<not set>'}"
    )
    lines.append("")
    lines.append("Checks")
    for check in bundle.get("checks", []):
        if isinstance(check, dict):
            lines.append(_format_check(DiagnosticCheck(**check)))
    lines.append("")
    summary = server_info.get("summary") if isinstance(server_info.get("summary"), dict) else {}
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
    text = json.dumps(bundle, indent=2, sort_keys=True) + "\n" if args.json else _format_debug_bundle(bundle)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

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
        if args.command == "debug-bundle":
            return run_debug_bundle(args, client, config, trace_id)
        if args.command == "config":
            if args.config_command in {None, "show"}:
                return run_config(client, trace_id)
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
