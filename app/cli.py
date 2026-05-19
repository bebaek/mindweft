from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.request  # noqa: F401 - exposed for existing CLI tests that monkeypatch urlopen.
from typing import Any, Sequence

from minigent_client.api_client import MinigentAPIClient
from minigent_client.config import ClientConfig, build_client_config
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

    subparsers.add_parser("health", help="Check API health.")
    subparsers.add_parser("config", help="Show resolved API configuration.")

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
    return MinigentAPIClient(build_config(args, trace_id), progress_stream=sys.stderr)


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


def format_message(message: dict[str, Any]) -> str:
    role = message["role"]
    tool_name = message.get("tool_name")
    suffix = f" ({tool_name})" if tool_name else ""
    return f"{role}{suffix}: {message['content']}"


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _make_stream_progress_printer() -> Any:
    seen_peer_update_tasks: set[str] = set()
    peer_task_statuses: dict[str, str] = {}

    def print_progress(event: dict[str, Any]) -> None:
        _print_stream_progress(
            event,
            seen_peer_update_tasks=seen_peer_update_tasks,
            peer_task_statuses=peer_task_statuses,
        )

    return print_progress


def _print_stream_progress(
    event: dict[str, Any],
    *,
    seen_peer_update_tasks: set[str] | None = None,
    peer_task_statuses: dict[str, str] | None = None,
) -> None:
    event_type = event.get("type")
    if event_type == "run.started":
        print("[run] started", file=sys.stderr)
    elif event_type == "llm.request":
        print(f"[llm] request iteration={event.get('iteration')}", file=sys.stderr)
    elif event_type == "tool.call":
        print(f"[tool] call {event.get('name')}", file=sys.stderr)
    elif event_type == "tool.result":
        status = "error" if event.get("is_error") else "ok"
        print(f"[tool] result {event.get('name')} {status}", file=sys.stderr)
    elif event_type == "peer.task.created":
        _print_peer_task_status(event, label="created", peer_task_statuses=peer_task_statuses)
    elif event_type == "peer.task.poll":
        _print_peer_task_status(event, label="status", peer_task_statuses=peer_task_statuses)
    elif event_type == "peer.task.completed":
        _print_peer_task_status(event, label="completed", peer_task_statuses=peer_task_statuses)
    elif event_type == "peer.task.event":
        _print_peer_task_event(event, seen_peer_update_tasks=seen_peer_update_tasks)
    elif event_type == "run.error":
        print(f"[run] error {event.get('status_code')}: {event.get('detail')}", file=sys.stderr)
    elif event_type == "run.completed":
        print("[run] completed", file=sys.stderr)


def _print_peer_task_status(
    event: dict[str, Any],
    *,
    label: str,
    peer_task_statuses: dict[str, str] | None,
) -> None:
    peer = event.get("peer")
    task_id = str(event.get("task_id") or "")
    status = str(event.get("status") or "")
    if label == "status":
        previous_status = peer_task_statuses.get(task_id) if peer_task_statuses is not None else None
        if previous_status == status:
            return
    if peer_task_statuses is not None and task_id and status:
        peer_task_statuses[task_id] = status
    task_part = f" task_id={task_id}" if task_id else ""
    print(f"[peer] task {label} peer={peer}{task_part} status={status}", file=sys.stderr)


def _print_peer_task_event(
    event: dict[str, Any],
    *,
    seen_peer_update_tasks: set[str] | None,
) -> None:
    peer_event = event.get("event")
    if not isinstance(peer_event, dict):
        print("[peer] event", file=sys.stderr)
        return
    peer_event_type = str(peer_event.get("type") or peer_event.get("event") or "event")
    if peer_event_type == "message_update":
        task_id = str(event.get("task_id") or "")
        if seen_peer_update_tasks is not None and task_id in seen_peer_update_tasks:
            return
        if seen_peer_update_tasks is not None and task_id:
            seen_peer_update_tasks.add(task_id)
        print("[peer] message updating...", file=sys.stderr)
        return
    if peer_event_type in {"message_start", "message_end", "turn_start", "turn_end", "session"}:
        return
    if peer_event_type == "agent_start":
        print("[peer] agent started", file=sys.stderr)
        return
    if peer_event_type == "agent_end":
        print("[peer] agent finished", file=sys.stderr)
        return
    if peer_event_type in {"tool_execution_start", "tool_execution_end"}:
        tool_name = _peer_event_tool_name(peer_event)
        action = "start" if peer_event_type.endswith("start") else "end"
        suffix = f" {tool_name}" if tool_name else ""
        print(f"[peer] tool {action}{suffix}", file=sys.stderr)
        return
    print(f"[peer] event {peer_event_type}", file=sys.stderr)


def _peer_event_tool_name(peer_event: dict[str, Any]) -> str:
    for key in ("tool_name", "name", "tool"):
        value = peer_event.get(key)
        if isinstance(value, str) and value:
            return value
    tool_call = peer_event.get("tool_call")
    if isinstance(tool_call, dict):
        for key in ("name", "tool_name"):
            value = tool_call.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


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


def run_config(client: MinigentAPIClient, trace_id: str | None) -> int:
    response = client.config()
    if trace_id is not None:
        response = {**response, "trace_id": trace_id}
    print_json(response)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    trace_id = secrets.token_hex(16) if args.trace else None
    config = build_config(args, trace_id)
    base_url = config.base_url
    client = MinigentAPIClient(config, progress_stream=sys.stderr)

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
        if args.command == "health":
            return run_health(client, args.json, trace_id)
        if args.command == "config":
            return run_config(client, trace_id)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    parser.error(f"Unhandled command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
