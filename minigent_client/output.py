from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from typing import Any, Literal, TextIO

TokenMode = Literal["auto", "live", "off"]

_MAX_INLINE_ARGUMENT_CHARS = 80
_MAX_TOOL_RESULT_CHARS = 2000
_RESET = "\033[0m"
_STYLES = {
    "assistant": "\033[32m",
    "dim": "\033[2m",
    "error": "\033[31m",
    "idle": "\033[2m",
    "markdown_code": "\033[36m",
    "markdown_fence": "\033[38;5;248m",
    "markdown_heading": "\033[1m",
    "markdown_bold": "\033[1m",
    "progress": "\033[38;5;248m",
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


def style_stream_progress_line(line: str, *, stream: TextIO) -> str:
    """Style streaming progress as provisional output.

    Stream progress is not the final assistant reply, so render the whole line
    as medium gray. ANSI faint is too subtle or ignored in some terminal/tmux
    combinations. Error lines remain high-contrast.
    """

    if line.startswith("✖"):
        return style_line(line, stream=stream)
    if line.startswith("⚠"):
        return style_text(line, "warning", stream=stream)
    return style_text(line, "progress", stream=stream)


def extract_reasoning_content(metadata: dict[str, Any] | None) -> str | None:
    """Extract reasoning content from message metadata.

    Supports:
    - OpenAI Responses API: summary text from reasoning items
    - OpenRouter/DeepSeek R1: reasoning field in chat completion response
    - Gemini: thinking content (if available)
    """
    if not metadata:
        return None

    # Direct reasoning content (e.g., from OpenRouter DeepSeek R1)
    reasoning_content = metadata.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content.strip()

    # OpenAI Responses API reasoning items
    reasoning_items = metadata.get("generic_oauth_responses_output_items")
    if isinstance(reasoning_items, list):
        summary_parts: list[str] = []
        for item in reasoning_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "reasoning":
                continue
            summary = item.get("summary")
            if isinstance(summary, list):
                for summary_item in summary:
                    if isinstance(summary_item, dict) and summary_item.get("type") == "summary_text":
                        text = summary_item.get("text")
                        if isinstance(text, str) and text.strip():
                            summary_parts.append(text.strip())
        if summary_parts:
            return "\n\n".join(summary_parts)

    # Gemini thought signature (opaque, not displayable)
    # Just return None - we can't show the actual thinking content
    return None


def format_reasoning_block(reasoning: str, *, stream: TextIO) -> str:
    """Format reasoning content for display.

    Returns styled text with a thinking indicator.
    """
    if not color_enabled(stream):
        return f"[Thinking]\n{reasoning}\n[End Thinking]"

    styled_lines: list[str] = []
    styled_lines.append(style_text("[Thinking]", "dim", stream=stream))
    for line in reasoning.splitlines():
        styled_lines.append(style_text(line, "dim", stream=stream))
    styled_lines.append(style_text("[End Thinking]", "dim", stream=stream))
    return "\n".join(styled_lines)


def style_assistant_markdown(text: str, *, stream: TextIO) -> str:
    """Add light Markdown-aware color without rendering or changing content."""

    if not color_enabled(stream):
        return text
    styled_lines: list[str] = []
    in_code_block = False
    for line in text.splitlines(keepends=True):
        newline = "\n" if line.endswith("\n") else ""
        content = line[:-1] if newline else line
        stripped = content.lstrip()
        if stripped.startswith(("```", "~~~")):
            styled_lines.append(style_text(content, "markdown_fence", stream=stream) + newline)
            in_code_block = not in_code_block
            continue
        if in_code_block:
            styled_lines.append(style_text(content, "markdown_code", stream=stream) + newline)
            continue
        if _MARKDOWN_HEADING_RE.match(content):
            styled_lines.append(style_text(content, "markdown_heading", stream=stream) + newline)
            continue
        if stripped.startswith(">"):
            styled_lines.append(style_text(content, "progress", stream=stream) + newline)
            continue
        content = _style_inline_bold(content, stream=stream)
        styled_lines.append(_style_inline_code(content, stream=stream) + newline)
    return "".join(styled_lines)


_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_INLINE_CODE_RE = re.compile(r"(`+)([^`\n]+?)\1")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")


def _style_inline_code(text: str, *, stream: TextIO) -> str:
    return _INLINE_CODE_RE.sub(
        lambda match: style_text(match.group(0), "markdown_code", stream=stream), text
    )


def _style_inline_bold(text: str, *, stream: TextIO) -> str:
    return _BOLD_RE.sub(
        lambda match: style_text(match.group(0), "markdown_bold", stream=stream), text
    )


def _style_prefix(line: str, prefix: str, style: str, *, stream: TextIO) -> str:
    return line.replace(prefix, style_text(prefix, style, stream=stream), 1)


def _format_bytes(n: int) -> str:
    """Human-friendly byte count."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} kB"
    return f"{n / (1024 * 1024):.1f} MB"


class _ProgressSpinner:
    """Animated spinner for TTY stderr, with optional byte counter."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, stream: TextIO, prefix: str) -> None:
        self._stream = stream
        self._prefix = prefix
        self._bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._tty = hasattr(stream, "isatty") and stream.isatty()
        self._start_time: float = 0.0

    def start(self) -> None:
        if not self._tty:
            return
        self._start_time = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update_bytes(self, n: int) -> None:
        with self._lock:
            self._bytes = n

    def _run(self) -> None:
        i = 0
        while not self._stop.wait(0.1):
            with self._lock:
                b = self._bytes
            elapsed = time.monotonic() - self._start_time
            frame = self.FRAMES[i % len(self.FRAMES)]
            parts: list[str] = []
            parts.append(f"{elapsed:.1f}s")
            if b:
                parts.append(_format_bytes(b))
            sep = " \u00b7 "
            suffix = f" {sep.join(parts)}" if parts else ""
            self._stream.write(f"\r{self._prefix}{suffix} {frame}  ")
            self._stream.flush()
            i += 1

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.2)
            # Use unbuffered write to guarantee the terminal processes
            # the cursor movement before any subsequent stdout write.
            try:
                fd = self._stream.fileno() if hasattr(self._stream, "fileno") else -1
            except OSError:
                fd = -1
            if fd >= 0:
                os.write(fd, b"\r\x1b[2K\n")
            else:
                self._stream.write("\r\x1b[2K\n")
                self._stream.flush()


