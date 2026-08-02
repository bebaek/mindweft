from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Any, Iterator, TextIO, cast

from minigent_client.config import ClientConfig
from minigent_client.errors import MinigentAPIError
from minigent_client.output import StreamProgressRenderer, TokenMode


def _resolve_token_mode(config_value: object, override: TokenMode | None) -> TokenMode:
    if override is not None:
        return override
    if config_value in {"auto", "live", "off"}:
        return cast(TokenMode, config_value)
    return "auto"


class MinigentAPIClient:
    def __init__(
        self,
        config: ClientConfig,
        output_stream: TextIO | None = None,
        progress_stream: TextIO | None = None,
        progress_verbose: bool = False,
        show_tool_results: bool | None = None,
        show_reasoning: bool | None = None,
        token_mode: TokenMode | None = None,
    ) -> None:
        self._config = config
        self._thread_id = config.thread_id
        self._output_stream = output_stream or sys.stdout
        self._progress_stream = progress_stream or sys.stderr
        self._stream_progress_renderer = StreamProgressRenderer(
            self._progress_stream,
            verbose=progress_verbose,
            show_tool_results=(
                bool(getattr(config, "show_tool_results", False))
                if show_tool_results is None
                else show_tool_results
            ),
            show_reasoning=(
                bool(getattr(config, "show_reasoning", False))
                if show_reasoning is None
                else show_reasoning
            ),
            token_mode=_resolve_token_mode(getattr(config, "token_mode", "auto"), token_mode),
        )

    def health(self) -> dict[str, Any]:
        response = self.request_json("GET", f"{self._config.base_url}/health")
        if not isinstance(response, dict):
            raise RuntimeError("Minigent health response must be an object")
        return cast(dict[str, Any], response)

    def config(self, *, export: bool = False) -> dict[str, Any]:
        url = f"{self._config.base_url}/config"
        if export:
            url = f"{url}?export=true"
        response = self.request_json("GET", url)
        if not isinstance(response, dict):
            raise RuntimeError("Minigent config response must be an object")
        return cast(dict[str, Any], response)

    def execution_options(self) -> dict[str, Any]:
        response = self.request_json("GET", f"{self._config.base_url}/execution-options")
        if not isinstance(response, dict):
            raise RuntimeError("Minigent execution-options response must be an object")
        return cast(dict[str, Any], response)

    def list_admin_tenants(
        self,
        *,
        limit: int | None = None,
        offset: int | None = None,
        status: str | None = None,
        plan: str | None = None,
        slug: str | None = None,
    ) -> dict[str, Any]:
        query = _build_query(
            {
                "limit": limit,
                "offset": offset,
                "status": status,
                "plan": plan,
                "slug": slug,
            }
        )
        response = self.request_json("GET", f"{self._config.base_url}/admin/tenants{query}")
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-list response must be an object")
        return cast(dict[str, Any], response)

    def create_admin_tenant(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/admin/tenants",
            payload=payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-create response must be an object")
        return cast(dict[str, Any], response)

    def get_admin_tenant(self, tenant_id: str) -> dict[str, Any]:
        response = self.request_json("GET", f"{self._config.base_url}/admin/tenants/{tenant_id}")
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant response must be an object")
        return cast(dict[str, Any], response)

    def update_admin_tenant(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.request_json(
            "PATCH",
            f"{self._config.base_url}/admin/tenants/{tenant_id}",
            payload=payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-update response must be an object")
        return cast(dict[str, Any], response)

    def transition_admin_tenant(self, tenant_id: str, transition: str) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/{transition}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-transition response must be an object")
        return cast(dict[str, Any], response)

    def delete_admin_tenant(self, tenant_id: str) -> dict[str, Any]:
        response = self.request_json("DELETE", f"{self._config.base_url}/admin/tenants/{tenant_id}")
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-delete response must be an object")
        return cast(dict[str, Any], response)

    def seed_admin_tenants(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/admin/tenants/seed",
            payload=payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-seed response must be an object")
        return cast(dict[str, Any], response)

    def list_admin_tenant_users(
        self,
        tenant_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        status: str | None = None,
        role: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        query = _build_query(
            {
                "limit": limit,
                "offset": offset,
                "status": status,
                "role": role,
                "email": email,
            }
        )
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/users{query}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-user-list response must be an object")
        return cast(dict[str, Any], response)

    def create_admin_tenant_user(self, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/users",
            payload=payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-user-create response must be an object")
        return cast(dict[str, Any], response)

    def get_admin_tenant_user(self, tenant_id: str, user_record_id: str) -> dict[str, Any]:
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/users/{user_record_id}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-user response must be an object")
        return cast(dict[str, Any], response)

    def update_admin_tenant_user(
        self, tenant_id: str, user_record_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.request_json(
            "PATCH",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/users/{user_record_id}",
            payload=payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-user-update response must be an object")
        return cast(dict[str, Any], response)

    def transition_admin_tenant_user(
        self, tenant_id: str, user_record_id: str, transition: str
    ) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/users/{user_record_id}/{transition}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-user-transition response must be an object")
        return cast(dict[str, Any], response)

    def delete_admin_tenant_user(self, tenant_id: str, user_record_id: str) -> dict[str, Any]:
        response = self.request_json(
            "DELETE",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/users/{user_record_id}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin tenant-user-delete response must be an object")
        return cast(dict[str, Any], response)

    def get_admin_tenant_entitlements(self, tenant_id: str) -> dict[str, Any]:
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/entitlements",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin entitlements response must be an object")
        return cast(dict[str, Any], response)

    def put_admin_tenant_entitlements(
        self, tenant_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.request_json(
            "PUT",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/entitlements",
            payload=payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin entitlement update response must be an object")
        return cast(dict[str, Any], response)

    def validate_admin_tenant_entitlements(
        self, tenant_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/entitlements/validate",
            payload=payload,
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin entitlement validation response must be an object")
        return cast(dict[str, Any], response)

    def delete_admin_tenant_entitlements(self, tenant_id: str) -> None:
        self.request_json(
            "DELETE",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/entitlements",
        )

    def list_admin_execution_config_tenants(self) -> list[str]:
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/admin/execution-config-tenants",
        )
        if not isinstance(response, dict):
            raise RuntimeError(
                "Minigent admin execution-config tenant-list response must be an object"
            )
        tenants = response.get("tenants")
        if not isinstance(tenants, list) or not all(isinstance(item, str) for item in tenants):
            raise RuntimeError(
                "Minigent admin execution-config tenant-list response must include tenants"
            )
        return cast(list[str], tenants)

    def get_admin_tenant_execution_config(self, tenant_id: str) -> dict[str, Any]:
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/execution-config",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin execution-config response must be an object")
        return cast(dict[str, Any], response)

    def put_admin_tenant_execution_config(
        self, tenant_id: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.request_json(
            "PUT",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/execution-config",
            payload={"config": config},
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent admin execution-config update response must be an object")
        return cast(dict[str, Any], response)

    def validate_admin_tenant_execution_config(
        self, tenant_id: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/admin/tenants/{tenant_id}/execution-config/validate",
            payload={"config": config},
        )
        if not isinstance(response, dict):
            raise RuntimeError(
                "Minigent admin execution-config validation response must be an object"
            )
        return cast(dict[str, Any], response)

    def create_thread(
        self,
        *,
        skill_name: str | None = None,
        skills: list[str] | None = None,
        capability_profile: str | None = None,
        llm_profile: str | None = None,
    ) -> dict[str, Any]:
        payload = _build_thread_create_payload(
            skill_name=skill_name,
            skills=skills,
            capability_profile=capability_profile,
            llm_profile=llm_profile,
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

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    def set_thread_id(self, thread_id: str | None) -> None:
        self._thread_id = thread_id

    def set_debug_enabled(self, enabled: bool) -> None:
        self._config = replace(self._config, debug_show_prompt=enabled)
        self._stream_progress_renderer.set_verbose(enabled)

    def flush_pending_token_summary(self) -> None:
        """Flush any deferred "done · tokens …" line to the progress stream."""
        self._stream_progress_renderer.flush_pending_summary()

    def add_message(
        self,
        thread_id: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        parts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content}
        uploaded_parts, uploaded_attachment_ids = self._upload_inline_image_parts(thread_id, parts)
        if uploaded_parts is not None:
            payload["parts"] = uploaded_parts
        if metadata is not None:
            payload["metadata"] = metadata
        try:
            response = self.request_json(
                "POST",
                f"{self._config.base_url}/threads/{thread_id}/messages",
                payload=payload,
            )
        except Exception:
            self._discard_uploaded_attachments(thread_id, uploaded_attachment_ids)
            raise
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

    def upload_attachment(
        self,
        thread_id: str,
        *,
        mime_type: str,
        data: str,
    ) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{thread_id}/attachments",
            payload={"mime_type": mime_type, "data": data},
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent attachment response must be an object")
        return cast(dict[str, Any], response)

    def delete_attachment(self, thread_id: str, attachment_id: str) -> None:
        self.request_json(
            "DELETE",
            f"{self._config.base_url}/threads/{thread_id}/attachments/{attachment_id}",
        )

    def send_user_message(
        self, content: str, *, parts: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        thread_id = self.ensure_thread()
        formatted_content = self._format_user_message(content)
        formatted_parts = _parts_with_formatted_text(parts, formatted_content)
        self._maybe_log_prompt(formatted_content)
        return self.add_message(
            thread_id,
            formatted_content,
            metadata={"raw_user_prompt": content},
            parts=formatted_parts,
        )

    def _upload_inline_image_parts(
        self,
        thread_id: str,
        parts: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]] | None, list[str]]:
        if parts is None:
            return None, []
        uploaded: list[dict[str, Any]] = []
        attachment_ids: list[str] = []
        try:
            for part in parts:
                clean_part = dict(part)
                data = clean_part.get("data")
                if clean_part.get("type") != "image" or not isinstance(data, str) or not data:
                    uploaded.append(clean_part)
                    continue
                mime_type = clean_part.get("mime_type")
                if not isinstance(mime_type, str) or not mime_type:
                    raise RuntimeError("Image part must include mime_type")
                attachment = self.upload_attachment(
                    thread_id,
                    mime_type=mime_type,
                    data=data,
                )
                attachment_id = attachment.get("attachment_id")
                if not isinstance(attachment_id, str) or not attachment_id:
                    raise RuntimeError("Minigent attachment response must include attachment_id")
                attachment_ids.append(attachment_id)
                clean_part.pop("data", None)
                clean_part["attachment_id"] = attachment_id
                uploaded.append(clean_part)
        except Exception:
            self._discard_uploaded_attachments(thread_id, attachment_ids)
            raise
        return uploaded, attachment_ids

    def _discard_uploaded_attachments(self, thread_id: str, attachment_ids: list[str]) -> None:
        for attachment_id in attachment_ids:
            try:
                self.delete_attachment(thread_id, attachment_id)
            except Exception:
                pass

    def run_thread(
        self, thread_id: str | None = None, *, stream: bool | None = None
    ) -> tuple[str, dict[str, Any] | None]:
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
        return reply, None

    def list_pending_private_value_consents(
        self, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        resolved_thread_id = thread_id or self.ensure_thread()
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/threads/{resolved_thread_id}/private-value-consents/pending",
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise RuntimeError("Minigent pending-consent response must be an array of objects")
        return cast(list[dict[str, Any]], response)

    def decide_private_value_consent(
        self,
        consent_id: str,
        *,
        approve: bool,
        one_shot: bool = True,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_thread_id = thread_id or self.ensure_thread()
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{resolved_thread_id}/private-value-consents/{consent_id}",
            payload={"approve": approve, "one_shot": one_shot},
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent consent-decision response must be an object")
        return cast(dict[str, Any], response)

    def resume_private_value_consent(
        self,
        consent_id: str,
        *,
        thread_id: str | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        resolved_thread_id = thread_id or self.ensure_thread()
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{resolved_thread_id}/private-value-consents/{consent_id}/resume",
        )
        if not isinstance(response, dict) or not isinstance(response.get("reply"), str):
            raise RuntimeError("Minigent consent-resume response must include a reply")
        return response["reply"], None

    def list_private_value_actions(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        resolved_thread_id = thread_id or self.ensure_thread()
        response = self.request_json(
            "GET",
            f"{self._config.base_url}/threads/{resolved_thread_id}/private-value-actions",
        )
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise RuntimeError("Minigent private-value action response must be an array of objects")
        return cast(list[dict[str, Any]], response)

    def discard_private_value_action(
        self,
        consent_id: str,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_thread_id = thread_id or self.ensure_thread()
        response = self.request_json(
            "DELETE",
            f"{self._config.base_url}/threads/{resolved_thread_id}/private-value-actions/{consent_id}",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent discarded-action response must be an object")
        return cast(dict[str, Any], response)

    def compact_thread(self, thread_id: str) -> dict[str, Any]:
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{thread_id}/compact",
        )
        if not isinstance(response, dict):
            raise RuntimeError("Minigent compact-thread response must be an object")
        return cast(dict[str, Any], response)

    def cancel_current_run(self, thread_id: str | None = None) -> dict[str, Any] | None:
        resolved_thread_id = thread_id or self._thread_id
        if resolved_thread_id is None:
            return None
        response = self.request_json(
            "POST",
            f"{self._config.base_url}/threads/{resolved_thread_id}/run/cancel",
        )
        if response is None:
            return None
        if not isinstance(response, dict):
            raise RuntimeError("Minigent cancel-run response must be an object")
        return cast(dict[str, Any], response)

    def _run_thread_stream(self, thread_id: str) -> tuple[str, dict[str, Any] | None]:
        reply: str | None = None
        metadata: dict[str, Any] | None = None
        saw_warning = False
        try:
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
                    metadata = event.get("metadata")
                elif event_type == "run.warning":
                    saw_warning = True
                elif event_type == "run.error":
                    status_code = event.get("status_code")
                    detail = event.get("detail")
                    raise _api_error_from_status(
                        "POST",
                        f"{self._config.base_url}/threads/{thread_id}/run/stream",
                        int(status_code) if isinstance(status_code, int) else None,
                        detail,
                        technical_detail=f"run.error event: status_code={status_code} detail={detail}",
                    )
        finally:
            self._stream_progress_renderer.stop_active_progress()
        if reply is None:
            if saw_warning:
                return "", metadata
            raise RuntimeError("Minigent run stream ended without an assistant message")
        return reply, metadata

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
            raise _api_error_from_status(
                method,
                url,
                exc.code,
                _extract_error_detail(body),
                technical_detail=f"{method} {url} failed: {exc.code} {body}",
            ) from exc
        except urllib.error.URLError as exc:
            raise _api_error_from_url_error(method, url, exc) from exc
        except TimeoutError as exc:
            raise _timeout_error(method, url, str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise MinigentAPIError(
                "The Minigent API returned malformed streaming data.",
                category="malformed_response",
                detail=f"{method} {url} returned invalid NDJSON: {exc}",
            ) from exc

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
            raise _api_error_from_status(
                method,
                url,
                exc.code,
                _extract_error_detail(body),
                technical_detail=f"{method} {url} failed: {exc.code} {body}",
            ) from exc
        except urllib.error.URLError as exc:
            raise _api_error_from_url_error(method, url, exc) from exc
        except TimeoutError as exc:
            raise _timeout_error(method, url, str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise MinigentAPIError(
                "The Minigent API returned malformed JSON.",
                category="malformed_response",
                detail=f"{method} {url} returned invalid JSON: {exc}",
            ) from exc

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


def _extract_error_detail(body: str) -> object:
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if detail is not None:
            return detail
    return body.strip()


def _structured_error_message(detail: object) -> str | None:
    if not isinstance(detail, dict):
        return None
    message = detail.get("message")
    return message if isinstance(message, str) and message else None


def _structured_error_type(detail: object) -> str | None:
    if not isinstance(detail, dict):
        return None
    error_type = detail.get("type")
    return error_type if isinstance(error_type, str) and error_type else None


def _detail_to_text(detail: object) -> str:
    if isinstance(detail, str):
        return detail
    if detail is None:
        return ""
    return json.dumps(detail, sort_keys=True)


def _api_error_from_status(
    method: str,
    url: str,
    status_code: int | None,
    detail: object,
    *,
    technical_detail: str,
) -> MinigentAPIError:
    detail_text = _detail_to_text(detail)
    structured_message = _structured_error_message(detail)
    structured_type = _structured_error_type(detail)
    if structured_message is not None:
        return MinigentAPIError(
            structured_message,
            category=structured_type or "request_failed",
            detail=technical_detail,
            status_code=status_code,
        )
    if status_code == 401:
        return MinigentAPIError(
            "Authentication failed. Check MINIGENT_API_TOKEN or your Minigent principal headers.",
            category="authentication_failed",
            detail=technical_detail,
            status_code=status_code,
        )
    if status_code == 403:
        return MinigentAPIError(
            "Permission denied. Your Minigent principal is not allowed to perform this action.",
            category="permission_denied",
            detail=technical_detail,
            status_code=status_code,
        )
    if status_code == 404:
        message = "Minigent resource not found. Check the thread ID or base URL."
        if detail_text:
            message = f"{message} {detail_text}"
        return MinigentAPIError(
            message,
            category="not_found",
            detail=technical_detail,
            status_code=status_code,
        )
    if status_code in {408, 504}:
        return MinigentAPIError(
            "The Minigent request timed out. Try again or check the API server.",
            category="timeout",
            detail=technical_detail,
            status_code=status_code,
        )
    if status_code is not None and status_code >= 500:
        message = f"Minigent server error ({status_code})."
        if detail_text:
            message = f"{message} {detail_text}"
        return MinigentAPIError(
            message,
            category="server_error",
            detail=technical_detail,
            status_code=status_code,
        )
    message = "Minigent request failed."
    if status_code is not None:
        message = f"Minigent request failed ({status_code})."
    if detail_text:
        message = f"{message} {detail_text}"
    return MinigentAPIError(
        message,
        category="request_failed",
        detail=technical_detail,
        status_code=status_code,
    )


def _api_error_from_url_error(
    method: str, url: str, exc: urllib.error.URLError
) -> MinigentAPIError:
    reason = exc.reason
    reason_text = str(reason)
    if isinstance(reason, TimeoutError) or "timed out" in reason_text.lower():
        return _timeout_error(method, url, reason_text)
    return MinigentAPIError(
        "Cannot reach the Minigent API. Check --base-url and make sure the server is running.",
        category="server_unavailable",
        detail=f"{method} {url} failed: {reason_text}",
    )


def _timeout_error(method: str, url: str, detail: str) -> MinigentAPIError:
    return MinigentAPIError(
        "The Minigent request timed out. Try again or check the API server.",
        category="timeout",
        detail=f"{method} {url} timed out: {detail}",
    )


def _parts_with_formatted_text(
    parts: list[dict[str, Any]] | None, formatted_content: str
) -> list[dict[str, Any]] | None:
    if parts is None:
        return None
    formatted_parts = [dict(part) for part in parts]
    for part in formatted_parts:
        if part.get("type") == "text":
            part["text"] = formatted_content
            return formatted_parts
    if formatted_content:
        return [{"type": "text", "text": formatted_content}, *formatted_parts]
    return formatted_parts


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
    llm_profile: str | None,
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
    if llm_profile is not None:
        payload["llm_profile"] = llm_profile
    return payload or None
