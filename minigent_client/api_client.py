from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, TextIO, cast

from minigent_client.config import ClientConfig
from minigent_client.output import StreamProgressRenderer


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
        self._stream_progress_renderer = StreamProgressRenderer(self._progress_stream)

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

    def list_admin_threads(
        self,
        tenant_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        status: str | None = None,
        profile: str | None = None,
        skill: str | None = None,
        created_after: str | None = None,
        updated_after: str | None = None,
    ) -> dict[str, Any]:
        query = _build_query(
            {
                "limit": limit,
                "offset": offset,
                "status": status,
                "profile": profile,
                "skill": skill,
                "created_after": created_after,
                "updated_after": updated_after,
            }
        )
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/threads{query}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin thread-list response must be an object")
        return cast(dict[str, Any], response)

    def get_admin_thread(self, tenant_id: str, thread_id: str) -> dict[str, Any]:
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/threads/{thread_id}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin thread-detail response must be an object")
        return cast(dict[str, Any], response)

    def delete_admin_thread(self, tenant_id: str, thread_id: str) -> dict[str, Any]:
        response = self.request_json(
            "DELETE",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/threads/{thread_id}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin thread-delete response must be an object")
        return cast(dict[str, Any], response)

    def prune_admin_threads(
        self,
        tenant_id: str,
        *,
        updated_before: str,
        status: str | None = None,
        profile: str | None = None,
        skill: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        query = _build_query(
            {
                "updated_before": updated_before,
                "status": status,
                "profile": profile,
                "skill": skill,
                "dry_run": dry_run if dry_run else None,
            }
        )
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/threads/prune{query}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin thread-prune response must be an object")
        return cast(dict[str, Any], response)

    def list_admin_audit_records(
        self,
        tenant_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        action: str | None = None,
        actor: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> dict[str, Any]:
        query = _build_query(
            {
                "limit": limit,
                "offset": offset,
                "action": action,
                "actor": actor,
                "created_after": created_after,
                "created_before": created_before,
            }
        )
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/audit-records{query}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin audit-record-list response must be an object")
        return cast(dict[str, Any], response)

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
        for event in self.request_ndjson_events(
            "POST",
            f"{self._config.base_url}/threads/{thread_id}/run/stream",
        ):
            self._stream_progress_renderer.render(event)
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

    def _resolved_prompt_preamble(self) -> str | None:
        if self._config.prompt_preamble:
            return self._config.prompt_preamble
        if self._config.location:
            return f"location={self._config.location}"
        return None


def _build_query(params: dict[str, object | None]) -> str:
    clean_params = {key: value for key, value in params.items() if value is not None}
    if not clean_params:
        return ""
    return "?" + urllib.parse.urlencode(clean_params)


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
