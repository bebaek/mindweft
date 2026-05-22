from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from app.execution import (
    AGENT_BACKEND_NATIVE,
    AGENT_BACKEND_PEER_AGENT,
    TenantExecutionResolver,
    build_tool_registry_for_capability_profile,
    build_tool_registry_for_skill,
    get_capability_profile,
    get_skill_configs,
)
from app.mcp_broker import (
    MINIGENT_MCP_BROKER_BASE_URL_ENV,
    MINIGENT_MCP_BROKER_SESSION_ENV,
    MINIGENT_MCP_BROKER_TOKEN_ENV,
    MINIGENT_MCP_BROKER_URL_ENV,
    MCPBrokerSessionStore,
)
from app.models import Message, MessageRole, Principal, ThreadStatus
from app.peer_agents import PeerAgentRegistry
from app.redaction import sanitize_value_for_logging
from app.runtime import AgentRuntime
from app.store import ThreadStore

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


class AgentBackend(ABC):
    @abstractmethod
    async def run_thread(
        self,
        principal: Principal,
        thread_id: str,
        *,
        event_sink: RunEventSink | None = None,
    ) -> str:
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
    ) -> str:
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
    ) -> None:
        self._store = store
        self._execution_resolver = execution_resolver
        self._native_backend = native_backend
        self._peer_agent_registry = peer_agent_registry
        self._mcp_broker_sessions = mcp_broker_sessions

    async def run_thread(
        self,
        principal: Principal,
        thread_id: str,
        *,
        event_sink: RunEventSink | None = None,
    ) -> str:
        execution = self._execution_resolver.resolve(principal.tenant_id)
        backend = execution.config.agent_backend
        if backend.type == AGENT_BACKEND_NATIVE:
            return await self._native_backend.run_thread(principal, thread_id, event_sink=event_sink)
        if backend.type == AGENT_BACKEND_PEER_AGENT:
            if backend.peer is None or backend.cwd is None:
                raise HTTPException(status_code=500, detail="peer_agent backend is incomplete")
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
    ) -> str:
        self._store.start_run(principal.tenant_id, thread_id)
        broker_session_id: str | None = None
        task_id: str | None = None
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
            task = await self._peer_agent_registry.create_task(peer, payload)
            task_id = str(task.get("task_id", "")).strip()
            await _emit_run_event(
                event_sink,
                {
                    "type": "peer.task.created",
                    "peer": peer,
                    "task_id": task_id,
                    "status": str(task.get("status", "")),
                },
            )
            if not task_id:
                raise HTTPException(
                    status_code=502,
                    detail="peer_agent backend returned task response without task_id",
                )
            last_peer_event_index = await self._emit_peer_task_events(
                event_sink,
                peer=peer,
                task_id=task_id,
                after=None,
            )
            deadline = time.monotonic() + timeout_seconds
            while str(task.get("status", "")) not in _TERMINAL_PEER_STATUSES:
                if time.monotonic() >= deadline:
                    await self._cancel_peer_agent_task(peer, task_id)
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
                last_peer_event_index = await self._emit_peer_task_events(
                    event_sink,
                    peer=peer,
                    task_id=task_id,
                    after=last_peer_event_index,
                )
            last_peer_event_index = await self._emit_peer_task_events(
                event_sink,
                peer=peer,
                task_id=task_id,
                after=last_peer_event_index,
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
            return reply
        except asyncio.CancelledError:
            if task_id:
                await self._cancel_peer_agent_task(peer, task_id)
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.IDLE)
            raise
        except HTTPException:
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.ERROR)
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
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
        execution = self._execution_resolver.resolve(principal.tenant_id)
        thread = self._store.get_thread(principal.tenant_id, thread_id)
        skill_names = thread.skill_names
        if skill_names is None and thread.skill_name is not None:
            skill_names = [thread.skill_name]
        skills = get_skill_configs(execution.config, skill_names)
        capability_profile = get_capability_profile(execution.config, thread.capability_profile)
        if capability_profile is not None:
            tool_registry = build_tool_registry_for_capability_profile(
                execution.config,
                thread.capability_profile,
                mcp_manager=execution.mcp_manager,
            )
        elif len(skills) == 1 and (
            skills[0].allowed_local_tools is not None or skills[0].mcp_server_names is not None
        ):
            tool_registry = build_tool_registry_for_skill(
                execution.config,
                skills[0].name,
                mcp_manager=execution.mcp_manager,
            )
        else:
            tool_registry = execution.tool_registry
        session = self._mcp_broker_sessions.create_session(
            principal=principal,
            thread_id=thread_id,
            tool_registry=tool_registry,
            ttl_seconds=ttl_seconds,
        )
        base_url = os.getenv(MINIGENT_MCP_BROKER_BASE_URL_ENV, "http://127.0.0.1:8000").rstrip("/")
        return {
            MINIGENT_MCP_BROKER_URL_ENV: f"{base_url}/mcp/peer/{session.session_id}",
            MINIGENT_MCP_BROKER_TOKEN_ENV: session.token,
            MINIGENT_MCP_BROKER_SESSION_ENV: session.session_id,
        }

    def _prompt_for_peer_agent(self, principal: Principal, thread_id: str) -> str:
        messages = self._store.list_messages(principal.tenant_id, thread_id)
        context = self._store.get_thread_context(principal.tenant_id, thread_id)
        sections = [
            "You are running as the execution backend for a Minigent thread.",
            "Use the provided conversation as context and return the final assistant reply for the latest user request.",
        ]
        if context.summary:
            sections.append(f"Thread summary:\n{context.summary}")
        rendered_messages = []
        for message in messages:
            if message.role == MessageRole.TOOL:
                label = f"tool:{message.tool_name or 'unknown'}"
            else:
                label = message.role.value
            rendered_messages.append(f"[{label}]\n{message.content}")
        sections.append("Conversation:\n" + "\n\n".join(rendered_messages))
        sections.append(
            "Return only the final response text. If files were changed, summarize changed paths and verification."
        )
        return "\n\n".join(sections)

    async def _emit_peer_task_events(
        self,
        event_sink: RunEventSink | None,
        *,
        peer: str,
        task_id: str,
        after: int | None,
    ) -> int | None:
        if event_sink is None:
            return after
        try:
            response = await self._peer_agent_registry.task_events(peer, task_id, after=after)
        except HTTPException:
            return after
        events = response.get("events")
        if not isinstance(events, list):
            return after
        next_index = response.get("next_index")
        for event in events:
            if not isinstance(event, dict):
                continue
            await _emit_run_event(
                event_sink,
                {
                    "type": "peer.task.event",
                    "peer": peer,
                    "task_id": task_id,
                    "event": _sanitize_peer_task_event(event),
                },
            )
        if isinstance(next_index, int) and next_index > 0:
            return next_index - 1
        return after

    def _reply_from_task(self, task: dict[str, object]) -> str:
        status = str(task.get("status", ""))
        final_output = str(task.get("final_output") or "").strip()
        if status == "completed":
            return final_output or str(task.get("stdout_tail") or "").strip()
        detail = final_output or str(task.get("stderr_tail") or "").strip() or status
        raise HTTPException(status_code=502, detail=f"peer_agent backend task {status}: {detail}")

    async def _cancel_peer_agent_task(self, peer: str, task_id: str) -> None:
        try:
            await self._peer_agent_registry.cancel_task(peer, task_id)
        except HTTPException:
            return


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
    safe_fields = configured_fields.get(tool_name) or configured_fields.get(_PEER_TOOL_ARG_ALLOW_ALL)
    if not safe_fields:
        return ""
    arguments = _peer_event_tool_arguments(event)
    if not isinstance(arguments, dict):
        return ""
    fields = tuple(str(field) for field in arguments) if _PEER_TOOL_ARG_ALLOW_ALL in safe_fields else safe_fields
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
    raw = os.getenv(PEER_TOOL_ARG_ALLOWLIST_ENV, "").strip()
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


def _peer_event_tool_arguments(event: dict[object, object]) -> object:
    for container in _peer_tool_argument_containers(event):
        for key in _PEER_TOOL_ARGUMENT_KEYS:
            if key in container:
                return container.get(key)
    return None


def _peer_tool_argument_containers(event: dict[object, object]) -> list[dict[object, object]]:
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
