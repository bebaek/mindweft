from __future__ import annotations

import asyncio
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
from app.runtime import AgentRuntime
from app.store import ThreadStore

_TERMINAL_PEER_STATUSES = {"completed", "failed", "canceled"}
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
            await _emit_run_event(
                event_sink,
                {
                    "type": "peer.task.completed",
                    "peer": peer,
                    "task_id": task_id,
                    "status": str(task.get("status", "")),
                },
            )
            self._store.append_message(
                principal.tenant_id,
                Message(thread_id=thread_id, role=MessageRole.ASSISTANT, content=reply),
            )
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.IDLE)
            return reply
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
    return sanitized or {"type": "event"}


def _peer_event_tool_name(event: dict[object, object]) -> str:
    for key in ("tool_name", "name", "tool"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    tool_call = event.get("tool_call")
    if isinstance(tool_call, dict):
        for key in ("name", "tool_name"):
            value = tool_call.get(key)
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
