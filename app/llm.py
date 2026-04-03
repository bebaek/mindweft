from __future__ import annotations

import json
from abc import ABC, abstractmethod

from app.models import LLMResponse, Message, MessageRole, ToolCall, ToolSpec


class LLMAdapter(ABC):
    @abstractmethod
    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        raise NotImplementedError


class MockLLMAdapter(LLMAdapter):
    """
    Deterministic fallback adapter for the POC.

    It proves the runtime loop without depending on an external model provider.
    If a user message starts with `/tool <name> <payload>`, it emits a tool call.
    After a tool result is present, it turns that into the final assistant reply.
    """

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        if messages and messages[-1].role == MessageRole.TOOL:
            return LLMResponse(content=f"Tool result: {messages[-1].content}")

        last_user = next((message for message in reversed(messages) if message.role == MessageRole.USER), None)
        if last_user is None:
            return LLMResponse(content="No user message found.")

        tool_names = {tool.name for tool in tools}
        if last_user.content.startswith("/tool "):
            _, tool_name, *rest = last_user.content.split(" ", 2)
            payload = rest[0] if rest else ""
            if tool_name in tool_names:
                return LLMResponse(tool_call=ToolCall(name=tool_name, arguments={"text": payload}))

        return LLMResponse(content=f"Mock reply: {last_user.content}")


class OpenAICompatibleAdapter(LLMAdapter):
    """
    Placeholder for a future provider-backed adapter.

    The POC ships with the mock adapter by default; this type exists so the API
    boundary is explicit and easy to replace later.
    """

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        raise NotImplementedError("Provider-backed adapters are not implemented in this POC.")


def serialize_tool_result(result: object) -> str:
    return json.dumps(result, ensure_ascii=True, default=str)
