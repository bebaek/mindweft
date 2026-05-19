from __future__ import annotations

import json
import sys
from typing import Any, TextIO

_MAX_INLINE_ARGUMENT_CHARS = 80


def format_message(message: dict[str, Any]) -> str:
    role = message["role"]
    tool_name = message.get("tool_name")
    suffix = f" ({tool_name})" if tool_name else ""
    return f"{role}{suffix}: {message['content']}"


def print_json(data: object, *, stream: TextIO | None = None) -> None:
    output_stream = stream or sys.stdout
    print(json.dumps(data, indent=2, sort_keys=True), file=output_stream)


class StreamProgressRenderer:
    def __init__(self, stream: TextIO | None = None, *, verbose: bool = False) -> None:
        self._stream = stream or sys.stderr
        self._verbose = verbose
        self._seen_peer_update_tasks: set[str] = set()
        self._peer_task_statuses: dict[str, str] = {}
        self._tool_call_arguments: dict[str, str] = {}
        self._tool_name_arguments: dict[str, str] = {}
        self._last_usage_summary: str | None = None

    def render(self, event: dict[str, Any]) -> None:
        usage = _format_usage_summary(event)
        if usage is not None:
            self._last_usage_summary = usage
        event_type = event.get("type")
        if event_type == "run.started":
            self._write("● preparing")
        elif event_type == "llm.request":
            suffix = f" iteration={event.get('iteration')}" if self._verbose else ""
            self._write(f"● sending{suffix}")
        elif event_type == "tool.call":
            self._write_tool_call(event)
        elif event_type == "tool.result":
            self._write_tool_result(event)
        elif event_type == "quality.remote_request":
            self._write("● reviewing")
        elif event_type == "peer.task.created":
            self._write_peer_task_status(event, label="created")
        elif event_type == "peer.task.poll":
            self._write_peer_task_status(event, label="status")
        elif event_type == "peer.task.completed":
            self._write_peer_task_status(event, label="completed")
        elif event_type == "peer.task.event":
            self._write_peer_task_event(event)
        elif event_type == "run.error":
            self._write(f"✖ error {event.get('status_code')}: {event.get('detail')}")
        elif event_type == "run.completed":
            usage = self._last_usage_summary
            self._write("● done" if usage is None else f"● done · {usage}")

    def _write_tool_call(self, event: dict[str, Any]) -> None:
        name = _event_name(event)
        arguments = _format_tool_arguments(event.get("arguments"))
        call_id = str(event.get("tool_call_id") or "")
        if call_id:
            self._tool_call_arguments[call_id] = arguments
        self._tool_name_arguments[name] = arguments
        self._write(f"🔧 {name}({arguments}) ...")

    def _write_tool_result(self, event: dict[str, Any]) -> None:
        name = _event_name(event)
        call_id = str(event.get("tool_call_id") or "")
        arguments = self._tool_call_arguments.get(call_id, self._tool_name_arguments.get(name, ""))
        status = "error" if event.get("is_error") else "done"
        suffix = f" ({status})" if status == "error" else " done"
        self._write(f"🔧 {name}({arguments}){suffix}")

    def _write_peer_task_status(self, event: dict[str, Any], *, label: str) -> None:
        peer = event.get("peer")
        task_id = str(event.get("task_id") or "")
        status = str(event.get("status") or "")
        if label == "status":
            previous_status = self._peer_task_statuses.get(task_id)
            if previous_status == status:
                return
        if task_id and status:
            self._peer_task_statuses[task_id] = status
        task_part = f" task_id={task_id}" if task_id else ""
        self._write(f"[peer] task {label} peer={peer}{task_part} status={status}")

    def _write_peer_task_event(self, event: dict[str, Any]) -> None:
        peer_event = event.get("event")
        if not isinstance(peer_event, dict):
            self._write("[peer] event")
            return
        peer_event_type = str(peer_event.get("type") or peer_event.get("event") or "event")
        if peer_event_type == "message_update":
            task_id = str(event.get("task_id") or "")
            if task_id in self._seen_peer_update_tasks:
                return
            if task_id:
                self._seen_peer_update_tasks.add(task_id)
            self._write("[peer] message updating...")
            return
        if peer_event_type in {"message_start", "message_end", "turn_start", "turn_end", "session"}:
            return
        if peer_event_type == "agent_start":
            self._write("[peer] agent started")
            return
        if peer_event_type == "agent_end":
            self._write("[peer] agent finished")
            return
        if peer_event_type in {"tool_execution_start", "tool_execution_end"}:
            tool_name = peer_event_tool_name(peer_event)
            action = "start" if peer_event_type.endswith("start") else "end"
            suffix = f" {tool_name}" if tool_name else ""
            self._write(f"[peer] tool {action}{suffix}")
            return
        self._write(f"[peer] event {peer_event_type}")

    def _write(self, line: str) -> None:
        self._stream.write(f"{line}\n")
        self._stream.flush()


def _event_name(event: dict[str, Any]) -> str:
    value = event.get("name")
    if isinstance(value, str) and value:
        return value
    return "tool"


def _format_tool_arguments(arguments: object) -> str:
    if arguments is None:
        return ""
    if isinstance(arguments, dict):
        text = ", ".join(
            f"{key}={_format_argument_value(value)}" for key, value in arguments.items()
        )
    else:
        text = _format_argument_value(arguments)
    if len(text) <= _MAX_INLINE_ARGUMENT_CHARS:
        return text
    return text[: _MAX_INLINE_ARGUMENT_CHARS - 1] + "…"


def _format_argument_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _format_usage_summary(event: dict[str, Any]) -> str | None:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = event
    prompt_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_int(usage, "total_tokens")
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    parts: list[str] = []
    if prompt_tokens is not None:
        parts.append(f"prompt {_format_token_count(prompt_tokens)}")
    if completion_tokens is not None:
        parts.append(f"completion {_format_token_count(completion_tokens)}")
    if total_tokens is not None:
        parts.append(f"total {_format_token_count(total_tokens)}")
    if not parts:
        return None
    return "tokens: " + " · ".join(parts)


def _usage_int(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, int):
            return value
    return None


def _format_token_count(value: int) -> str:
    if value < 1000:
        return str(value)
    return f"{value / 1000:.1f}k"


def peer_event_tool_name(peer_event: dict[str, Any]) -> str:
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
