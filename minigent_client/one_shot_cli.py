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
from minigent_client.api_client import MinigentAPIClient
from minigent_client.config import ClientConfig, build_client_config
from minigent_client.config_commands import (
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
from minigent_client.one_shot_parser import build_parser
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
        llm_profile=args.llm_profile,
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
        llm_profile=args.llm_profile,
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
    if section in {None, "agents"}:
        sections.append(_format_execution_agent_section("Agents", response.get("agents")))
    return "\n".join(part for part in sections if part) + ("\n" if sections else "")


def _format_execution_agent_section(title: str, payload: object) -> str:
    if not isinstance(payload, dict):
        return f"{title}:\n  none reported\n"
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
        suffix_parts: list[str] = []
        skill_name = item.get("skill_name") or item.get("skillName")
        skills = item.get("skills") or item.get("skill_names") or item.get("skillNames")
        capability_profile = item.get("capability_profile") or item.get("capabilityProfile")
        description = item.get("description")
        if isinstance(skill_name, str) and skill_name:
            suffix_parts.append(f"skill={skill_name}")
        if isinstance(skills, list) and all(isinstance(skill, str) for skill in skills):
            suffix_parts.append("skills=" + ",".join(skills))
        if isinstance(capability_profile, str) and capability_profile:
            suffix_parts.append(f"profile={capability_profile}")
        if isinstance(description, str) and description:
            suffix_parts.append(description)
        suffix = " — " + " · ".join(suffix_parts) if suffix_parts else ""
        lines.append(f"  {name}{suffix}")
    if len(lines) == 1:
        lines.append("  none configured")
    return "\n".join(lines) + "\n"


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