class StreamProgressRenderer:
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        verbose: bool = False,
        show_tool_results: bool = False,
        show_reasoning: bool = False,
        token_mode: TokenMode = "auto",
    ) -> None:
        self._stream = stream or sys.stderr
        self._verbose = verbose
        self._show_tool_results = show_tool_results
        self._show_reasoning = show_reasoning
        self._token_mode = token_mode
        self._seen_peer_update_tasks: set[str] = set()
        self._peer_task_statuses: dict[str, str] = {}
        self._tool_call_arguments: dict[str, str] = {}
        self._tool_name_arguments: dict[str, str] = {}
        self._peer_tool_call_arguments: dict[str, str] = {}
        self._peer_tool_name_arguments: dict[str, str] = {}
        self._last_usage_summary: str | None = None
        self._last_thread_context_summary: str | None = None
        self._saw_peer_event = False
        self._pending_summary: str | None = None
        self._current_spinner: _ProgressSpinner | None = None

    def set_verbose(self, verbose: bool) -> None:
        self._verbose = verbose

    def render(self, event: dict[str, Any]) -> None:
        usage = format_usage_summary(event)
        if usage is not None:
            self._last_usage_summary = usage
            if self._token_mode == "live" and event.get("type") != "run.completed":
                self._write(f"● {usage}")
        thread_context = format_thread_context_summary(event)
        if thread_context is not None:
            self._last_thread_context_summary = thread_context
        event_type = event.get("type")
        if event_type == "run.started":
            self._write("● preparing")
        elif event_type == "llm.request":
            suffix = f" iteration={event.get('iteration')}" if self._verbose else ""
            spinner = _ProgressSpinner(self._stream, f"● sending{suffix}")
            if spinner._tty:
                self._stop_spinner()
                self._current_spinner = spinner
                self._write_inline(f"● sending{suffix}")
                spinner.start()
            else:
                self._write(f"● sending{suffix}")
        elif event_type == "llm.progress":
            if self._current_spinner:
                self._current_spinner.update_bytes(event.get("bytes", 0))
        elif event_type == "assistant.message":
            self._stop_spinner()
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
        elif event_type == "reasoning":
            if self._show_reasoning:
                content = event.get("content", "")
                if content:
                    self._write_reasoning_block(content)
        elif event_type == "run.error":
            self._write(f"✖ error {event.get('status_code')}: {_format_error_detail(event.get('detail'))}")
        elif event_type == "run.warning":
            self._write(f"⚠ {_format_error_detail(event.get('detail'))}")
        elif event_type == "run.completed":
            summaries = []
            if self._token_mode != "off":
                if self._last_thread_context_summary is not None:
                    summaries.append(self._last_thread_context_summary)
                if self._last_usage_summary is not None:
                    summaries.append(self._last_usage_summary)
            if summaries:
                summary_line = f"● done · {' · '.join(summaries)}"
            elif self._token_mode == "live" and self._saw_peer_event:
                summary_line = "● done · tokens unavailable for peer backend"
            else:
                summary_line = "● done"
            if self._token_mode == "live":
                self._write(summary_line)
            else:
                self._pending_summary = summary_line

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

    def _write_reasoning_block(self, content: str) -> None:
        """Display reasoning/thinking content in styled format."""
        if not content.strip():
            return
        styled_start = style_text("[Thinking]", "dim", stream=self._stream)
        styled_end = style_text("[End Thinking]", "dim", stream=self._stream)
        self._write(styled_start)
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            styled_line = style_text(stripped, "dim", stream=self._stream)
            self._write(f"  {styled_line}")
        self._write(styled_end)

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
            arguments = _format_peer_tool_arguments(peer_event)
            call_id = _peer_event_tool_call_id(peer_event)
            if arguments:
                if call_id:
                    self._peer_tool_call_arguments[call_id] = arguments
                if tool_name:
                    self._peer_tool_name_arguments[tool_name] = arguments
            elif call_id:
                arguments = self._peer_tool_call_arguments.get(call_id, "")
            if not arguments and tool_name:
                arguments = self._peer_tool_name_arguments.get(tool_name, "")
            action = peer_event_type.removeprefix("tool_execution_")
            if action == "end":
                action = "end"
            suffix = f" {tool_name}" if tool_name else ""
            if suffix and arguments:
                suffix += f"({arguments})"
            status = peer_event.get("status")
            status_suffix = f" status={status}" if isinstance(status, str) and status else ""
            self._write(f"[peer] tool {action}{suffix}{status_suffix}")
            if self._show_tool_results:
                details = _peer_event_details(peer_event)
                if details:
                    self._write_tool_detail_block(details)
            return
        self._write(f"[peer] event {peer_event_type}")

    def stop_active_progress(self) -> None:
        """Stop any active live progress indicator without printing deferred output."""
        self._stop_spinner()

    def flush_pending_summary(self) -> None:
        """Write any pending summary line (e.g., token stats) and clear it."""
        self._stop_spinner()
        if self._pending_summary is not None:
            self._write(self._pending_summary)
            self._pending_summary = None

    def _stop_spinner(self) -> None:
        """Stop and clear any active progress spinner."""
        if self._current_spinner is not None:
            self._current_spinner.stop()
            self._current_spinner = None

    def _write_inline(self, line: str) -> None:
        """Write to the progress stream without a trailing newline."""
        self._stream.write(line)
        self._stream.flush()

    def _write(self, line: str) -> None:
        self._stop_spinner()
        self._stream.write(f"{style_stream_progress_line(line, stream=self._stream)}\n")
        self._stream.flush()


