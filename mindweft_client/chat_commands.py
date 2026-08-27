from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import secrets
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence, TextIO, cast

from mindweft_client.api_client import MindweftAPIClient
from mindweft_client.audio_files import read_audio_file
from mindweft_client.config import ClientConfig, build_client_config
from mindweft_client.document_files import read_document_file
from mindweft_client.output import (
    StreamProgressRenderer,
    TokenMode,
    format_message,
    print_json,
    style_assistant_markdown,
    token_usage_from_event,
)
from mindweft_client.state import ClientState, ThreadHistoryItem, thread_history_items_from_api
from mindweft_client.state import state_scope_key as build_state_scope_key
from mindweft_client.thread_titles import (
    is_placeholder_thread_title,
)
from mindweft_client.thread_titles import (
    thread_title_from_message as _thread_title_from_message,
)


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
    scope_key = state_scope_key(base_url, args)
    existing = next(
        (item for item in state.list_threads(scope_key) if item.thread_id == thread_id), None
    )
    if existing is not None and not is_placeholder_thread_title(existing.title):
        title = existing.title
    state.set_last_thread(scope_key, thread_id, title=title, message_count=message_count)
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


def build_client(args: argparse.Namespace, trace_id: str | None) -> MindweftAPIClient:
    progress_stream: TextIO = (
        cast(TextIO, _QuietProgressStream()) if getattr(args, "quiet", False) else sys.stderr
    )
    token_mode = cast(
        TokenMode,
        "off" if getattr(args, "quiet", False) else getattr(args, "tokens", "auto"),
    )
    return MindweftAPIClient(
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
    client: MindweftAPIClient,
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


def _audio_parts_from_paths(paths: Sequence[str] | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for raw_path in paths or []:
        try:
            path, mime_type, data = read_audio_file(raw_path)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        parts.append(
            {
                "type": "audio",
                "mime_type": mime_type,
                "data": base64.b64encode(data).decode("ascii"),
                "filename": path.name,
            }
        )
    return parts


def _document_parts_from_paths(paths: Sequence[str] | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for raw_path in paths or []:
        try:
            path, mime_type, data = read_document_file(raw_path)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        parts.append(
            {
                "type": "document",
                "mime_type": mime_type,
                "data": base64.b64encode(data).decode("ascii"),
                "filename": path.name,
            }
        )
    return parts


def _message_parts(
    content: str,
    image_paths: Sequence[str] | None,
    document_paths: Sequence[str] | None,
    audio_paths: Sequence[str] | None = None,
    *,
    detail: str,
) -> list[dict[str, Any]] | None:
    image_parts = _image_parts_from_paths(image_paths, detail=detail)
    audio_parts = _audio_parts_from_paths(audio_paths)
    document_parts = _document_parts_from_paths(document_paths)
    if not image_parts and not audio_parts and not document_parts:
        return None
    parts: list[dict[str, Any]] = []
    if content:
        parts.append({"type": "text", "text": content})
    parts.extend(image_parts)
    parts.extend(audio_parts)
    parts.extend(document_parts)
    return parts


def run_chat(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    thread_id, created_thread = ensure_thread(args, client, base_url)
    message_parts = _message_parts(
        args.message,
        getattr(args, "image", None),
        getattr(args, "document", None),
        getattr(args, "audio", None),
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
        from mindweft_client.output import extract_reasoning_content, format_reasoning_block

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
    client: MindweftAPIClient,
    base_url: str,
    trace_id: str | None,
) -> int:
    try:
        response = client.list_threads(
            q=getattr(args, "search", None),
            archived=getattr(args, "archived", False),
            pinned=True if getattr(args, "pinned", False) else None,
        )
    except RuntimeError:
        threads = list_remembered_threads(base_url, args)
    else:
        threads = thread_history_items_from_api(response)
        state = ClientState.load()
        state.thread_history[state_scope_key(base_url, args)] = threads
        state.save()
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
    print("Archived threads" if getattr(args, "archived", False) else "Recent threads")
    print("")
    for index, item in enumerate(threads, start=1):
        title = item.title or "Untitled thread"
        updated_at = item.updated_at or "unknown"
        message_count = "?" if item.message_count is None else str(item.message_count)
        print(f"{index}. {updated_at}  {title}  messages={message_count}  {item.thread_id}")
    return 0


def run_threads_search(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    response = client.search_threads(
        args.query,
        scope=args.scope,
        archived=args.archived,
        limit=100,
    )
    if args.json:
        output = dict(response)
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    results = response.get("results")
    if not isinstance(results, list) or not results:
        print("No matching threads.")
        return 0
    for index, result in enumerate(results, start=1):
        if not isinstance(result, dict):
            continue
        thread = result.get("thread")
        if not isinstance(thread, dict):
            continue
        print(f"{index}. {thread.get('title') or 'Untitled thread'}  {thread.get('thread_id')}")
        matches = result.get("matches")
        if isinstance(matches, list):
            for match in matches:
                if isinstance(match, dict):
                    print(f"   {match.get('role', 'message')}: {match.get('snippet', '')}")
    return 0


def run_threads_organization(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    command = args.threads_command
    organization = {
        "pinned": command == "pin" if command in {"pin", "unpin"} else None,
        "archived": command == "archive" if command in {"archive", "restore"} else None,
    }
    thread = client.update_thread_organization(args.thread_id, **organization)
    if args.json:
        output: dict[str, Any] = {"thread": thread}
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    labels = {
        "pin": "Pinned",
        "unpin": "Unpinned",
        "archive": "Archived",
        "restore": "Restored",
    }
    print(f"{labels[command]} thread {args.thread_id}")
    return 0


def run_resume(
    args: argparse.Namespace,
    client: MindweftAPIClient,
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
    lines = ["# Mindweft transcript", "", f"Thread: `{thread_id}`", ""]
    for message in messages:
        role = str(message.get("role") or "message").replace("_", " ").title()
        tool_name = message.get("tool_name")
        heading = role if not tool_name else f"{role} ({tool_name})"
        content = str(message.get("content") or "")
        lines.extend([f"## {heading}", "", content, ""])
    return "\n".join(lines).rstrip() + "\n"


def run_export(
    args: argparse.Namespace,
    client: MindweftAPIClient,
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
        text = json.dumps(output, indent=2, sort_keys=True) + "\n"
    else:
        trace_comment = f"<!-- trace_id={trace_id} -->\n" if trace_id is not None else ""
        text = trace_comment + _format_markdown_transcript(thread_id, messages)
    output_path = getattr(args, "output", None)
    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def run_threads_retitle(
    args: argparse.Namespace,
    client: MindweftAPIClient,
    trace_id: str | None,
) -> int:
    limit = int(args.limit)
    concurrency = int(args.concurrency)
    if limit < 1 or limit > 10_000:
        raise SystemExit("--limit must be between 1 and 10000")
    if concurrency < 1 or concurrency > 8:
        raise SystemExit("--concurrency must be between 1 and 8")

    inspected: list[dict[str, Any]] = []
    offset = 0
    while len(inspected) < limit:
        page_limit = min(100, limit - len(inspected))
        response = client.list_threads(limit=page_limit, offset=offset)
        page = response.get("threads")
        if not isinstance(page, list) or not page:
            break
        inspected.extend(item for item in page if isinstance(item, dict))
        offset += len(page)
        total = response.get("total")
        if isinstance(total, int) and offset >= total:
            break

    eligible = [
        item
        for item in inspected
        if item.get("title_source") not in {"manual", "semantic"}
        and isinstance(item.get("message_count"), int)
        and item["message_count"] > 0
        and isinstance(item.get("thread_id"), str)
    ]
    if args.dry_run:
        results = [
            {
                "thread_id": item["thread_id"],
                "status": "eligible",
                "title": item.get("title"),
            }
            for item in eligible
        ]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(client.generate_thread_title, str(item["thread_id"])): str(
                    item["thread_id"]
                )
                for item in eligible
            }
            for future in as_completed(futures):
                thread_id = futures[future]
                try:
                    result = future.result()
                except RuntimeError as exc:
                    result = {
                        "thread_id": thread_id,
                        "status": "failed",
                        "reason": str(exc),
                    }
                results.append(result)
        results.sort(key=lambda item: str(item.get("thread_id", "")))

    skipped_before_request = len(inspected) - len(eligible)
    counts = Counter(str(item.get("status", "unknown")) for item in results)
    summary = {
        "inspected": len(inspected),
        "eligible": len(eligible),
        "updated": counts["updated"],
        "skipped": counts["skipped"] + skipped_before_request,
        "failed": counts["failed"],
        "insufficient_context": sum(
            1 for item in results if item.get("reason") == "insufficient_context"
        ),
    }
    if args.json:
        output: dict[str, Any] = {
            "dry_run": bool(args.dry_run),
            "summary": summary,
            "results": results,
        }
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0 if not summary["failed"] else 1

    if trace_id is not None:
        print(f"trace_id={trace_id}")
    for item in results:
        detail = item.get("title") or item.get("reason") or ""
        print(f"{item.get('status', 'unknown')}: {item.get('thread_id')}  {detail}".rstrip())
    print(
        "Summary: "
        f"inspected={summary['inspected']} eligible={summary['eligible']} "
        f"updated={summary['updated']} skipped={summary['skipped']} "
        f"failed={summary['failed']} insufficient_context={summary['insufficient_context']}"
    )
    return 0 if not summary["failed"] else 1


def run_threads_create(
    args: argparse.Namespace,
    client: MindweftAPIClient,
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
    client: MindweftAPIClient,
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
    client: MindweftAPIClient,
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
