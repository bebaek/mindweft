from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.execution import (
    AGENT_BACKEND_NATIVE,
    AGENT_BACKEND_PEER_AGENT,
    TenantExecutionContext,
    TenantExecutionResolver,
    build_tool_registry_for_capability_profile,
    build_tool_registry_for_constraints,
    build_tool_registry_for_mcp_server_names,
    build_tool_registry_for_skill,
)
from app.mcp_broker import (
    MINIGENT_MCP_BROKER_BASE_URL_ENV,
    MINIGENT_MCP_BROKER_SESSION_ENV,
    MINIGENT_MCP_BROKER_TOKEN_ENV,
    MINIGENT_MCP_BROKER_URL_ENV,
    MCPBrokerSessionStore,
)
from app.models import Message, MessageRole, Principal, Thread, ThreadStatus
from app.peer_agents import PeerAgentRegistry
from app.redaction import sanitize_value_for_logging
from app.runtime import AgentRuntime, load_active_skill_instructions
from app.store import ThreadStore
from app.tools import ToolRegistry
from app.user_execution import (
    UserExecutionConfigSource,
    UserExecutionResolutionError,
    effective_execution_catalog,
    has_personal_execution_refs,
)

_TERMINAL_PEER_STATUSES = {"completed", "failed", "canceled"}
PEER_TOOL_ARG_ALLOWLIST_ENV = "MINIGENT_PEER_TOOL_ARG_ALLOWLIST"
_MAX_PEER_TOOL_ARGS_SUMMARY_CHARS = 80
_DEFAULT_SAFE_PEER_TOOL_ARG_FIELDS: dict[str, tuple[str, ...]] = {
    "read": ("path", "limit", "offset"),
    "grep": ("pattern", "path", "glob", "limit"),
    "find": ("pattern", "path", "limit"),
    "ls": ("path", "limit"),
}
_PEER_TOOL_ARG_ALLOW_ALL = "*"
_PEER_TOOL_ARGUMENT_KEYS = {"arguments", "args", "input", "params"}
RunEventSink = Callable[[dict[str, object]], Awaitable[None]]
MCPServerNameAuthorizer = Callable[[str, str], set[str] | None]


@dataclass(frozen=True)
class PeerBackendSettings:
    mcp_broker_base_url: str = "http://127.0.0.1:8000"
    safe_tool_arg_fields: dict[str, tuple[str, ...]] | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PeerBackendSettings:
        lookup = os.environ if env is None else env
        base_url = lookup.get(MINIGENT_MCP_BROKER_BASE_URL_ENV, "http://127.0.0.1:8000").rstrip("/")
        return cls(
            mcp_broker_base_url=base_url,
            safe_tool_arg_fields=_safe_peer_tool_arg_fields_from_raw(
                lookup.get(PEER_TOOL_ARG_ALLOWLIST_ENV, "")
            ),
        )


def peer_backend_settings_from_env() -> PeerBackendSettings:
    return PeerBackendSettings.from_env()


