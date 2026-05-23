from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app.models import LLMResponse, Message, MessageRole, ToolCall, ToolSpec
from app.oauth import GENERIC_OAUTH_PROVIDER, GenericOAuthProvider

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
                        id=f"mock-{tool_name}-call",
                        name=tool_name,
                        arguments=_parse_mock_tool_arguments(tool_name, payload),
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
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        request_messages = messages
        pruned_for_azure = False
        if _is_azure_openai_base_url(self._base_url):
            request_messages = _prune_historical_tool_messages_for_azure(messages)
            pruned_for_azure = True
        logger.debug(
            "LLM request provider=%s base_url=%s pruned_for_azure=%s",
            self.describe()["provider"],
            self._base_url,
            pruned_for_azure,
        )
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await self._post_chat_completion(
                    client=client,
                    headers=headers,
                    request_messages=request_messages,
                    tools=tools,
                    tool_name_map=tool_name_map,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                retry_messages = _prune_historical_tool_messages_for_azure(messages)
                should_retry = (
                    not pruned_for_azure
                    and retry_messages != request_messages
                    and _is_missing_tool_call_for_output_error(exc.response)
                )
                if should_retry:
                    logger.warning(
                        "Retrying chat completion with Azure tool-history pruning after provider error "
                        "provider=%s base_url=%s",
                        self.describe()["provider"],
                        self._base_url,
                    )
                    retry_response = await self._post_chat_completion(
                        client=client,
                        headers=headers,
                        request_messages=retry_messages,
                        tools=tools,
                        tool_name_map=tool_name_map,
                    )
                    try:
                        retry_response.raise_for_status()
                    except httpx.HTTPStatusError as retry_exc:
                        detail = retry_exc.response.text or str(retry_exc)
                        logger.warning(
                            "Retried chat completion with Azure tool-history pruning failed provider=%s "
                            "base_url=%s",
                            self.describe()["provider"],
                            self._base_url,
                        )
                        raise HTTPException(
                            status_code=502, detail=f"LLM provider error: {detail}"
                        ) from retry_exc
                    logger.info(
                        "Retried chat completion with Azure tool-history pruning succeeded provider=%s "
                        "base_url=%s",
                        self.describe()["provider"],
                        self._base_url,
                    )
                    response = retry_response
                else:
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
        elif _is_azure_openai_base_url(self._base_url):
            provider = "azure-openai"
        elif "api.openai.com" in self._base_url:
            provider = "openai"
        return {
            "provider": provider,
            "model": self._model,
            "base_url": self._base_url,
            "headers": sorted(self._extra_headers.keys()),
            "adapter": "OpenAICompatibleAdapter",
        }

    async def _post_chat_completion(
        self,
        *,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        request_messages: list[Message],
        tools: list[ToolSpec],
        tool_name_map: dict[str, str],
    ) -> httpx.Response:
        payload = {
            "model": self._model,
            "messages": [
                _message_to_chat_payload(message, tool_name_map) for message in request_messages
            ],
            "tools": [_tool_to_payload(tool, tool_name_map) for tool in tools],
        }
        return await client.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers,
        )


