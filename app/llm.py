from __future__ import annotations

import hashlib
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
LLM_DEBUG_LOG_RESPONSES_ENV = "MINIGENT_LLM_DEBUG_LOG_RESPONSES"
LLM_DEBUG_LOG_RESPONSE_MAX_CHARS_ENV = "MINIGENT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS"
LLM_DEBUG_RESPONSE_LOG_PATH_ENV = "MINIGENT_LLM_DEBUG_RESPONSE_LOG_PATH"
LLM_DEBUG_REQUEST_LOG_PATH_ENV = "MINIGENT_LLM_DEBUG_REQUEST_LOG_PATH"
LLM_PROMPT_CACHE_KEY_ENV = "MINIGENT_LLM_PROMPT_CACHE_KEY"
RESPONSES_OUTPUT_ITEMS_METADATA_KEY = "generic_oauth_responses_output_items"
DEFAULT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS = 20000


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
        tools = _stable_tool_order(tools)
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
        _debug_log_raw_llm_response(
            "openai-compatible",
            response.text,
            content_type=response.headers.get("content-type"),
        )
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
        }
        if tools:
            payload["tools"] = [_tool_to_payload(tool, tool_name_map) for tool in tools]
        if "openrouter.ai" in self._base_url:
            payload["usage"] = {"include": True}
        _debug_log_llm_request(
            "openai-compatible",
            payload,
            model=self._model,
            message_count=len(request_messages),
            tool_count=len(tools),
        )
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

        tools = _stable_tool_order(tools)
        tool_name_map = _build_provider_tool_name_map(tools)
        headers = {
            "Authorization": f"Bearer {credentials.access_token}",
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        if account_id_header and credentials.account_id:
            headers[account_id_header] = credentials.account_id
        thread_id = _thread_id_for_prompt_cache(messages)
        if thread_id:
            headers.setdefault("session_id", thread_id)
            headers.setdefault("session-id", thread_id)
            headers.setdefault("thread-id", thread_id)
            headers.setdefault("x-client-request-id", thread_id)
        payload = {
            "model": self._model,
            "store": False,
            "stream": True,
            "instructions": _responses_instructions(messages),
            "input": [
                item
                for message in messages
                if message.role != MessageRole.SYSTEM
                for item in _message_to_responses_payload(message, tool_name_map)
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if tools:
            payload["tools"] = [_tool_to_responses_payload(tool, tool_name_map) for tool in tools]
        if _is_chatgpt_codex_responses_url(self._url):
            payload["include"] = ["reasoning.encrypted_content"]
        prompt_cache_key = _prompt_cache_key_for_request(messages)
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for attempt in range(2):
                _debug_log_llm_request(
                    "generic-oauth-responses",
                    payload,
                    model=self._model,
                    message_count=len(messages),
                    tool_count=len(tools),
                )
                body, content_type = await _post_responses_request(
                    client,
                    self._url,
                    payload,
                    headers,
                )
                _debug_log_raw_llm_response(
                    GENERIC_OAUTH_PROVIDER,
                    body,
                    content_type=content_type,
                )
                try:
                    if "text/event-stream" in content_type or _looks_like_sse(body):
                        return _parse_responses_sse(body, tool_name_map)
                    return _parse_responses_payload(json.loads(body), tool_name_map)
                except json.JSONDecodeError as exc:
                    logger.error("Generic OAuth LLM returned non-JSON body: %s", body[:2000])
                    raise HTTPException(
                        status_code=502,
                        detail="Generic OAuth LLM returned a non-JSON, non-SSE response",
                    ) from exc
                except HTTPException as exc:
                    if not _is_no_responses_output_error(exc) or attempt > 0:
                        raise
                    reasoning_items = _responses_reasoning_input_items_from_body(body)
                    if not reasoning_items:
                        raise
                    logger.info(
                        "Generic OAuth LLM returned reasoning-only output; retrying once with %s reasoning item(s)",
                        len(reasoning_items),
                    )
                    payload["input"] = [*payload["input"], *reasoning_items]

        raise HTTPException(
            status_code=502, detail="Generic OAuth LLM returned no assistant output"
        )

    def describe(self) -> dict[str, Any]:
        return {
            "provider": GENERIC_OAUTH_PROVIDER,
            "model": self._model,
            "url": self._url,
            "headers": sorted(self._extra_headers.keys()),
            "adapter": "GenericOAuthResponsesAdapter",
            "prompt_cache_key_configured": bool(os.getenv(LLM_PROMPT_CACHE_KEY_ENV, "").strip()),
        }


async def _post_responses_request(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> tuple[str, str]:
    body_chunks: list[str] = []
    content_type = ""
    read_error: httpx.HTTPError | None = None
    try:
        async with client.stream(
            "POST",
            url,
            json=payload,
            headers=headers,
        ) as response:
            content_type = response.headers.get("content-type", "")
            if response.is_error:
                detail = await response.aread()
                text = detail.decode(errors="replace") if detail else response.reason_phrase
                raise HTTPException(status_code=502, detail=f"Generic OAuth LLM error: {text}")
            try:
                async for chunk in response.aiter_text():
                    body_chunks.append(chunk)
            except httpx.HTTPError as exc:
                read_error = exc
    except httpx.HTTPError as exc:
        detail = str(exc) or exc.__class__.__name__
        raise HTTPException(
            status_code=502,
            detail=f"Generic OAuth LLM request failed: {detail}",
        ) from exc

    body = "".join(body_chunks)
    if read_error is not None and not body:
        detail = str(read_error) or read_error.__class__.__name__
        raise HTTPException(
            status_code=502,
            detail=f"Generic OAuth LLM request failed: {detail}",
        ) from read_error
    return body, content_type


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


def _debug_log_llm_request(
    adapter: str,
    payload: dict[str, Any],
    *,
    model: str,
    message_count: int,
    tool_count: int,
) -> None:
    log_path = os.getenv(LLM_DEBUG_REQUEST_LOG_PATH_ENV, "").strip()
    if not log_path:
        return
    try:
        raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        canonical_json = json.dumps(
            _canonical_jsonish(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        record = {
            "adapter": adapter,
            "model": model,
            "message_count": message_count,
            "tool_count": tool_count,
            "raw_sha256": hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
            "canonical_sha256": hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
            "raw_chars": len(raw_json),
            "canonical_chars": len(canonical_json),
            "payload": payload,
        }
        with open(log_path, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Failed to write raw LLM request debug log")


def _debug_log_raw_llm_response(
    adapter: str,
    body: str,
    *,
    content_type: str | None = None,
) -> None:
    if not _env_bool(LLM_DEBUG_LOG_RESPONSES_ENV):
        return
    max_chars = _env_int(
        LLM_DEBUG_LOG_RESPONSE_MAX_CHARS_ENV,
        DEFAULT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS,
    )
    truncated = max_chars >= 0 and len(body) > max_chars
    logged_body = body[:max_chars] if truncated else body
    logger.info(
        "LLM raw response adapter=%s content_type=%s chars=%s truncated=%s body=%s",
        adapter,
        content_type or "",
        len(body),
        truncated,
        logged_body,
    )
    log_path = os.getenv(LLM_DEBUG_RESPONSE_LOG_PATH_ENV, "").strip()
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as file:
                file.write(
                    json.dumps(
                        {
                            "adapter": adapter,
                            "content_type": content_type or "",
                            "chars": len(body),
                            "truncated": truncated,
                            "body": logged_body,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            logger.exception("Failed to write raw LLM response debug log")


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid integer value for %s", name)
        return default


def _parse_mock_tool_arguments(tool_name: str, payload: str) -> dict[str, Any]:
    stripped = payload.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="mock tool payload is invalid JSON"
            ) from exc
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
                    "arguments": json.dumps(
                        message.tool_arguments or {},
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
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
            "parameters": _canonical_jsonish(tool.input_schema),
        },
    }


def _is_chatgpt_codex_responses_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.netloc == "chatgpt.com" and parsed.path.rstrip("/") == "/backend-api/codex/responses"
    )


def _thread_id_for_prompt_cache(messages: list[Message]) -> str | None:
    message = next((item for item in messages if item.thread_id), None)
    return message.thread_id if message is not None else None


def _prompt_cache_key_for_request(messages: list[Message]) -> str | None:
    configured = os.getenv(LLM_PROMPT_CACHE_KEY_ENV, "").strip()
    if configured and configured.lower() not in {"thread", "thread_id", "auto"}:
        return configured
    return _thread_id_for_prompt_cache(messages)


def _responses_instructions(messages: list[Message]) -> str:
    system_parts = [message.content for message in messages if message.role == MessageRole.SYSTEM]
    if not system_parts:
        return "You are a helpful assistant."
    return "\n\n".join(system_parts)


def _message_to_responses_payload(
    message: Message, tool_name_map: dict[str, str]
) -> list[dict[str, Any]]:
    if message.role == MessageRole.TOOL:
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id or "unknown-call",
                "output": message.content,
            }
        ]

    payload_items = _stored_responses_output_items(message)
    if message.role == MessageRole.ASSISTANT and message.tool_call_id and message.tool_name:
        payload_items.append(
            {
                "type": "function_call",
                "call_id": message.tool_call_id,
                "name": tool_name_map.get(
                    message.tool_name, _sanitize_tool_name(message.tool_name)
                ),
                "arguments": json.dumps(
                    message.tool_arguments or {},
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            }
        )
        return payload_items

    role = "assistant" if message.role == MessageRole.ASSISTANT else "user"
    payload_items.append({"role": role, "content": message.content})
    return payload_items


def _stored_responses_output_items(message: Message) -> list[dict[str, Any]]:
    metadata = message.metadata or {}
    raw_items = metadata.get(RESPONSES_OUTPUT_ITEMS_METADATA_KEY)
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        if raw_item.get("type") != "reasoning":
            continue
        encrypted_content = raw_item.get("encrypted_content")
        if not isinstance(encrypted_content, str) or not encrypted_content:
            continue
        item: dict[str, Any] = {
            "type": "reasoning",
            "summary": _responses_reasoning_summary(raw_item),
            "encrypted_content": encrypted_content,
        }
        item_id = raw_item.get("id")
        if isinstance(item_id, str):
            item["id"] = item_id
        items.append(item)
    return items


def _tool_to_responses_payload(tool: ToolSpec, tool_name_map: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool_name_map[tool.name],
        "description": tool.description,
        "parameters": _canonical_jsonish(tool.input_schema),
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
    usage: dict[str, int] | None = None
    output_items_by_key: dict[str, dict[str, Any]] = {}
    output_item_order: list[str] = []
    argument_deltas_by_key: dict[str, list[str]] = {}
    done_arguments_by_key: dict[str, str] = {}
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
        event_usage = _normalized_usage_from_event(event)
        if event_usage is not None:
            usage = event_usage
        event_type = event.get("type")
        delta = event.get("delta")
        if event_type == "response.output_text.delta" and isinstance(delta, str):
            text_parts.append(delta)
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            final_response = response_payload
        item = event.get("item")
        item_key = _responses_sse_item_key(event, item)
        if isinstance(item, dict) and item_key is not None:
            existing = output_items_by_key.get(item_key, {})
            merged = {**existing, **item}
            output_items_by_key[item_key] = merged
            if item_key not in output_item_order:
                output_item_order.append(item_key)
        if event_type in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            argument_key = item_key or _responses_sse_argument_key(event)
            if argument_key is not None:
                if event_type == "response.function_call_arguments.done":
                    arguments = event.get("arguments")
                    if isinstance(arguments, str):
                        done_arguments_by_key[argument_key] = arguments
                elif isinstance(delta, str):
                    argument_deltas_by_key.setdefault(argument_key, []).append(delta)

    output_items = _responses_sse_output_items(
        output_items_by_key,
        output_item_order,
        argument_deltas_by_key,
        done_arguments_by_key,
    )
    if final_response is not None:
        try:
            response = _parse_responses_payload(
                final_response,
                tool_name_map,
                log_missing_output=False,
            )
            if response.usage is None and usage is not None:
                response.usage = usage
            return response
        except HTTPException:
            if text_parts:
                return LLMResponse(content="".join(text_parts), usage=usage)
            if output_items:
                payload: dict[str, Any] = {"output": output_items}
                if usage is not None:
                    payload["usage"] = usage
                return _parse_responses_payload(payload, tool_name_map)
            logger.error(
                "Responses SSE final response missing message/function output: %s",
                _truncate_json(final_response),
            )
            raise
    if output_items:
        payload = {"output": output_items}
        if usage is not None:
            payload["usage"] = usage
        return _parse_responses_payload(payload, tool_name_map)
    if text_parts:
        return LLMResponse(content="".join(text_parts), usage=usage)
    logger.error("Responses SSE missing message/function output: %s", body[:2000])
    raise HTTPException(status_code=502, detail="Generic OAuth LLM returned no assistant output")


def _normalized_usage_from_event(event: dict[str, Any]) -> dict[str, int] | None:
    for candidate in (
        event.get("usage"),
        event.get("response"),
        event.get("message"),
    ):
        if not isinstance(candidate, dict):
            continue
        usage = _normalize_llm_usage(candidate.get("usage") if "usage" in candidate else candidate)
        if usage is not None:
            return usage
    return None


def _responses_sse_item_key(event: dict[str, Any], item: Any) -> str | None:
    item_id = event.get("item_id")
    if isinstance(item_id, str):
        return item_id
    if isinstance(item, dict):
        item_item_id = item.get("id")
        if isinstance(item_item_id, str):
            return item_item_id
        call_id = item.get("call_id")
        if isinstance(call_id, str):
            return call_id
    return _responses_sse_argument_key(event)


def _responses_sse_argument_key(event: dict[str, Any]) -> str | None:
    for key_name in ("item_id", "call_id", "output_index"):
        value = event.get(key_name)
        if isinstance(value, str):
            return value
        if isinstance(value, int):
            return str(value)
    return None


def _responses_sse_output_items(
    output_items_by_key: dict[str, dict[str, Any]],
    output_item_order: list[str],
    argument_deltas_by_key: dict[str, list[str]],
    done_arguments_by_key: dict[str, str],
) -> list[dict[str, Any]]:
    output_items: list[dict[str, Any]] = []
    for key in output_item_order:
        item = dict(output_items_by_key[key])
        if item.get("type") == "function_call":
            arguments = done_arguments_by_key.get(key)
            if arguments is None and key in argument_deltas_by_key:
                arguments = "".join(argument_deltas_by_key[key])
            if arguments is not None:
                item["arguments"] = arguments
        output_items.append(item)
    return output_items


def _parse_responses_payload(
    payload: dict[str, Any],
    tool_name_map: dict[str, str],
    *,
    log_missing_output: bool = True,
) -> LLMResponse:
    output = payload.get("output") or []
    if not isinstance(output, list):
        logger.error("Responses payload output is not a list: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail="Generic OAuth LLM returned invalid output")

    reverse_tool_name_map = {value: key for key, value in tool_name_map.items()}
    metadata = _responses_metadata_from_output(output)
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
                ),
                usage=_normalize_llm_usage(payload.get("usage")),
                metadata=metadata,
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
        return LLMResponse(
            content="".join(text_parts),
            usage=_normalize_llm_usage(payload.get("usage")),
            metadata=metadata,
        )
    if log_missing_output:
        logger.error(
            "Responses payload missing message/function output: %s", _truncate_json(payload)
        )
    raise HTTPException(status_code=502, detail="Generic OAuth LLM returned no assistant output")


def _is_no_responses_output_error(exc: HTTPException) -> bool:
    return exc.status_code == 502 and exc.detail == "Generic OAuth LLM returned no assistant output"


def _responses_reasoning_input_items_from_body(body: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    output = payload.get("output")
    if not isinstance(output, list):
        return []
    return _responses_reasoning_items_from_output(output)


def _responses_reasoning_summary(item: dict[str, Any]) -> list[Any]:
    summary = item.get("summary")
    return summary if isinstance(summary, list) else []


def _responses_reasoning_items_from_output(output: list[Any]) -> list[dict[str, Any]]:
    output_items: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        encrypted_content = item.get("encrypted_content")
        if not isinstance(encrypted_content, str) or not encrypted_content:
            continue
        stored_item: dict[str, Any] = {
            "type": "reasoning",
            "summary": _responses_reasoning_summary(item),
            "encrypted_content": encrypted_content,
        }
        item_id = item.get("id")
        if isinstance(item_id, str):
            stored_item["id"] = item_id
        output_items.append(stored_item)
    return output_items


def _responses_metadata_from_output(output: list[Any]) -> dict[str, Any] | None:
    output_items = _responses_reasoning_items_from_output(output)
    if not output_items:
        return None
    return {RESPONSES_OUTPUT_ITEMS_METADATA_KEY: output_items}


def _parse_chat_completion(payload: dict[str, Any], tool_name_map: dict[str, str]) -> LLMResponse:
    usage = _normalize_llm_usage(payload.get("usage"))
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
            ),
            usage=usage,
        )

    content = _extract_text_content(message)
    if content is None:
        logger.error("LLM response missing message content: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail="LLM provider returned no message content")
    return LLMResponse(content=str(content), usage=usage)


def _normalize_llm_usage(raw_usage: Any) -> dict[str, int] | None:
    if not isinstance(raw_usage, dict):
        return None

    usage: dict[str, int] = {}
    prompt_tokens = _usage_int(raw_usage, "prompt_tokens", "input_tokens", "input")
    completion_tokens = _usage_int(
        raw_usage,
        "completion_tokens",
        "output_tokens",
        "output",
    )
    total_tokens = _usage_int(raw_usage, "total_tokens", "totalTokens", "total")
    if prompt_tokens is not None:
        usage["prompt_tokens"] = prompt_tokens
        usage["input_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage["completion_tokens"] = completion_tokens
        usage["output_tokens"] = completion_tokens
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    elif prompt_tokens is not None and completion_tokens is not None:
        usage["total_tokens"] = prompt_tokens + completion_tokens

    prompt_details = raw_usage.get("prompt_tokens_details")
    input_details = raw_usage.get("input_tokens_details")
    cache_read_tokens = _usage_int(
        raw_usage,
        "cache_read_tokens",
        "cacheRead",
        "cache_read",
        "cache_read_input_tokens",
    )
    cache_write_tokens = _usage_int(
        raw_usage,
        "cache_write_tokens",
        "cacheWrite",
        "cache_write",
        "cache_creation_input_tokens",
    )
    if cache_read_tokens is None:
        cache_read_tokens = _usage_int(prompt_details, "cached_tokens")
    if cache_read_tokens is None:
        cache_read_tokens = _usage_int(input_details, "cached_tokens")
    if cache_write_tokens is None:
        cache_write_tokens = _usage_int(
            prompt_details,
            "cache_write_tokens",
            "cache_creation_tokens",
        )
    if cache_write_tokens is None:
        cache_write_tokens = _usage_int(
            input_details,
            "cache_write_tokens",
            "cache_creation_tokens",
        )
    if cache_read_tokens is not None:
        usage["cache_read_tokens"] = cache_read_tokens
    if cache_write_tokens is not None:
        usage["cache_write_tokens"] = cache_write_tokens

    return usage or None


def _usage_int(usage: Any, *keys: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


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


def _truncate_json(payload: dict[str, Any], limit: int = 4000) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, default=str)
    if len(serialized) <= limit:
        return serialized
    return serialized[:limit] + "...<truncated>"


def _stable_tool_order(tools: list[ToolSpec]) -> list[ToolSpec]:
    return sorted(tools, key=lambda tool: (_sanitize_tool_name(tool.name), tool.name))


def _canonical_jsonish(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_jsonish(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_jsonish(item) for item in value]
    return value


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
