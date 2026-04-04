from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException

from app.models import LLMResponse, Message, MessageRole, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


class LLMAdapter(ABC):
    @abstractmethod
    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        raise NotImplementedError

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        raise NotImplementedError


class MockLLMAdapter(LLMAdapter):
    """
    Deterministic fallback adapter for the POC.

    It proves the runtime loop without depending on an external model provider.
    If a user message starts with `/tool <name> <payload>`, it emits a tool call.
    After a tool result is present, it turns that into the final assistant reply.
    """

    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        if messages and messages[-1].role == MessageRole.TOOL:
            return LLMResponse(content=f"Tool result: {messages[-1].content}")

        last_user = next(
            (message for message in reversed(messages) if message.role == MessageRole.USER), None
        )
        if last_user is None:
            return LLMResponse(content="No user message found.")

        tool_names = {tool.name for tool in tools}
        if last_user.content.startswith("/tool "):
            _, tool_name, *rest = last_user.content.split(" ", 2)
            payload = rest[0] if rest else ""
            if tool_name in tool_names:
                return LLMResponse(
                    tool_call=ToolCall(
                        id=f"mock-{tool_name}-call", name=tool_name, arguments={"text": payload}
                    )
                )

        return LLMResponse(content=f"Mock reply: {last_user.content}")

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "mock",
            "model": None,
            "base_url": None,
            "headers": [],
            "adapter": "MockLLMAdapter",
        }


class OpenAICompatibleAdapter(LLMAdapter):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._extra_headers = extra_headers or {}
        self._timeout = timeout
        self._transport = transport

    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        tool_name_map = _build_provider_tool_name_map(tools)
        payload = {
            "model": self._model,
            "messages": [_message_to_chat_payload(message, tool_name_map) for message in messages],
            "tools": [_tool_to_payload(tool, tool_name_map) for tool in tools],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text or str(exc)
                raise HTTPException(
                    status_code=502, detail=f"LLM provider error: {detail}"
                ) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"LLM request failed: {exc}") from exc
        return _parse_chat_completion(response.json(), tool_name_map)

    def describe(self) -> dict[str, Any]:
        provider = "openai-compatible"
        if "openrouter.ai" in self._base_url:
            provider = "openrouter"
        elif "api.openai.com" in self._base_url:
            provider = "openai"
        return {
            "provider": provider,
            "model": self._model,
            "base_url": self._base_url,
            "headers": sorted(self._extra_headers.keys()),
            "adapter": "OpenAICompatibleAdapter",
        }


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
            raise RuntimeError(
                "OPENROUTER_API_KEY is required when MINIGENT_LLM_PROVIDER=openrouter"
            )
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


def _message_to_chat_payload(message: Message, tool_name_map: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.role == MessageRole.ASSISTANT and message.tool_call_id and message.tool_name:
        payload["content"] = None
        payload["tool_calls"] = [
            {
                "id": message.tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_name_map.get(
                        message.tool_name, _sanitize_tool_name(message.tool_name)
                    ),
                    "arguments": json.dumps(message.tool_arguments or {}, ensure_ascii=True),
                },
            }
        ]
    if message.role == MessageRole.TOOL:
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
    return payload


def _tool_to_payload(tool: ToolSpec, tool_name_map: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool_name_map[tool.name],
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _parse_chat_completion(payload: dict[str, Any], tool_name_map: dict[str, str]) -> LLMResponse:
    choices = payload.get("choices") or []
    if not choices:
        logger.error("LLM response missing choices: %s", _truncate_json(payload))
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
                raise HTTPException(
                    status_code=502, detail="LLM returned invalid tool arguments"
                ) from exc
        else:
            parsed_arguments = arguments
        return LLMResponse(
            tool_call=ToolCall(
                id=tool_calls[0].get("id"),
                name=_resolve_internal_tool_name(function.get("name", ""), tool_name_map),
                arguments=parsed_arguments,
            )
        )

    content = _extract_text_content(message)
    if content is None:
        logger.error("LLM response missing message content: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail="LLM provider returned no message content")
    return LLMResponse(content=str(content))


def _extract_text_content(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
                continue
            if part.get("type") == "output_text":
                nested_text = part.get("text")
                if isinstance(nested_text, str) and nested_text:
                    text_parts.append(nested_text)
        if text_parts:
            return "".join(text_parts)

    for key in ("output_text", "refusal", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value

    if isinstance(message.get("refusal"), list):
        text_parts = [
            part.get("text", "")
            for part in message["refusal"]
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if text_parts:
            return "".join(text_parts)

    return None


def _truncate_json(payload: dict[str, Any], limit: int = 1200) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, default=str)
    if len(serialized) <= limit:
        return serialized
    return serialized[:limit] + "...<truncated>"


def _build_provider_tool_name_map(tools: list[ToolSpec]) -> dict[str, str]:
    provider_names: dict[str, str] = {}
    used_provider_names: set[str] = set()
    for tool in tools:
        base_name = _sanitize_tool_name(tool.name)
        provider_name = base_name
        suffix = 2
        while provider_name in used_provider_names:
            provider_name = f"{base_name}_{suffix}"
            suffix += 1
        provider_names[tool.name] = provider_name
        used_provider_names.add(provider_name)
    return provider_names


def _sanitize_tool_name(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")
    return sanitized or "tool"


def _resolve_internal_tool_name(provider_name: str, tool_name_map: dict[str, str]) -> str:
    for internal_name, mapped_name in tool_name_map.items():
        if mapped_name == provider_name:
            return internal_name
    return provider_name