class AgentBackend(ABC):
    @abstractmethod
    async def run_thread(
        self,
        principal: Principal,
        thread_id: str,
        *,
        event_sink: RunEventSink | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        raise NotImplementedError


class NativeAgentBackend(AgentBackend):
    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def run_thread(
        self,
        principal: Principal,
        thread_id: str,
        *,
        event_sink: RunEventSink | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        return await self._runtime.run_thread(principal, thread_id, event_sink=event_sink)


class AgentBackendRouter(AgentBackend):
    def __init__(
        self,
        *,
        store: ThreadStore,
        execution_resolver: TenantExecutionResolver,
        native_backend: NativeAgentBackend,
        peer_agent_registry: PeerAgentRegistry,
        mcp_broker_sessions: MCPBrokerSessionStore | None = None,
        mcp_server_name_authorizer: MCPServerNameAuthorizer | None = None,
        user_execution_config_source: UserExecutionConfigSource | None = None,
        principal_execution_resolver: Callable[[Principal], TenantExecutionContext] | None = None,
        principal_tool_registry_provider: Callable[[Principal], ToolRegistry | None] | None = None,
    ) -> None:
        self._store = store
        self._execution_resolver = execution_resolver
        self._native_backend = native_backend
        self._peer_agent_registry = peer_agent_registry
        self._mcp_broker_sessions = mcp_broker_sessions
        self._mcp_server_name_authorizer = mcp_server_name_authorizer
        self._user_execution_config_source = user_execution_config_source
        self._principal_execution_resolver = principal_execution_resolver
        self._principal_tool_registry_provider = principal_tool_registry_provider

    def _resolve_execution(self, principal: Principal) -> TenantExecutionContext:
        if self._principal_execution_resolver is not None:
            return self._principal_execution_resolver(principal)
        return self._execution_resolver.resolve(principal.tenant_id)

    async def run_thread(
        self,
        principal: Principal,
        thread_id: str,
        *,
        event_sink: RunEventSink | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        execution = self._resolve_execution(principal)
        thread = self._store.get_thread(principal.tenant_id, thread_id)
        self._enforce_personal_execution_owner(principal, thread)
        backend = execution.config.agent_backend
        if backend.type == AGENT_BACKEND_NATIVE:
            return await self._native_backend.run_thread(
                principal, thread_id, event_sink=event_sink
            )
        if backend.type == AGENT_BACKEND_PEER_AGENT:
            if backend.peer is None or backend.cwd is None:
                raise HTTPException(status_code=500, detail="peer_agent backend is incomplete")
            if _thread_has_image_input(self._store, principal.tenant_id, thread_id):
                raise HTTPException(
                    status_code=400,
                    detail="peer_agent backend does not support image input",
                )
            return await self._run_peer_agent_thread(
                principal,
                thread_id,
                peer=backend.peer,
                cwd=backend.cwd,
                timeout_seconds=backend.timeout_seconds,
                poll_interval_seconds=backend.poll_interval_seconds,
                mcp_broker_enabled=backend.mcp_broker_enabled,
                event_sink=event_sink,
            )
        raise HTTPException(status_code=500, detail=f"Unsupported agent backend '{backend.type}'")

    async def _run_peer_agent_thread(
        self,
        principal: Principal,
        thread_id: str,
        *,
        peer: str,
        cwd: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
        mcp_broker_enabled: bool,
        event_sink: RunEventSink | None,
    ) -> tuple[str, dict[str, Any] | None]:
        self._store.start_run(principal.tenant_id, thread_id)
        broker_session_id: str | None = None
        task_id: str | None = None
        peer_task_terminal = False
        try:
            prompt = self._prompt_for_peer_agent(principal, thread_id)
            payload: dict[str, object] = {"cwd": cwd, "prompt": prompt}
            broker_env = (
                self._create_mcp_broker_env(
                    principal,
                    thread_id,
                    ttl_seconds=timeout_seconds + 60.0,
                )
                if mcp_broker_enabled
                else {}
            )
            if broker_env:
                broker_session_id = broker_env[MINIGENT_MCP_BROKER_SESSION_ENV]
                payload["env"] = broker_env
                payload["prompt"] = prompt + _mcp_broker_prompt_suffix()
            task_id = f"task_{uuid4().hex}"
            payload["task_id"] = task_id
            attached = self._store.attach_peer_task(
                principal.tenant_id,
                thread_id,
                peer_name=peer,
                peer_base_url=self._peer_agent_registry.agent_base_url(peer),
                task_id=task_id,
            )
            if not attached:
                raise HTTPException(status_code=409, detail="Thread run lease was lost")
            task = await self._peer_agent_registry.create_task(peer, payload)
            returned_task_id = str(task.get("task_id", "")).strip()
            if returned_task_id != task_id:
                raise HTTPException(
                    status_code=502,
                    detail="peer_agent backend returned an unexpected task_id",
                )
            await _emit_run_event(
                event_sink,
                {
                    "type": "peer.task.created",
                    "peer": peer,
                    "task_id": task_id,
                    "status": str(task.get("status", "")),
                },
            )
            last_peer_event_index = None
            if event_sink is not None:
                last_peer_event_index = await self._emit_peer_task_events(
                    event_sink,
                    principal=principal,
                    thread_id=thread_id,
                    peer=peer,
                    task_id=task_id,
                    after=None,
                )
            deadline = time.monotonic() + timeout_seconds
            while str(task.get("status", "")) not in _TERMINAL_PEER_STATUSES:
                if time.monotonic() >= deadline:
                    raise HTTPException(
                        status_code=504,
                        detail=f"peer_agent backend task '{task_id}' timed out",
                    )
                await asyncio.sleep(poll_interval_seconds)
                task = await self._peer_agent_registry.task(peer, task_id)
                await _emit_run_event(
                    event_sink,
                    {
                        "type": "peer.task.poll",
                        "peer": peer,
                        "task_id": task_id,
                        "status": str(task.get("status", "")),
                    },
                )
                if event_sink is not None:
                    last_peer_event_index = await self._emit_peer_task_events(
                        event_sink,
                        principal=principal,
                        thread_id=thread_id,
                        peer=peer,
                        task_id=task_id,
                        after=last_peer_event_index,
                    )
            peer_task_terminal = True
            if event_sink is not None:
                last_peer_event_index = await self._emit_peer_task_events(
                    event_sink,
                    principal=principal,
                    thread_id=thread_id,
                    peer=peer,
                    task_id=task_id,
                    after=last_peer_event_index,
                )
            else:
                self._persist_peer_task_events_tail(
                    principal,
                    thread_id,
                    peer=peer,
                    task_id=task_id,
                    task=task,
                )
            reply = self._reply_from_task(task)
            completed_event: dict[str, object] = {
                "type": "peer.task.completed",
                "peer": peer,
                "task_id": task_id,
                "status": str(task.get("status", "")),
            }
            usage = _usage_from_peer_task(task)
            if usage is not None:
                completed_event["usage"] = usage
            await _emit_run_event(event_sink, completed_event)
            self._store.append_message(
                principal.tenant_id,
                Message(thread_id=thread_id, role=MessageRole.ASSISTANT, content=reply),
            )
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.IDLE)
            return reply, None
        except asyncio.CancelledError:
            if task_id and not peer_task_terminal:
                await self._cancel_or_enqueue_peer_agent_task(principal, thread_id, peer, task_id)
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.IDLE)
            raise
        except HTTPException:
            if task_id and not peer_task_terminal:
                await self._cancel_or_enqueue_peer_agent_task(principal, thread_id, peer, task_id)
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.ERROR)
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            if task_id and not peer_task_terminal:
                await self._cancel_or_enqueue_peer_agent_task(principal, thread_id, peer, task_id)
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.ERROR)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            if broker_session_id is not None and self._mcp_broker_sessions is not None:
                self._mcp_broker_sessions.delete_session(broker_session_id)

    def _create_mcp_broker_env(
        self,
        principal: Principal,
        thread_id: str,
        *,
        ttl_seconds: float,
    ) -> dict[str, str]:
        if self._mcp_broker_sessions is None:
            return {}
        tool_registry = self.tool_registry_for_thread(principal, thread_id)
        session = self._mcp_broker_sessions.create_session(
            principal=principal,
            thread_id=thread_id,
            tool_registry=tool_registry,
            ttl_seconds=ttl_seconds,
        )
        base_url = peer_backend_settings_from_env().mcp_broker_base_url
        return {
            MINIGENT_MCP_BROKER_URL_ENV: f"{base_url}/mcp/peer/{session.session_id}",
            MINIGENT_MCP_BROKER_TOKEN_ENV: session.token,
            MINIGENT_MCP_BROKER_SESSION_ENV: session.session_id,
        }

    def tool_registry_for_thread(self, principal: Principal, thread_id: str) -> ToolRegistry:
        execution = self._resolve_execution(principal)
        thread = self._store.get_thread(principal.tenant_id, thread_id)
        skill_names = thread.skill_names
        if skill_names is None and thread.skill_name is not None:
            skill_names = [thread.skill_name]
        catalog = effective_execution_catalog(
            execution.config,
            self._user_execution_config_source if thread.execution_user_id is not None else None,
            tenant_id=principal.tenant_id,
            user_id=thread.execution_user_id or principal.user_id,
        )
        try:
            skills = catalog.resolve_skill_refs(
                skill_names, use_defaults=thread.execution_user_id is None
            )
            capability_profile = catalog.resolve_capability_profile(
                thread.capability_profile, use_default=thread.execution_user_id is None
            )
            personal_capability_constraints = (
                catalog.personal_capability_constraints(capability_profile)
                if capability_profile is not None and capability_profile.source == "user"
                else None
            )
        except UserExecutionResolutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        allowed_mcp_server_names = (
            self._mcp_server_name_authorizer(principal.tenant_id, principal.user_id)
            if self._mcp_server_name_authorizer is not None and not principal.is_admin
            else None
        )
        if personal_capability_constraints is not None:
            registry = build_tool_registry_for_constraints(
                execution.config,
                profile_allowed_local_tools=personal_capability_constraints.allowed_local_tools,
                profile_mcp_server_names=(personal_capability_constraints.shared_mcp_server_names),
                personal_mcp_servers=personal_capability_constraints.personal_mcp_servers,
                mcp_manager=execution.mcp_manager,
                allowed_mcp_server_names=allowed_mcp_server_names,
            )
        elif capability_profile is not None:
            registry = build_tool_registry_for_capability_profile(
                execution.config,
                capability_profile.stored_ref,
                mcp_manager=execution.mcp_manager,
                allowed_mcp_server_names=allowed_mcp_server_names,
            )
        elif len(skills) == 1 and (
            skills[0].config.allowed_local_tools is not None
            or skills[0].config.mcp_server_names is not None
        ):
            registry = build_tool_registry_for_skill(
                execution.config,
                skills[0].stored_ref,
                mcp_manager=execution.mcp_manager,
                allowed_mcp_server_names=allowed_mcp_server_names,
            )
        elif allowed_mcp_server_names is not None:
            registry = build_tool_registry_for_mcp_server_names(
                execution.config, allowed_mcp_server_names, mcp_manager=execution.mcp_manager
            )
        else:
            registry = execution.tool_registry
        if not isinstance(registry, ToolRegistry) or self._principal_tool_registry_provider is None:
            return registry
        principal_registry = self._principal_tool_registry_provider(principal)
        if principal_registry is None:
            return registry
        return ToolRegistry.combine(registry, principal_registry)

    @staticmethod
    def _enforce_personal_execution_owner(principal: Principal, thread: Thread) -> None:
        skill_names = thread.skill_names
        if skill_names is None and thread.skill_name is not None:
            skill_names = [thread.skill_name]
        if not has_personal_execution_refs(skill_names, thread.capability_profile):
            return
        if thread.execution_user_id is None or thread.execution_user_id != principal.user_id:
            raise HTTPException(
                status_code=403,
                detail="Personal execution resources belong to a different user",
            )

    def _prompt_for_peer_agent(self, principal: Principal, thread_id: str) -> str:
        messages = self._store.list_messages(principal.tenant_id, thread_id)
        context = self._store.get_thread_context(principal.tenant_id, thread_id)
        sections = [
            "You are running as the execution backend for a Minigent thread.",
            "Use the provided conversation as context and return the final assistant reply for the latest user request.",
        ]
        thread = self._store.get_thread(principal.tenant_id, thread_id)
        skill_names = thread.skill_names
        if skill_names is None and thread.skill_name is not None:
            skill_names = [thread.skill_name]
        execution = self._resolve_execution(principal)
        catalog = effective_execution_catalog(
            execution.config,
            self._user_execution_config_source if thread.execution_user_id is not None else None,
            tenant_id=principal.tenant_id,
            user_id=thread.execution_user_id or principal.user_id,
        )
        try:
            skills = catalog.resolve_skill_refs(
                skill_names, use_defaults=thread.execution_user_id is None
            )
        except UserExecutionResolutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if skills:
            sections.append(
                "Active skill instructions:\n"
                + "\n\n".join(
                    f"[{skill.config.name}]\n{load_active_skill_instructions(skill.config)}"
                    for skill in skills
                )
            )
        if context.summary:
            sections.append(f"Thread summary:\n{context.summary}")
        rendered_messages = [_render_peer_context_message(message) for message in messages]
        sections.append("Conversation:\n" + "\n\n".join(rendered_messages))
        sections.append(
            "Return only the final response text. If files were changed, summarize changed paths and verification."
        )
        return "\n\n".join(sections)

    async def _emit_peer_task_events(
        self,
        event_sink: RunEventSink | None,
        *,
        principal: Principal,
        thread_id: str,
        peer: str,
        task_id: str,
        after: int | None,
    ) -> int | None:
        try:
            response = await self._peer_agent_registry.task_events(peer, task_id, after=after)
        except HTTPException:
            return after
        events = response.get("events")
        if not isinstance(events, list):
            return after
        next_index = response.get("next_index")
        for _, sanitized_event in self._persist_peer_task_events(
            principal,
            thread_id,
            peer=peer,
            task_id=task_id,
            events=events,
        ):
            await _emit_run_event(
                event_sink,
                {
                    "type": "peer.task.event",
                    "peer": peer,
                    "task_id": task_id,
                    "event": sanitized_event,
                },
            )
        if isinstance(next_index, int) and next_index > 0:
            return next_index - 1
        return after

    def _persist_peer_task_events_tail(
        self,
        principal: Principal,
        thread_id: str,
        *,
        peer: str,
        task_id: str,
        task: dict[str, object],
    ) -> None:
        events = task.get("events_tail")
        if isinstance(events, list):
            self._persist_peer_task_events(
                principal,
                thread_id,
                peer=peer,
                task_id=task_id,
                events=events,
            )

    def _persist_peer_task_events(
        self,
        principal: Principal,
        thread_id: str,
        *,
        peer: str,
        task_id: str,
        events: list[object],
    ) -> list[tuple[dict[object, object], dict[str, object]]]:
        persisted: list[tuple[dict[object, object], dict[str, object]]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            sanitized_event = _sanitize_peer_task_event(event)
            self._persist_peer_tool_event(
                principal,
                thread_id,
                peer=peer,
                task_id=task_id,
                event=event,
                sanitized_event=sanitized_event,
            )
            persisted.append((event, sanitized_event))
        return persisted

    def _persist_peer_tool_event(
        self,
        principal: Principal,
        thread_id: str,
        *,
        peer: str,
        task_id: str,
        event: dict[object, object],
        sanitized_event: dict[str, object],
    ) -> None:
        event_type = str(sanitized_event.get("type") or "")
        if not event_type.startswith("tool_execution_"):
            return
        tool_name = str(sanitized_event.get("tool_name") or "").strip()
        if not tool_name:
            return
        tool_call_id = _peer_event_tool_call_id(event) or _peer_event_tool_call_id(sanitized_event)
        event_index = sanitized_event.get("index")
        if not tool_call_id:
            index_suffix = (
                event_index
                if isinstance(event_index, int)
                else len(self._store.list_messages(principal.tenant_id, thread_id))
            )
            tool_call_id = f"peer-{task_id}-{index_suffix}"
        if event_type.endswith("_start"):
            self._store.append_message(
                principal.tenant_id,
                Message(
                    thread_id=thread_id,
                    role=MessageRole.ASSISTANT,
                    content="",
                    metadata={"source": "peer_agent", "peer": peer, "task_id": task_id},
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    tool_arguments=_peer_tool_arguments_for_context(sanitized_event),
                ),
            )
            return
        if event_type.endswith("_end") or sanitized_event.get("status") in {
            "completed",
            "failed",
            "error",
        }:
            self._store.append_message(
                principal.tenant_id,
                Message(
                    thread_id=thread_id,
                    role=MessageRole.TOOL,
                    content=_peer_tool_result_for_context(sanitized_event),
                    metadata={"source": "peer_agent", "peer": peer, "task_id": task_id},
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                ),
            )

    def _reply_from_task(self, task: dict[str, object]) -> str:
        status = str(task.get("status", ""))
        final_output = str(task.get("final_output") or "").strip()
        if status == "completed":
            return final_output or str(task.get("stdout_tail") or "").strip()
        detail = final_output or str(task.get("stderr_tail") or "").strip() or status
        raise HTTPException(status_code=502, detail=f"peer_agent backend task {status}: {detail}")

    async def _cancel_or_enqueue_peer_agent_task(
        self,
        principal: Principal,
        thread_id: str,
        peer: str,
        task_id: str,
    ) -> None:
        if await self._cancel_peer_agent_task(peer, task_id):
            return
        self._store.enqueue_owned_peer_task_cancellation(principal.tenant_id, thread_id)

    async def _cancel_peer_agent_task(self, peer: str, task_id: str) -> bool:
        try:
            await self._peer_agent_registry.cancel_task(peer, task_id)
        except HTTPException:
            return False
        return True


def _render_peer_context_message(message: Message) -> str:
    if message.role == MessageRole.ASSISTANT and message.tool_name:
        lines = ["[assistant tool_call]", f"name: {message.tool_name}"]
        if message.tool_call_id:
            lines.append(f"id: {message.tool_call_id}")
        arguments = json.dumps(
            message.tool_arguments or {},
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        )
        lines.append(f"arguments: {arguments}")
        if message.content:
            lines.append(f"content: {message.content}")
        return "\n".join(lines)
    if message.role == MessageRole.TOOL:
        lines = ["[tool_result]", f"name: {message.tool_name or 'unknown'}"]
        if message.tool_call_id:
            lines.append(f"id: {message.tool_call_id}")
        lines.append(message.content)
        return "\n".join(lines)
    return f"[{message.role.value}]\n{message.content}"


def _thread_has_image_input(store: ThreadStore, tenant_id: str, thread_id: str) -> bool:
    return any(
        part.type == "image"
        for message in store.list_messages(tenant_id, thread_id)
        for part in (message.parts or [])
    )


def _usage_from_peer_task(task: dict[str, object]) -> dict[str, int] | None:
    usage = task.get("usage")
    if not isinstance(usage, dict):
        return None
    normalized: dict[str, int] = {}
    prompt_tokens = _usage_int(usage, "prompt_tokens", "input_tokens", "input")
    completion_tokens = _usage_int(usage, "completion_tokens", "output_tokens", "output")
    total_tokens = _usage_int(usage, "total_tokens", "totalTokens", "total")
    if prompt_tokens is not None:
        normalized["prompt_tokens"] = prompt_tokens
        normalized["input_tokens"] = prompt_tokens
    if completion_tokens is not None:
        normalized["completion_tokens"] = completion_tokens
        normalized["output_tokens"] = completion_tokens
    if total_tokens is not None:
        normalized["total_tokens"] = total_tokens
    elif prompt_tokens is not None and completion_tokens is not None:
        normalized["total_tokens"] = prompt_tokens + completion_tokens
    return normalized or None


def _usage_int(usage: dict[object, object], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                continue
    return None


def _sanitize_peer_task_event(event: dict[object, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    index = event.get("index")
    if isinstance(index, int):
        sanitized["index"] = index
    event_type = event.get("type") or event.get("event")
    if isinstance(event_type, str) and event_type:
        sanitized["type"] = event_type
    status = event.get("status")
    if isinstance(status, str) and status:
        sanitized["status"] = status
    tool_name = _peer_event_tool_name(event)
    if tool_name:
        sanitized["tool_name"] = tool_name
    if isinstance(event_type, str) and event_type.startswith("tool_execution_"):
        args_summary = _safe_peer_tool_args_summary(tool_name, event)
        if args_summary:
            sanitized["args_summary"] = args_summary
        sanitized.update(_sanitize_peer_tool_event_details(event))
    return sanitized or {"type": "event"}


def _sanitize_peer_tool_event_details(event: dict[object, object]) -> dict[str, object]:
    omitted_keys = {
        "index",
        "type",
        "event",
        "status",
        "tool_call_id",
        "toolCallId",
        "tool_name",
        "toolName",
        "name",
        "tool",
        # These Pi message containers can contain assistant draft/thinking text.
        "message",
        "messages",
        "assistantMessageEvent",
        "partial",
        "args_summary",
        *_PEER_TOOL_ARGUMENT_KEYS,
    }
    return {
        str(key): _strip_peer_tool_arguments(value)
        for key, value in event.items()
        if isinstance(key, str) and key not in omitted_keys and _is_json_like(value)
    }


def _strip_peer_tool_arguments(value: object) -> object:
    if isinstance(value, list):
        return [_strip_peer_tool_arguments(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_peer_tool_arguments(item)
            for key, item in value.items()
            if isinstance(key, str) and key not in _PEER_TOOL_ARGUMENT_KEYS
        }
    return value


def _safe_peer_tool_args_summary(tool_name: str, event: dict[object, object]) -> str:
    configured_fields = _safe_peer_tool_arg_fields()
    safe_fields = configured_fields.get(tool_name) or configured_fields.get(
        _PEER_TOOL_ARG_ALLOW_ALL
    )
    if not safe_fields:
        return ""
    arguments = _peer_event_tool_arguments(event)
    if not isinstance(arguments, dict):
        return ""
    fields = (
        tuple(str(field) for field in arguments)
        if _PEER_TOOL_ARG_ALLOW_ALL in safe_fields
        else safe_fields
    )
    parts: list[str] = []
    for field in fields:
        if field not in arguments:
            continue
        value = sanitize_value_for_logging(field, arguments[field])
        parts.append(f"{field}={_format_peer_tool_arg_value(value)}")
    text = ", ".join(parts)
    if len(text) <= _MAX_PEER_TOOL_ARGS_SUMMARY_CHARS:
        return text
    return text[: _MAX_PEER_TOOL_ARGS_SUMMARY_CHARS - 1] + "…"


def _safe_peer_tool_arg_fields() -> dict[str, tuple[str, ...]]:
    return peer_backend_settings_from_env().safe_tool_arg_fields or {}


def _safe_peer_tool_arg_fields_from_raw(raw_value: str) -> dict[str, tuple[str, ...]]:
    raw = raw_value.strip()
    if not raw:
        return _DEFAULT_SAFE_PEER_TOOL_ARG_FIELDS
    if raw.lower() in {"off", "none", "false", "0"}:
        return {}
    if raw.lower() in {"all", "*"}:
        return {_PEER_TOOL_ARG_ALLOW_ALL: (_PEER_TOOL_ARG_ALLOW_ALL,)}
    parsed = _parse_peer_tool_arg_allowlist_json(raw)
    if parsed is not None:
        return parsed
    parsed = _parse_peer_tool_arg_allowlist_spec(raw)
    if parsed is not None:
        return parsed
    return _DEFAULT_SAFE_PEER_TOOL_ARG_FIELDS


def _parse_peer_tool_arg_allowlist_json(raw: str) -> dict[str, tuple[str, ...]] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    parsed: dict[str, tuple[str, ...]] = {}
    for tool_name, fields in value.items():
        if not isinstance(tool_name, str) or not tool_name.strip():
            return None
        if not isinstance(fields, list):
            return None
        safe_fields: list[str] = []
        for field in fields:
            if not isinstance(field, str) or not field.strip():
                return None
            safe_fields.append(field.strip())
        parsed[tool_name.strip()] = tuple(safe_fields)
    return parsed


def _parse_peer_tool_arg_allowlist_spec(raw: str) -> dict[str, tuple[str, ...]] | None:
    parsed: dict[str, tuple[str, ...]] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            return None
        tool_name, raw_fields = entry.split(":", 1)
        tool_name = tool_name.strip()
        if not tool_name:
            return None
        fields = tuple(field.strip() for field in raw_fields.split(",") if field.strip())
        parsed[tool_name] = fields
    return parsed


def _peer_event_tool_arguments(event: Mapping[Any, Any]) -> object:
    for container in _peer_tool_argument_containers(event):
        for key in _PEER_TOOL_ARGUMENT_KEYS:
            if key in container:
                return container.get(key)
    return None


def _peer_event_tool_call_id(event: Mapping[Any, Any]) -> str:
    for container in _peer_tool_argument_containers(event):
        for key in ("tool_call_id", "toolCallId", "id", "call_id", "callId"):
            value = container.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _peer_tool_arguments_for_context(sanitized_event: dict[str, object]) -> dict[str, object]:
    arguments: dict[str, object] = {}
    args_summary = sanitized_event.get("args_summary")
    if isinstance(args_summary, str) and args_summary:
        arguments["summary"] = args_summary
    return arguments


def _peer_tool_result_for_context(sanitized_event: dict[str, object]) -> str:
    result_fields = {
        key: value
        for key, value in sanitized_event.items()
        if key not in {"index", "type", "tool_name", "args_summary"}
    }
    if not result_fields:
        result_fields = {"event": sanitized_event}
    return json.dumps(result_fields, ensure_ascii=True, sort_keys=True, default=str)


def _peer_tool_argument_containers(event: Mapping[Any, Any]) -> list[Mapping[Any, Any]]:
    containers = [event]
    for nested_key in ("tool_call", "toolCall", "tool_result", "toolResult"):
        nested = event.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)
    return containers


def _format_peer_tool_arg_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _is_json_like(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(_is_json_like(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_like(item) for key, item in value.items())
    return False


def _peer_event_tool_name(event: dict[object, object]) -> str:
    for key in ("tool_name", "toolName", "name", "tool"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    for nested_key in ("tool_call", "toolCall", "tool_result", "toolResult"):
        nested = event.get(nested_key)
        if isinstance(nested, dict):
            for key in ("name", "tool_name", "toolName", "tool"):
                value = nested.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""


async def _emit_run_event(
    event_sink: RunEventSink | None,
    event: dict[str, object],
) -> None:
    if event_sink is not None:
        await event_sink(event)


def _mcp_broker_prompt_suffix() -> str:
    return (
        "\n\nMinigent MCP broker:\n"
        f"- URL is available in ${MINIGENT_MCP_BROKER_URL_ENV}.\n"
        f"- Bearer token is available in ${MINIGENT_MCP_BROKER_TOKEN_ENV}.\n"
        "Use this broker only for tools needed to answer the current Minigent thread."
    )
