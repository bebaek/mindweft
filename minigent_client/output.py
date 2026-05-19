from __future__ import annotations

import json
import sys
from typing import Any, TextIO


def format_message(message: dict[str, Any]) -> str:
    role = message["role"]
    tool_name = message.get("tool_name")
    suffix = f" ({tool_name})" if tool_name else ""
    return f"{role}{suffix}: {message['content']}"


def print_json(data: object, *, stream: TextIO | None = None) -> None:
    output_stream = stream or sys.stdout
    print(json.dumps(data, indent=2, sort_keys=True), file=output_stream)


class StreamProgressRenderer:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stderr
        self._seen_peer_update_tasks: set[str] = set()
        self._peer_task_statuses: dict[str, str] = {}

    def render(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "run.started":
            self._write("[run] started")
        elif event_type == "llm.request":
            self._write(f"[llm] request iteration={event.get('iteration')}")
        elif event_type == "tool.call":
            self._write(f"[tool] call {event.get('name')}")
        elif event_type == "tool.result":
            status = "error" if event.get("is_error") else "ok"
            self._write(f"[tool] result {event.get('name')} {status}")
        elif event_type == "peer.task.created":
            self._write_peer_task_status(event, label="created")
        elif event_type == "peer.task.poll":
            self._write_peer_task_status(event, label="status")
        elif event_type == "peer.task.completed":
            self._write_peer_task_status(event, label="completed")
        elif event_type == "peer.task.event":
            self._write_peer_task_event(event)
        elif event_type == "run.error":
            self._write(f"[run] error {event.get('status_code')}: {event.get('detail')}")
        elif event_type == "run.completed":
            self._write("[run] completed")

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