def _event_name(event: dict[str, Any]) -> str:
    value = event.get("name")
    if isinstance(value, str) and value:
        return value
    return "tool"


def _format_error_detail(detail: object) -> str:
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
        error_type = detail.get("type")
        if isinstance(error_type, str) and error_type:
            return error_type
    return str(detail)


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


def _format_peer_tool_arguments(peer_event: dict[str, Any]) -> str:
    args_summary = peer_event.get("args_summary")
    if isinstance(args_summary, str):
        if len(args_summary) > _MAX_INLINE_ARGUMENT_CHARS:
            return args_summary[: _MAX_INLINE_ARGUMENT_CHARS - 1] + "…"
        return args_summary
    return ""


def _peer_tool_argument_containers(peer_event: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [peer_event]
    for nested_key in ("tool_call", "toolCall", "tool_result", "toolResult"):
        nested = peer_event.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)
    return containers


def _peer_event_tool_call_id(peer_event: dict[str, Any]) -> str:
    for container in _peer_tool_argument_containers(peer_event):
        for key in ("tool_call_id", "toolCallId", "id"):
            value = container.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


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
    cache_read_tokens = _usage_int(usage, "cache_read_tokens", "cacheRead", "cache_read")
    cache_write_tokens = _usage_int(usage, "cache_write_tokens", "cacheWrite", "cache_write")
    if total_tokens is not None:
        result["total_tokens"] = total_tokens
    if cache_read_tokens is not None:
        result["cache_read_tokens"] = cache_read_tokens
    if cache_write_tokens is not None:
        result["cache_write_tokens"] = cache_write_tokens
    return result or None


def format_usage_summary(event_or_usage: dict[str, Any]) -> str | None:
    usage = token_usage_from_event(event_or_usage)
    if usage is None:
        return None
    parts: list[str] = []
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    cache_read_tokens = usage.get("cache_read_tokens")
    cache_write_tokens = usage.get("cache_write_tokens")
    if prompt_tokens is not None:
        parts.append(f"prompt {_format_token_count(prompt_tokens)}")
    if completion_tokens is not None:
        parts.append(f"completion {_format_token_count(completion_tokens)}")
    if total_tokens is not None:
        parts.append(f"total {_format_token_count(total_tokens)}")
    if cache_read_tokens is not None:
        parts.append(f"cache read {_format_token_count(cache_read_tokens)}")
    if cache_write_tokens is not None:
        parts.append(f"cache write {_format_token_count(cache_write_tokens)}")
    if not parts:
        return None
    return "tokens: " + " · ".join(parts)


def format_thread_context_summary(event: dict[str, Any]) -> str | None:
    thread_context = event.get("thread_context")
    if not isinstance(thread_context, dict):
        return None
    total_tokens = _usage_int(thread_context, "total_tokens")
    if total_tokens is None:
        return None
    estimated = " est." if thread_context.get("estimated") is True else ""
    return f"thread context{estimated}: {_format_token_count(total_tokens)}"


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
