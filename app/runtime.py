from __future__ import annotations

import inspect
import json
import os
import textwrap
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from app.execution import (
    FixedTenantExecutionResolver,
    TenantExecutionResolver,
    build_tool_registry_for_capability_profile,
    build_tool_registry_for_skill,
    get_capability_profile,
    get_skill_configs,
)
from app.llm import LLMAdapter, serialize_tool_result
from app.models import LLMResponse, Message, MessageRole, Principal, ThreadContext, ThreadStatus
from app.store import InMemoryThreadStore
from app.tools import ToolExecutionContext, ToolRegistry

RUNTIME_SYSTEM_PROMPT = (
    "Use tools when they are relevant and ground claims in tool results. "
    "Distinguish clearly between direct verification and inference. "
    "Do not claim a live status, current availability, or real-time confirmation unless a tool result directly confirms it. "
    "If tool results fail, are indirect, or are insufficient, say that you could not directly verify the answer and explain what you were able to infer."
)
DEFAULT_MAX_ITERATIONS = 16
MAX_ITERATIONS_ENV = "MINIGENT_MAX_ITERATIONS"
RunEventSink = Callable[[dict[str, object]], Awaitable[None]]


def max_iterations_from_env() -> int:
    raw = os.getenv(MAX_ITERATIONS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_ITERATIONS
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{MAX_ITERATIONS_ENV} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{MAX_ITERATIONS_ENV} must be a positive integer")
    return value


class AgentRuntime:
    def __init__(
        self,
        store: InMemoryThreadStore,
        execution_resolver: TenantExecutionResolver | None = None,
        llm_adapter: LLMAdapter | None = None,
        tool_registry: ToolRegistry | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        recent_message_limit: int = 8,
        min_recent_message_limit: int = 4,
        max_summary_chars: int = 4000,
        target_prompt_tokens: int = 3000,
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
        self._recent_message_limit = max(1, recent_message_limit)
        self._min_recent_message_limit = min(
            self._recent_message_limit, max(1, min_recent_message_limit)
        )
        self._max_summary_chars = max(256, max_summary_chars)
        self._target_prompt_tokens = max(256, target_prompt_tokens)

    async def run_thread(
        self,
        principal: Principal,
        thread_id: str,
        *,
        event_sink: RunEventSink | None = None,
    ) -> str:
        self._store.start_run(principal.tenant_id, thread_id)
        failed_tool_calls: set[str] = set()
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
        try:
            for iteration in range(1, self._max_iterations + 1):
                messages = self._messages_for_llm(
                    principal,
                    thread_id,
                    skill_prompts=[skill.system_prompt for skill in skills],
                    skill_names=[skill.name for skill in skills],
                )
                await _emit_run_event(
                    event_sink,
                    {
                        "type": "llm.request",
                        "iteration": iteration,
                        "message_count": len(messages),
                        "tool_count": len(tool_registry.specs()),
                    },
                )
                response = await execution.llm_adapter.generate(messages, tool_registry.specs())
                if response.tool_call is not None:
                    await self._handle_tool_call(
                        principal,
                        thread_id,
                        response,
                        failed_tool_calls,
                        tool_registry=tool_registry,
                        event_sink=event_sink,
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
        event_sink: RunEventSink | None = None,
    ) -> None:
        tool_call = response.tool_call
        if tool_call is None:
            return
        await _emit_run_event(
            event_sink,
            {
                "type": "tool.call",
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            },
        )
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
                execute_signature = inspect.signature(tool_registry.execute)
                if "context" in execute_signature.parameters:
                    result = await tool_registry.execute(
                        tool_call.name,
                        tool_call.arguments,
                        context=ToolExecutionContext(
                            tenant_id=principal.tenant_id,
                            thread_id=thread_id,
                        ),
                    )
                else:
                    result = await tool_registry.execute(tool_call.name, tool_call.arguments)
            except HTTPException as exc:
                failed_tool_calls.add(tool_call_signature)
                result = _serialize_tool_error(tool_call.name, exc)
            else:
                normalized_error = _normalize_tool_error_result(tool_call.name, result)
                if normalized_error is not None:
                    failed_tool_calls.add(tool_call_signature)
                    result = normalized_error
        serialized_result = serialize_tool_result(result)
        self._store.append_message(
            principal.tenant_id,
            Message(
                thread_id=thread_id,
                role=MessageRole.TOOL,
                content=serialized_result,
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
            )
        )
        await _emit_run_event(
            event_sink,
            {
                "type": "tool.result",
                "tool_call_id": tool_call.id,
                "name": tool_call.name,
                "is_error": _normalize_tool_error_result(tool_call.name, result) is not None,
                "result": result,
            },
        )

    def _messages_for_llm(
        self,
        principal: Principal,
        thread_id: str,
        *,
        skill_prompts: list[str] | None = None,
        skill_names: list[str] | None = None,
    ) -> list[Message]:
        context = self._refresh_thread_context(principal, thread_id)
        system_prompt = RUNTIME_SYSTEM_PROMPT
        if skill_prompts:
            sections: list[str] = [system_prompt]
            for index, prompt in enumerate(skill_prompts):
                label = (
                    skill_names[index]
                    if skill_names is not None and index < len(skill_names)
                    else f"skill-{index + 1}"
                )
                sections.append(f"[Skill: {label}]\n{prompt}")
            system_prompt = "\n\n".join(sections)
        prompt_messages = [
            Message(thread_id=thread_id, role=MessageRole.SYSTEM, content=system_prompt),
        ]
        if context.summary:
            prompt_messages.append(
                Message(
                    thread_id=thread_id,
                    role=MessageRole.SYSTEM,
                    content=f"Thread summary:\n{context.summary}",
                )
            )
        prompt_messages.extend(
            self._store.list_messages(principal.tenant_id, thread_id)[
                context.summarized_message_count :
            ]
        )
        return prompt_messages

    def _refresh_thread_context(self, principal: Principal, thread_id: str) -> ThreadContext:
        messages = self._store.list_messages(principal.tenant_id, thread_id)
        context = self._store.get_thread_context(principal.tenant_id, thread_id)
        summarize_upto = self._compute_summarize_upto(messages, context)
        if summarize_upto <= context.summarized_message_count:
            return context

        new_summary = _merge_summaries(
            context.summary,
            _summarize_messages(messages[context.summarized_message_count:summarize_upto]),
            max_chars=self._max_summary_chars,
        )
        self._store.update_thread_context(
            principal.tenant_id,
            thread_id,
            summary=new_summary,
            summarized_message_count=summarize_upto,
        )
        return self._store.compact_thread_messages(principal.tenant_id, thread_id)

    def _compute_summarize_upto(self, messages: list[Message], context: ThreadContext) -> int:
        max_summarize_upto = max(0, len(messages) - self._min_recent_message_limit)
        default_summarize_upto = max(0, len(messages) - self._recent_message_limit)
        summarize_upto = max(context.summarized_message_count, default_summarize_upto)
        if summarize_upto >= max_summarize_upto:
            return summarize_upto

        estimated_tokens = _estimate_prompt_tokens(
            messages[summarize_upto:],
            summary=context.summary,
        )
        while estimated_tokens > self._target_prompt_tokens and summarize_upto < max_summarize_upto:
            estimated_tokens -= _estimate_message_tokens(messages[summarize_upto])
            summarize_upto += 1
        return summarize_upto


async def _emit_run_event(
    event_sink: RunEventSink | None,
    event: dict[str, object],
) -> None:
    if event_sink is not None:
        await event_sink(event)


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


def _summarize_messages(messages: list[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        line = _summarize_message(message)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _summarize_message(message: Message) -> str:
    role_labels = {
        MessageRole.USER: "User",
        MessageRole.ASSISTANT: "Assistant",
        MessageRole.TOOL: "Tool",
        MessageRole.SYSTEM: "System",
    }
    if message.role == MessageRole.ASSISTANT and message.tool_name and message.tool_call_id:
        arguments = json.dumps(message.tool_arguments or {}, ensure_ascii=True, sort_keys=True)
        return _clamp_summary_line(
            f"Assistant requested tool {message.tool_name} with arguments {arguments}."
        )
    if message.role == MessageRole.TOOL and message.tool_name:
        return _clamp_summary_line(
            f"Tool {message.tool_name} returned {_normalize_summary_text(message.content)}."
        )
    return _clamp_summary_line(
        f"{role_labels.get(message.role, message.role.value.title())}: "
        f"{_normalize_summary_text(message.content)}"
    )


def _normalize_summary_text(content: str) -> str:
    normalized = " ".join(content.split())
    if not normalized:
        return "[empty]"
    return normalized


def _clamp_summary_line(line: str, *, width: int = 280) -> str:
    if len(line) <= width:
        return line
    return f"{line[: width - 3].rstrip()}..."


def _merge_summaries(existing: str, new_chunk: str, *, max_chars: int) -> str:
    if not new_chunk:
        return existing
    merged = new_chunk if not existing else f"{existing}\n{new_chunk}"
    if len(merged) <= max_chars:
        return merged

    lines = merged.splitlines()
    trimmed: list[str] = []
    total = len("... [older summarized context omitted]")
    for line in reversed(lines):
        added = len(line) + (1 if trimmed else 0)
        if total + added > max_chars:
            break
        trimmed.append(line)
        total += added
    trimmed.reverse()
    body = "\n".join(trimmed)
    if not body:
        return "... [older summarized context omitted]"
    return textwrap.dedent(
        f"""\
        ... [older summarized context omitted]
        {body}
        """
    ).strip()


def _estimate_prompt_tokens(messages: list[Message], *, summary: str = "") -> int:
    total = _estimate_text_tokens(RUNTIME_SYSTEM_PROMPT)
    if summary:
        total += _estimate_text_tokens(f"Thread summary:\n{summary}")
    for message in messages:
        total += _estimate_message_tokens(message)
    return total


def _estimate_message_tokens(message: Message) -> int:
    total = _estimate_text_tokens(message.content)
    if message.tool_name:
        total += _estimate_text_tokens(message.tool_name)
    if message.tool_arguments:
        total += _estimate_text_tokens(
            json.dumps(message.tool_arguments, ensure_ascii=True, sort_keys=True)
        )
    return total + 6


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 1
    return max(1, (len(text) + 3) // 4)
