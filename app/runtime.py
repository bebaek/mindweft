from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from app.execution import (
    FixedTenantExecutionResolver,
    TenantExecutionResolver,
    build_tool_registry_for_skill,
    get_skill_config,
)
from app.llm import LLMAdapter, serialize_tool_result
from app.models import LLMResponse, Message, MessageRole, Principal, ThreadStatus
from app.store import InMemoryThreadStore
from app.tools import ToolRegistry

RUNTIME_SYSTEM_PROMPT = (
    "Use tools when they are relevant and ground claims in tool results. "
    "Distinguish clearly between direct verification and inference. "
    "Do not claim a live status, current availability, or real-time confirmation unless a tool result directly confirms it. "
    "If tool results fail, are indirect, or are insufficient, say that you could not directly verify the answer and explain what you were able to infer."
)


class AgentRuntime:
    def __init__(
        self,
        store: InMemoryThreadStore,
        execution_resolver: TenantExecutionResolver | None = None,
        llm_adapter: LLMAdapter | None = None,
        tool_registry: ToolRegistry | None = None,
        max_iterations: int = 8,
    ) -> None:
        self._store = store
        if execution_resolver is not None:
            self._execution_resolver = execution_resolver
        elif llm_adapter is not None and tool_registry is not None:
            self._execution_resolver = FixedTenantExecutionResolver(llm_adapter, tool_registry)
        else:
            raise ValueError(
                "AgentRuntime requires execution_resolver or both llm_adapter and tool_registry"
            )
        self._max_iterations = max_iterations

    async def run_thread(self, principal: Principal, thread_id: str) -> str:
        self._store.start_run(principal.tenant_id, thread_id)
        failed_tool_calls: set[str] = set()
        execution = self._execution_resolver.resolve(principal.tenant_id)
        thread = self._store.get_thread(principal.tenant_id, thread_id)
        skill = get_skill_config(execution.config, thread.skill_name)
        tool_registry = (
            build_tool_registry_for_skill(execution.config, thread.skill_name)
            if skill is not None
            else execution.tool_registry
        )
        try:
            for _ in range(self._max_iterations):
                messages = self._messages_for_llm(principal, thread_id, skill_prompt=skill.system_prompt if skill else None)
                response = await execution.llm_adapter.generate(
                    messages, tool_registry.specs()
                )
                if response.tool_call is not None:
                    await self._handle_tool_call(
                        principal,
                        thread_id,
                        response,
                        failed_tool_calls,
                        tool_registry=tool_registry,
                    )
                    continue

                if response.content is None:
                    raise HTTPException(
                        status_code=500, detail="LLM returned neither content nor tool call"
                    )

                self._store.append_message(
                    principal.tenant_id,
                    Message(
                        thread_id=thread_id, role=MessageRole.ASSISTANT, content=response.content
                    )
                )
                self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.IDLE)
                return response.content
        except HTTPException:
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.ERROR)
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.ERROR)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        self._store.set_thread_status(principal.tenant_id, thread_id, ThreadStatus.ERROR)
        raise HTTPException(status_code=500, detail="Agent exceeded maximum tool iterations")

    async def _handle_tool_call(
        self,
        principal: Principal,
        thread_id: str,
        response: LLMResponse,
        failed_tool_calls: set[str],
        *,
        tool_registry: ToolRegistry,
    ) -> None:
        tool_call = response.tool_call
        if tool_call is None:
            return
        tool_call_signature = _tool_call_signature(tool_call.name, tool_call.arguments)
        self._store.append_message(
            principal.tenant_id,
            Message(
                thread_id=thread_id,
                role=MessageRole.ASSISTANT,
                content="",
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                tool_arguments=tool_call.arguments,
            )
        )
        if tool_call_signature in failed_tool_calls:
            result = _serialize_tool_error(
                tool_call.name,
                HTTPException(
                    status_code=409,
                    detail="Repeated failed tool call blocked for identical arguments",
                ),
                blocked=True,
            )
        else:
            try:
                result = await tool_registry.execute(tool_call.name, tool_call.arguments)
            except HTTPException as exc:
                failed_tool_calls.add(tool_call_signature)
                result = _serialize_tool_error(tool_call.name, exc)
            else:
                normalized_error = _normalize_tool_error_result(tool_call.name, result)
                if normalized_error is not None:
                    failed_tool_calls.add(tool_call_signature)
                    result = normalized_error
        self._store.append_message(
            principal.tenant_id,
            Message(
                thread_id=thread_id,
                role=MessageRole.TOOL,
                content=serialize_tool_result(result),
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
            )
        )

    def _messages_for_llm(
        self,
        principal: Principal,
        thread_id: str,
        *,
        skill_prompt: str | None = None,
    ) -> list[Message]:
        system_prompt = RUNTIME_SYSTEM_PROMPT
        if skill_prompt:
            system_prompt = f"{system_prompt}\n\n{skill_prompt}"
        return [
            Message(thread_id=thread_id, role=MessageRole.SYSTEM, content=system_prompt),
            *self._store.list_messages(principal.tenant_id, thread_id),
        ]


def _serialize_tool_error(
    tool_name: str, exc: HTTPException, *, blocked: bool = False
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "tool_name": tool_name,
        "status_code": exc.status_code,
        "detail": exc.detail,
    }
    if blocked:
        error["blocked"] = True
    return {
        "error": {
            **error,
        }
    }


def _tool_call_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    return f"{tool_name}:{json.dumps(arguments, ensure_ascii=True, sort_keys=True, default=str)}"


def _normalize_tool_error_result(tool_name: str, result: object) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None

    error_value = result.get("error")
    if (
        isinstance(error_value, dict)
        and "tool_name" in error_value
        and "status_code" in error_value
    ):
        return result

    if "error" not in result:
        return None

    status_code = result.get("status_code", result.get("status"))
    detail = result.get("detail")
    normalized_error: dict[str, Any] = {
        "tool_name": tool_name,
        "status_code": status_code if isinstance(status_code, int) else 502,
        "detail": detail if detail is not None else error_value,
    }
    if error_value is not None:
        normalized_error["message"] = error_value
    if "documentation" in result:
        normalized_error["documentation"] = result["documentation"]
    return {"error": normalized_error}
