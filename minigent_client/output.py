from __future__ import annotations

import json
import os
import sys
from typing import Any, Literal, TextIO

TokenMode = Literal["auto", "live", "off"]

_MAX_INLINE_ARGUMENT_CHARS = 80
_MAX_TOOL_RESULT_CHARS = 2000
_RESET = "\033[0m"
_STYLES = {
    "assistant": "\033[32m",
    "error": "\033[31m",
    "idle": "\033[2m",
    "peer": "\033[35m",
    "status": "\033[2m",
    "tool": "\033[36m",
    "user": "\033[34m",
    "warning": "\033[33m",
}


def format_message(message: dict[str, Any]) -> str:
    role = message["role"]
    tool_name = message.get("tool_name")
    suffix = f" ({tool_name})" if tool_name else ""
    return f"{role}{suffix}: {message['content']}"


def print_json(data: object, *, stream: TextIO | None = None) -> None:
    output_stream = stream or sys.stdout
    print(json.dumps(data, indent=2, sort_keys=True), file=output_stream)


def color_enabled(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty() and "NO_COLOR" not in os.environ)


def style_text(text: str, style: str, *, stream: TextIO) -> str:
    if not color_enabled(stream):
        return text
    code = _STYLES.get(style)
    if code is None:
        return text
    return f"{code}{text}{_RESET}"


def style_line(line: str, *, stream: TextIO) -> str:
    if line.startswith("[assistant]"):
        return _style_prefix(line, "[assistant]", "assistant", stream=stream)
    if line.startswith("[user]"):
        return _style_prefix(line, "[user]", "user", stream=stream)
    if line.startswith("[warning]"):
        return _style_prefix(line, "[warning]", "warning", stream=stream)
    if line.startswith("[idle]"):
        return _style_prefix(line, "[idle]", "idle", stream=stream)
    if line.startswith("[peer]"):
        return _style_prefix(line, "[peer]", "peer", stream=stream)
    if line.startswith("✖"):
        return _style_prefix(line, "✖", "error", stream=stream)
    if line.startswith("🔧"):
        return _style_prefix(line, "🔧", "tool", stream=stream)
    if line.startswith("●"):
        return _style_prefix(line, "●", "status", stream=stream)
    return line


def _style_prefix(line: str, prefix: str, style: str, *, stream: TextIO) -> str:
    return line.replace(prefix, style_text(prefix, style, stream=stream), 1)


class StreamProgressRenderer:
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        verbose: bool = False,
        show_tool_results: bool = False,
        token_mode: TokenMode = "auto",
    ) -> None:
        self._stream = stream or sys.stderr
        self._verbose = verbose
        self._show_tool_results = show_tool_results
        self._token_mode = token_mode
        self._seen_peer_update_tasks: set[str] = set()
        self._peer_task_statuses: dict[str, str] = {}
        self._tool_call_arguments: dict[str, str] = {}
        self._tool_name_arguments: dict[str, str] = {}
        self._last_usage_summary: str | None = None
        self._saw_peer_event = False

    def set_verbose(self, verbose: bool) -> None:
        self._verbose = verbose

    def render(self, event: dict[str, Any]) -> None:
        usage = format_usage_summary(event)
        if usage is not None:
            self._last_usage_summary = usage
            if self._token_mode == "live" and event.get("type") != "run.completed":
                self._write(f"● {usage}")
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
            self._saw_peer_event = True
            self._write_peer_task_status(event, label="created")
        elif event_type == "peer.task.poll":
            self._saw_peer_event = True
            self._write_peer_task_status(event, label="status")
        elif event_type == "peer.task.completed":
            self._saw_peer_event = True
            self._write_peer_task_status(event, label="completed")
        elif event_type == "peer.task.event":
            self._saw_peer_event = True
            self._write_peer_task_event(event)
        elif event_type == "run.error":
            self._write(f"✖ error {event.get('status_code')}: {event.get('detail')}")
        elif event_type == "run.completed":
            usage = None if self._token_mode == "off" else self._last_usage_summary
            if usage is not None:
                self._write(f"● done · {usage}")
            elif self._token_mode == "live" and self._saw_peer_event:
                self._write("● done · tokens unavailable for peer backend")
            else:
                self._write("● done")

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
        if self._show_tool_results and "result" in event:
            self._write_tool_result_block(event["result"])

    def _write_tool_result_block(self, result: object) -> None:
        self._write_tool_detail_block(result, label="result")

    def _write_tool_detail_block(self, detail: object, *, label: str = "details") -> None:
        text = _format_tool_result(detail)
        if not self._verbose and len(text) > _MAX_TOOL_RESULT_CHARS:
            text = text[: _MAX_TOOL_RESULT_CHARS - 1].rstrip() + "…"
        self._write(f"   {label}:")
        for line in text.splitlines() or [""]:
            self._write(f"     {line}")

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
        if peer_event_type in {
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        }:
            tool_name = peer_event_tool_name(peer_event)
            action = peer_event_type.removeprefix("tool_execution_")
            if action == "end":
                action = "end"
            suffix = f" {tool_name}" if tool_name else ""
            status = peer_event.get("status")
            status_suffix = f" status={status}" if isinstance(status, str) and status else ""
            self._write(f"[peer] tool {action}{suffix}{status_suffix}")
            if self._show_tool_results:
                details = _peer_event_details(peer_event)
                if details:
                    self._write_tool_detail_block(details)
            return
        self._write(f"[peer] event {peer_event_type}")

    def _write(self, line: str) -> None:
        self._stream.write(f"{style_line(line, stream=self._stream)}\n")
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


