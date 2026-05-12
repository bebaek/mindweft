from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

from fastapi import HTTPException

from app.execution import AGENT_BACKEND_NATIVE, AGENT_BACKEND_PEER_AGENT, TenantExecutionResolver
from app.models import Message, MessageRole, Principal, ThreadStatus
from app.peer_agents import PeerAgentRegistry
from app.runtime import AgentRuntime
from app.store import InMemoryThreadStore

_TERMINAL_PEER_STATUSES = {"completed", "failed", "canceled"}


class AgentBackend(ABC):
    @abstractmethod
    async def run_thread(self, principal: Principal, thread_id: str) -> str:
        raise NotImplementedError


class NativeAgentBackend(AgentBackend):
    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def run_thread(self, principal: Principal, thread_id: str) -> str:
        return await self._runtime.run_thread(principal, thread_id)


class AgentBackendRouter(AgentBackend):
    def __init__(
        self,
        *,
        store: InMemoryThreadStore,
        execution_resolver: TenantExecutionResolver,
        native_backend: NativeAgentBackend,
        peer_agent_registry: PeerAgentRegistry,
    ) -> None:
        self._store = store
        self._execution_resolver = execution_resolver
        self._native_backend = native_backend
        self._peer_agent_registry = peer_agent_registry

    async def run_thread(self, principal: Principal, thread_id: str) -> str:
        execution = self._execution_resolver.resolve(principal.tenant_id)
        backend = execution.config.agent_backend
        if backend.type == AGENT_BACKEND_NATIVE:
            return await self._native_backend.run_thread(principal, thread_id)
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
    ) -> str:
        self._store.start_run(principal.tenant_id, thread_id)
        try:
            prompt = self._prompt_for_peer_agent(principal, thread_id)
            task = await self._peer_agent_registry.create_task(peer, {"cwd": cwd, "prompt": prompt})
            task_id = str(task.get("task_id", "")).strip()
            if not task_id:
                raise HTTPException(
                    status_code=502,
                    detail="peer_agent backend returned task response without task_id",
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
            reply = self._reply_from_task(task)
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
