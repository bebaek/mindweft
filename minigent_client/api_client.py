from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Iterator, TextIO, cast

from minigent_client.config import ClientConfig


class MinigentAPIClient:
    def __init__(
        self,
        config: ClientConfig,
        output_stream: TextIO | None = None,
        progress_stream: TextIO | None = None,
    ) -> None:
        self._config = config
        self._thread_id = config.thread_id
        self._output_stream = output_stream or sys.stdout
        self._progress_stream = progress_stream or sys.stderr

    def health(self) -> dict[str, Any]:
        response = self.request_json("GET", f"{self._config.base_url}/health")
        if not isinstance(response, dict):
            raise RuntimeError("Minigent health response must be an object")
        return cast(dict[str, Any], response)

    def config(self) -> dict[str, Any]:
        response = self.request_json("GET", f"{self._config.base_url}/config")
        if not isinstance(response, dict):
            raise RuntimeError("Minigent config response must be an object")
        return cast(dict[str, Any], response)

    def create_thread(
        self,
        *,
        skill_name: str | None = None,
        skills: list[str] | None = None,
        capability_profile: str | None = None,
    ) -> dict[str, Any]:
        payload = _build_thread_create_payload(
            skill_name=skill_name,
            skills=skills,
            capability_profile=capability_profile,
        )
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads",
            payload=payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent create-thread response must be an object")
        thread = cast(dict[str, Any], response)
        thread_id = thread.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            self._thread_id = thread_id
        return thread

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        messages = self.request_json(
            "GET",
            f"{self._config.base_url}/threads/{thread_id}/messages",
        )
        if not isinstance(messages, list):
            raise RuntimeError("Minigent thread messages response must be a list")
        return {"thread_id": thread_id, "messages": messages}

    def delete_thread(self, thread_id: str) -> None:
        self.request_json("DELETE", f"{self._config.base_url}/threads/{thread_id}")
        if self._thread_id == thread_id:
            self._thread_id = None

    def add_message(self, thread_id: str, content: str) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{thread_id}/messages",
            payload={"content": content},
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent add-message response must be an object")
        return cast(dict[str, Any], response)

    def ensure_thread(self) -> str:
        if self._thread_id:
            return self._thread_id
        response = self.create_thread(skill_name=self._config.skill_name)
        thread_id = response["thread_id"]
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("Minigent create-thread response must include thread_id")
        return thread_id

    def send_user_message(self, content: str) -> dict[str, Any]:
        thread_id = self.ensure_thread()
        formatted_content = self._format_user_message(content)
        self._maybe_log_prompt(formatted_content)
        return self.add_message(thread_id, formatted_content)

    def run_thread(self, thread_id: str | None = None, *, stream: bool | None = None) -> str:
        resolved_thread_id = thread_id or self.ensure_thread()
        use_stream = self._config.stream_runs if stream is None else stream
        if use_stream:
            return self._run_thread_stream(resolved_thread_id)
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{resolved_thread_id}/run",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent run response must be an object")
        reply = response["reply"]
        if not isinstance(reply, str):
            raise RuntimeError("Minigent reply must be a string")
        return reply

    def _run_thread_stream(self, thread_id: str) -> str:
        reply: str | None = None
        seen_peer_update_tasks: set[str] = set()
        peer_task_statuses: dict[str, str] = {}
        for event in self.request_ndjson_events(
            "POST",
            f"{self._config.base_url}/threads/{thread_id}/run/stream",
        ):
            self._maybe_log_stream_progress(
                event,
                seen_peer_update_tasks=seen_peer_update_tasks,
                peer_task_statuses=peer_task_statuses,
            )
            event_type = event.get("type")
            if event_type == "assistant.message":
                content = event.get("content")
                if not isinstance(content, str):
                    raise RuntimeError("Minigent stream assistant message must be a string")
                reply = content
            elif event_type == "run.error":
                status_code = event.get("status_code")
                detail = event.get("detail")
                raise RuntimeError(f"Minigent run stream failed: {status_code} {detail}")
        if reply is None:
            raise RuntimeError("Minigent run stream ended without an assistant message")
        return reply

    def request_ndjson_events(
        self,
        method: str,
        url: str,
    ) -> Iterator[dict[str, Any]]:
        headers = {
            "Accept": "application/x-ndjson",
            **self._config.principal.build_headers(),
            **self._config.extra_headers,
        }
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise RuntimeError(f"{method} {url} returned a non-object NDJSON event")
                    yield event
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {url} returned invalid NDJSON: {exc}") from exc

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = {**self._config.principal.build_headers(), **self._config.extra_headers}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                raw_body = response.read().decode("utf-8")
                if not raw_body:
                    return None
                return json.loads(raw_body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

    def _format_user_message(self, content: str) -> str:
        preamble = self._resolved_prompt_preamble()
        if preamble is None:
            return content
        return f"Client context:\n{preamble}\n\n{content}"

    def _maybe_log_prompt(self, content: str) -> None:
        if not self._config.debug_show_prompt:
            return
        self._output_stream.write(f"[prompt]\n{content}\n")
        self._output_stream.flush()

    def _maybe_log_stream_progress(
        self,
        event: dict[str, Any],
        *,
        seen_peer_update_tasks: set[str] | None = None,
        peer_task_statuses: dict[str, str] | None = None,
    ) -> None:
        event_type = event.get("type")
        if event_type == "run.started":
            self._write_progress("[run] started")
        elif event_type == "llm.request":
            self._write_progress(f"[llm] request iteration={event.get('iteration')}")
        elif event_type == "tool.call":
            self._write_progress(f"[tool] call {event.get('name')}")
        elif event_type == "tool.result":
            status = "error" if event.get("is_error") else "ok"
            self._write_progress(f"[tool] result {event.get('name')} {status}")
        elif event_type == "peer.task.created":
            self._write_peer_task_status(
                event, label="created", peer_task_statuses=peer_task_statuses
            )
        elif event_type == "peer.task.poll":
            self._write_peer_task_status(
                event, label="status", peer_task_statuses=peer_task_statuses
            )
        elif event_type == "peer.task.completed":
            self._write_peer_task_status(
                event, label="completed", peer_task_statuses=peer_task_statuses
            )
        elif event_type == "peer.task.event":
            self._write_peer_task_event(event, seen_peer_update_tasks=seen_peer_update_tasks)
        elif event_type == "run.error":
            self._write_progress(f"[run] error {event.get('status_code')}: {event.get('detail')}")
        elif event_type == "run.completed":
            self._write_progress("[run] completed")

    def _write_peer_task_status(
        self,
        event: dict[str, Any],
        *,
        label: str,
        peer_task_statuses: dict[str, str] | None,
    ) -> None:
        peer = event.get("peer")
        task_id = str(event.get("task_id") or "")
        status = str(event.get("status") or "")
        if label == "status":
            previous_status = peer_task_statuses.get(task_id) if peer_task_statuses else None
            if previous_status == status:
                return
        if peer_task_statuses is not None and task_id and status:
            peer_task_statuses[task_id] = status
        task_part = f" task_id={task_id}" if task_id else ""
        self._write_progress(f"[peer] task {label} peer={peer}{task_part} status={status}")

    def _write_peer_task_event(
        self,
        event: dict[str, Any],
        *,
        seen_peer_update_tasks: set[str] | None,
    ) -> None:
        peer_event = event.get("event")
        if not isinstance(peer_event, dict):
            self._write_progress("[peer] event")
            return
        peer_event_type = str(peer_event.get("type") or peer_event.get("event") or "event")
        if peer_event_type == "message_update":
            task_id = str(event.get("task_id") or "")
            if seen_peer_update_tasks is not None and task_id in seen_peer_update_tasks:
                return
            if seen_peer_update_tasks is not None and task_id:
                seen_peer_update_tasks.add(task_id)
            self._write_progress("[peer] message updating...")
            return
        if peer_event_type in {"message_start", "message_end", "turn_start", "turn_end", "session"}:
            return
        if peer_event_type == "agent_start":
            self._write_progress("[peer] agent started")
            return
        if peer_event_type == "agent_end":
            self._write_progress("[peer] agent finished")
            return
        if peer_event_type in {"tool_execution_start", "tool_execution_end"}:
            tool_name = _peer_event_tool_name(peer_event)
            action = "start" if peer_event_type.endswith("start") else "end"
            suffix = f" {tool_name}" if tool_name else ""
            self._write_progress(f"[peer] tool {action}{suffix}")
            return
        self._write_progress(f"[peer] event {peer_event_type}")

    def _write_progress(self, line: str) -> None:
        self._progress_stream.write(f"{line}\n")
        self._progress_stream.flush()

    def _resolved_prompt_preamble(self) -> str | None:
        if self._config.prompt_preamble:
            return self._config.prompt_preamble
        if self._config.location:
            return f"location={self._config.location}"
        return None


def _build_thread_create_payload(
    *,
    skill_name: str | None,
    skills: list[str] | None,
    capability_profile: str | None,
) -> dict[str, Any] | None:
    if skill_name is not None and skills is not None:
        raise ValueError("Provide either skill_name or skills, not both.")
    payload: dict[str, Any] = {}
    if skill_name is not None:
        payload["skill_name"] = skill_name
    if skills is not None:
        payload["skill_names"] = skills
    if capability_profile is not None:
        payload["capability_profile"] = capability_profile
    return payload or None


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