class GenericOAuthResponsesAdapter(LLMAdapter):
    def __init__(
        self,
        *,
        model: str,
        url: str,
        oauth_provider: GenericOAuthProvider | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._url = url
        self._oauth_provider = oauth_provider or GenericOAuthProvider()
        self._extra_headers = extra_headers or {}
        self._timeout = timeout
        self._transport = transport

    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        credentials = await self._oauth_provider.get_credentials()
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Generic OAuth credentials are missing. Start login at /oauth/generic/login.",
            )
        account_id_header = os.getenv("MINIGENT_LLM_ACCOUNT_ID_HEADER", "").strip()
        if account_id_header and not credentials.account_id:
            raise HTTPException(status_code=401, detail="OAuth credentials are missing account_id")

        tool_name_map = _build_provider_tool_name_map(tools)
        headers = {
            "Authorization": f"Bearer {credentials.access_token}",
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        if account_id_header and credentials.account_id:
            headers[account_id_header] = credentials.account_id
        payload = {
            "model": self._model,
            "store": False,
            "stream": True,
            "instructions": "You are a helpful assistant.",
            "input": [_message_to_responses_payload(message, tool_name_map) for message in messages],
            "tools": [_tool_to_responses_payload(tool, tool_name_map) for tool in tools],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(
                    self._url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text or str(exc)
                raise HTTPException(status_code=502, detail=f"Generic OAuth LLM error: {detail}") from exc
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"Generic OAuth LLM request failed: {exc}") from exc
        body = response.text
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type or _looks_like_sse(body):
            return _parse_responses_sse(body, tool_name_map)
        try:
            return _parse_responses_payload(response.json(), tool_name_map)
        except json.JSONDecodeError as exc:
            logger.error("Generic OAuth LLM returned non-JSON body: %s", body[:2000])
            raise HTTPException(
                status_code=502,
                detail="Generic OAuth LLM returned a non-JSON, non-SSE response",
            ) from exc

    def describe(self) -> dict[str, Any]:
        return {
            "provider": GENERIC_OAUTH_PROVIDER,
            "model": self._model,
            "url": self._url,
            "headers": sorted(self._extra_headers.keys()),
            "adapter": "GenericOAuthResponsesAdapter",
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
    if provider == GENERIC_OAUTH_PROVIDER:
        model = _required_env("MINIGENT_LLM_MODEL")
        url = _required_env("MINIGENT_LLM_URL")
        extra_headers = _json_string_map_env("MINIGENT_LLM_EXTRA_HEADERS")
        logger.info("LLM config: provider=%s model=%s url=%s", provider, model, url)
        return GenericOAuthResponsesAdapter(model=model, url=url, extra_headers=extra_headers)

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


def _parse_mock_tool_arguments(tool_name: str, payload: str) -> dict[str, Any]:
    stripped = payload.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="mock tool payload is invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="mock tool payload must be a JSON object")
        return parsed
    if tool_name == "retrieve_knowledge":
        return {"query": stripped}
    return {"text": stripped}


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


def _message_to_responses_payload(message: Message, tool_name_map: dict[str, str]) -> dict[str, Any]:
    if message.role == MessageRole.TOOL:
        return {
            "type": "function_call_output",
            "call_id": message.tool_call_id or "unknown-call",
            "output": message.content,
        }
    if message.role == MessageRole.ASSISTANT and message.tool_call_id and message.tool_name:
        return {
            "type": "function_call",
            "call_id": message.tool_call_id,
            "name": tool_name_map.get(message.tool_name, _sanitize_tool_name(message.tool_name)),
            "arguments": json.dumps(message.tool_arguments or {}, ensure_ascii=True),
        }
    role = "assistant" if message.role == MessageRole.ASSISTANT else "user"
    return {"role": role, "content": message.content}


def _tool_to_responses_payload(tool: ToolSpec, tool_name_map: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool_name_map[tool.name],
        "description": tool.description,
        "parameters": tool.input_schema,
    }


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _json_string_map_env(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise RuntimeError(f"{name} must be a JSON object of string values")
    return dict(payload)


def _is_azure_openai_base_url(base_url: str) -> bool:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return host.endswith(".openai.azure.com") or (
        host.endswith(".services.ai.azure.com") and "/openai/" in path
    )


def _is_missing_tool_call_for_output_error(response: httpx.Response) -> bool:
    detail = response.text or ""
    return "No tool call found for function call output with call_id" in detail


def _prune_historical_tool_messages_for_azure(messages: list[Message]) -> list[Message]:
    active_tool_call_id = _active_tool_call_id(messages)
    pruned: list[Message] = []
    dropped_messages = 0
    for message in messages:
        if _should_drop_historical_tool_message(message, active_tool_call_id):
            dropped_messages += 1
            continue
        pruned.append(message)
    if dropped_messages:
        logger.debug(
            "Pruned %s historical tool-related message(s) from Azure chat-completions payload",
            dropped_messages,
        )
    return pruned


def _active_tool_call_id(messages: list[Message]) -> str | None:
    if len(messages) < 2:
        return None
    if _is_completed_tool_pair(messages[-2], messages[-1]):
        return messages[-1].tool_call_id
    return None


def _is_completed_tool_pair(assistant_message: Message, tool_message: Message) -> bool:
    return (
        assistant_message.role == MessageRole.ASSISTANT
        and tool_message.role == MessageRole.TOOL
        and bool(assistant_message.tool_name)
        and bool(assistant_message.tool_call_id)
        and assistant_message.tool_call_id == tool_message.tool_call_id
    )


def _should_drop_historical_tool_message(message: Message, active_tool_call_id: str | None) -> bool:
    tool_call_id = message.tool_call_id
    if not tool_call_id or tool_call_id == active_tool_call_id:
        return False
    if message.role == MessageRole.TOOL:
        return True
    return message.role == MessageRole.ASSISTANT and bool(message.tool_name)


def _looks_like_sse(body: str) -> bool:
    stripped = body.lstrip()
    return stripped.startswith("data:") or stripped.startswith("event:")


def _parse_responses_sse(body: str, tool_name_map: dict[str, str]) -> LLMResponse:
    text_parts: list[str] = []
    final_response: dict[str, Any] | None = None
    output_items: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        delta = event.get("delta")
        if event.get("type") == "response.output_text.delta" and isinstance(delta, str):
            text_parts.append(delta)
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            final_response = response_payload
        item = event.get("item")
        if isinstance(item, dict):
            output_items.append(item)

    if final_response is not None:
        try:
            return _parse_responses_payload(final_response, tool_name_map)
        except HTTPException:
            if text_parts:
                return LLMResponse(content="".join(text_parts))
            if output_items:
                return _parse_responses_payload({"output": output_items}, tool_name_map)
            raise
    if output_items:
        return _parse_responses_payload({"output": output_items}, tool_name_map)
    if text_parts:
        return LLMResponse(content="".join(text_parts))
    logger.error("Responses SSE missing message/function output: %s", body[:2000])
    raise HTTPException(status_code=502, detail="Generic OAuth LLM returned no assistant output")


def _parse_responses_payload(payload: dict[str, Any], tool_name_map: dict[str, str]) -> LLMResponse:
    output = payload.get("output") or []
    if not isinstance(output, list):
        logger.error("Responses payload output is not a list: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail="Generic OAuth LLM returned invalid output")

    reverse_tool_name_map = {value: key for key, value in tool_name_map.items()}
    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            raw_name = item.get("name")
            raw_arguments = item.get("arguments") or "{}"
            call_id = item.get("call_id") or item.get("id") or "generic-oauth-tool-call"
            if not isinstance(raw_name, str) or not isinstance(call_id, str):
                continue
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else {}
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            return LLMResponse(
                tool_call=ToolCall(
                    id=call_id,
                    name=reverse_tool_name_map.get(raw_name, raw_name),
                    arguments=arguments,
                )
            )
        if item.get("type") == "message":
            content = item.get("content") or []
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text") or part.get("output_text")
                if isinstance(text, str):
                    text_parts.append(text)
    if text_parts:
        return LLMResponse(content="".join(text_parts))
    logger.error("Responses payload missing message/function output: %s", _truncate_json(payload))
    raise HTTPException(status_code=502, detail="Generic OAuth LLM returned no assistant output")


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