def _format_tool_result(result: object) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _peer_event_details(peer_event: dict[str, Any]) -> dict[str, Any]:
    omitted_keys = {
        "index",
        "type",
        "event",
        "tool_call_id",
        "toolCallId",
        "tool_name",
        "toolName",
        "name",
        "tool",
        "status",
    }
    return {key: value for key, value in peer_event.items() if key not in omitted_keys}


def token_usage_from_event(event: dict[str, Any]) -> dict[str, int] | None:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = event
    prompt_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_int(usage, "total_tokens")
    if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
    result: dict[str, int] = {}
    if prompt_tokens is not None:
        result["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        result["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        result["total_tokens"] = total_tokens
    return result or None


def format_usage_summary(event_or_usage: dict[str, Any]) -> str | None:
    usage = token_usage_from_event(event_or_usage)
    if usage is None:
        return None
    parts: list[str] = []
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if prompt_tokens is not None:
        parts.append(f"prompt {_format_token_count(prompt_tokens)}")
    if completion_tokens is not None:
        parts.append(f"completion {_format_token_count(completion_tokens)}")
    if total_tokens is not None:
        parts.append(f"total {_format_token_count(total_tokens)}")
    if not parts:
        return None
    return "tokens: " + " · ".join(parts)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    total = _estimate_text_tokens(str(message.get("content") or ""))
    tool_name = message.get("tool_name")
    if isinstance(tool_name, str):
        total += _estimate_text_tokens(tool_name)
    tool_arguments = message.get("tool_arguments")
    if tool_arguments:
        total += _estimate_text_tokens(
            json.dumps(tool_arguments, ensure_ascii=True, sort_keys=True, default=str)
        )
    return total + 6


def estimate_thread_token_usage(messages: list[dict[str, Any]]) -> dict[str, int | bool]:
    total = 0
    for message in messages:
        if isinstance(message, dict):
            total += estimate_message_tokens(message)
    return {"estimated": True, "total_tokens": total, "message_count": len(messages)}


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 1
    return max(1, (len(text) + 3) // 4)


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
    for key in ("tool_name", "toolName", "name", "tool"):
        value = peer_event.get(key)
        if isinstance(value, str) and value:
            return value
    for nested_key in ("tool_call", "toolCall", "tool_result", "toolResult"):
        nested = peer_event.get(nested_key)
        if isinstance(nested, dict):
            for key in ("name", "tool_name", "toolName", "tool"):
                value = nested.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""
