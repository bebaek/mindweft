from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

STATE_DIR_NAME = ".minigent"
STATE_FILE_NAME = "cli-state.json"


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


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, method=method, data=data, headers=request_headers)
    try:
        with urllib.request.urlopen(request) as response:
            raw_body = response.read().decode("utf-8")
            if not raw_body:
                return None
            return json.loads(raw_body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} failed: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{method} {url} failed: {exc.reason}") from exc


def build_trace_headers(trace_id: str | None) -> dict[str, str]:
    if trace_id is None:
        return {}
    parent_id = secrets.token_hex(8)
    return {"traceparent": f"00-{trace_id}-{parent_id}-01"}


def build_principal_headers(user_id: str, tenant_id: str, is_admin: bool) -> dict[str, str]:
    return {
        "X-Minigent-User-Id": user_id,
        "X-Minigent-Tenant-Id": tenant_id,
        "X-Minigent-Admin": "true" if is_admin else "false",
    }


def build_auth_headers(args: argparse.Namespace) -> dict[str, str]:
    if args.api_token:
        return {"Authorization": f"Bearer {args.api_token}"}
    return build_principal_headers(args.user_id, args.tenant_id, args.admin)


def state_file_path() -> Path:
    return Path.home() / STATE_DIR_NAME / STATE_FILE_NAME


def load_state() -> dict[str, Any]:
    path = state_file_path()
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def principal_key(args: argparse.Namespace) -> str:
    if args.api_token:
        token_fingerprint = hashlib.sha256(args.api_token.encode("utf-8")).hexdigest()[:16]
        return f"bearer:{token_fingerprint}"
    return f"dev:{args.user_id}:{args.tenant_id}:{str(args.admin).lower()}"


def state_scope_key(base_url: str, args: argparse.Namespace) -> str:
    return f"{base_url}|{principal_key(args)}"


def remember_thread(base_url: str, args: argparse.Namespace, thread_id: str) -> None:
    state = load_state()
    recent_threads = state.setdefault("recent_threads", {})
    if not isinstance(recent_threads, dict):
        recent_threads = {}
        state["recent_threads"] = recent_threads
    recent_threads[state_scope_key(base_url, args)] = thread_id
    save_state(state)


def load_remembered_thread(base_url: str, args: argparse.Namespace) -> str:
    state = load_state()
    recent_threads = state.get("recent_threads", {})
    if not isinstance(recent_threads, dict):
        raise SystemExit("No saved thread history is available for this CLI.")
    thread_id = recent_threads.get(state_scope_key(base_url, args))
    if not isinstance(thread_id, str) or not thread_id:
        raise SystemExit("No remembered thread for this server and principal. Start a chat first.")
    return thread_id


def forget_thread(base_url: str, args: argparse.Namespace, thread_id: str) -> None:
    state = load_state()
    recent_threads = state.get("recent_threads", {})
    if not isinstance(recent_threads, dict):
        return
    scope_key = state_scope_key(base_url, args)
    if recent_threads.get(scope_key) != thread_id:
        return
    del recent_threads[scope_key]
    save_state(state)


def ensure_thread(
    args: argparse.Namespace,
    base_url: str,
    headers: dict[str, str],
) -> tuple[str, bool]:
    if args.thread:
        return args.thread, False
    if args.resume_last:
        return load_remembered_thread(base_url, args), False
    payload = _build_thread_create_payload(args.skill, args.skills, args.capability_profile)
    response = request_json("POST", f"{base_url}/threads", payload=payload, headers=headers)
    return response["thread_id"], True


def format_message(message: dict[str, Any]) -> str:
    role = message["role"]
    tool_name = message.get("tool_name")
    suffix = f" ({tool_name})" if tool_name else ""
    return f"{role}{suffix}: {message['content']}"


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def run_chat(args: argparse.Namespace, base_url: str, headers: dict[str, str], trace_id: str | None) -> int:
    thread_id, created_thread = ensure_thread(args, base_url, headers)
    request_json(
        "POST",
        f"{base_url}/threads/{thread_id}/messages",
        payload={"content": args.message},
        headers=headers,
    )
    run_response = request_json("POST", f"{base_url}/threads/{thread_id}/run", headers=headers)
    reply = run_response["reply"]
    remember_thread(base_url, args, thread_id)

    if args.json:
        output: dict[str, Any] = {
            "thread_id": thread_id,
            "created_thread": created_thread,
            "reply": reply,
        }
        if trace_id is not None:
            output["trace_id"] = trace_id
        if args.transcript:
            output["messages"] = request_json(
                "GET", f"{base_url}/threads/{thread_id}/messages", headers=headers
            )
        print_json(output)
        return 0

    if trace_id is not None:
        print(f"trace_id={trace_id}")
    if args.print_thread_id:
        print(f"thread_id={thread_id}")
    print(reply)
    if args.transcript:
        messages = request_json("GET", f"{base_url}/threads/{thread_id}/messages", headers=headers)
        print("")
        for message in messages:
            print(format_message(message))
    return 0


def run_threads_create(
    args: argparse.Namespace, base_url: str, headers: dict[str, str], trace_id: str | None
) -> int:
    payload = _build_thread_create_payload(args.skill, args.skills, args.capability_profile)
    response = request_json("POST", f"{base_url}/threads", payload=payload, headers=headers)
    remember_thread(base_url, args, response["thread_id"])
    if args.json:
        output: dict[str, Any] = {"thread_id": response["thread_id"]}
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(response["thread_id"])
    return 0


def _build_thread_create_payload(
    skill: str | None,
    skills: list[str] | None,
    capability_profile: str | None,
) -> dict[str, Any] | None:
    if skill is not None and skills is not None:
        raise SystemExit("Provide either --skill or --skills, not both.")
    payload: dict[str, Any] = {}
    if skill is not None:
        payload["skill_name"] = skill
    if skills is not None:
        payload["skill_names"] = skills
    if capability_profile is not None:
        payload["capability_profile"] = capability_profile
    return payload or None


def run_threads_show(
    args: argparse.Namespace, base_url: str, headers: dict[str, str], trace_id: str | None
) -> int:
    messages = request_json("GET", f"{base_url}/threads/{args.thread_id}/messages", headers=headers)
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
    args: argparse.Namespace, base_url: str, headers: dict[str, str], trace_id: str | None
) -> int:
    request_json("DELETE", f"{base_url}/threads/{args.thread_id}", headers=headers)
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


def run_health(base_url: str, headers: dict[str, str], as_json: bool, trace_id: str | None) -> int:
    response = request_json("GET", f"{base_url}/health", headers=headers)
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


def run_config(base_url: str, headers: dict[str, str], trace_id: str | None) -> int:
    response = request_json("GET", f"{base_url}/config", headers=headers)
    if trace_id is not None and isinstance(response, dict):
        response = {**response, "trace_id": trace_id}
    print_json(response)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    base_url = args.base_url.rstrip("/")
    trace_id = secrets.token_hex(16) if args.trace else None
    headers = {
        **build_trace_headers(trace_id),
        **build_auth_headers(args),
    }

    if args.command == "chat":
        return run_chat(args, base_url, headers, trace_id)
    if args.command == "threads":
        if args.threads_command == "create":
            return run_threads_create(args, base_url, headers, trace_id)
        if args.threads_command == "show":
            return run_threads_show(args, base_url, headers, trace_id)
        if args.threads_command == "delete":
            return run_threads_delete(args, base_url, headers, trace_id)
    if args.command == "health":
        return run_health(base_url, headers, args.json, trace_id)
    if args.command == "config":
        return run_config(base_url, headers, trace_id)

    parser.error(f"Unhandled command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
