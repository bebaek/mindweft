from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException

from app.models import LLMResponse, Message, MessageRole, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


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
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._extra_headers = extra_headers or {}
        self._timeout = timeout
        self._transport = transport

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        payload = {
            "model": self._model,
            "messages": [_message_to_chat_payload(message) for message in messages],
            "tools": [_tool_to_payload(tool) for tool in tools],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text or str(exc)
                raise HTTPException(status_code=502, detail=f"LLM provider error: {detail}") from exc
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"LLM request failed: {exc}") from exc

        return _parse_chat_completion(response.json())


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    extra_headers: dict[str, str]


def build_llm_adapter_from_env() -> LLMAdapter:
    provider = os.getenv("MINIGENT_LLM_PROVIDER", "mock").lower()
    if provider == "mock":
        logger.info("LLM config: provider=mock adapter=MockLLMAdapter")
        return MockLLMAdapter()

    config = load_provider_config(provider)
    logger.info(
        "LLM config: provider=%s model=%s base_url=%s headers=%s",
        config.provider,
        config.model,
        config.base_url,
        sorted(config.extra_headers.keys()),
    )
    return OpenAICompatibleAdapter(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        extra_headers=config.extra_headers,
    )


def load_provider_config(provider: str) -> ProviderConfig:
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when MINIGENT_LLM_PROVIDER=openai")
        return ProviderConfig(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            extra_headers={},
        )

    if provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        model = os.getenv("OPENROUTER_MODEL", "openai/gpt-5.4-mini")
        base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required when MINIGENT_LLM_PROVIDER=openrouter")
        extra_headers: dict[str, str] = {}
        site_url = os.getenv("OPENROUTER_HTTP_REFERER")
        app_name = os.getenv("OPENROUTER_APP_NAME")
        if site_url:
            extra_headers["HTTP-Referer"] = site_url
        if app_name:
            extra_headers["X-OpenRouter-Title"] = app_name
        return ProviderConfig(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            extra_headers=extra_headers,
        )

    raise RuntimeError(f"Unsupported MINIGENT_LLM_PROVIDER '{provider}'")


def serialize_tool_result(result: object) -> str:
    return json.dumps(result, ensure_ascii=True, default=str)


def _message_to_chat_payload(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.role == MessageRole.TOOL and message.tool_name:
        payload["name"] = message.tool_name
    return payload


def _tool_to_payload(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _parse_chat_completion(payload: dict[str, Any]) -> LLMResponse:
    choices = payload.get("choices") or []
    if not choices:
        raise HTTPException(status_code=502, detail="LLM provider returned no choices")

    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        function = tool_calls[0].get("function") or {}
        arguments = function.get("arguments") or "{}"
        if isinstance(arguments, str):
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=502, detail="LLM returned invalid tool arguments") from exc
        else:
            parsed_arguments = arguments
        return LLMResponse(
            tool_call=ToolCall(
                name=function.get("name", ""),
                arguments=parsed_arguments,
            )
        )

    content = message.get("content")
    if isinstance(content, list):
        text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        content = "".join(text_parts)
    if content is None:
        raise HTTPException(status_code=502, detail="LLM provider returned no message content")
    return LLMResponse(content=str(content))
