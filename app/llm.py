from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app.models import ImagePart, LLMResponse, Message, MessageRole, TextPart, ToolCall, ToolSpec
from app.oauth import GENERIC_OAUTH_PROVIDER, GenericOAuthProvider

logger = logging.getLogger(__name__)
LLM_DEBUG_LOG_RESPONSES_ENV = "MINIGENT_LLM_DEBUG_LOG_RESPONSES"
LLM_DEBUG_LOG_RESPONSE_MAX_CHARS_ENV = "MINIGENT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS"
LLM_DEBUG_RESPONSE_LOG_PATH_ENV = "MINIGENT_LLM_DEBUG_RESPONSE_LOG_PATH"
LLM_DEBUG_REQUEST_LOG_PATH_ENV = "MINIGENT_LLM_DEBUG_REQUEST_LOG_PATH"
LLM_PROMPT_CACHE_KEY_ENV = "MINIGENT_LLM_PROMPT_CACHE_KEY"
LLM_REASONING_EFFORT_ENV = "MINIGENT_LLM_REASONING_EFFORT"
LLM_REASONING_SUMMARY_ENV = "MINIGENT_LLM_REASONING_SUMMARY"
LLM_MAX_TOOL_RESULT_CHARS_ENV = "MINIGENT_LLM_MAX_TOOL_RESULT_CHARS"
ANTHROPIC_MAX_TOKENS_ENV = "ANTHROPIC_MAX_TOKENS"
ANTHROPIC_VERSION_ENV = "ANTHROPIC_VERSION"
ANTHROPIC_THINKING_ENABLED_ENV = "ANTHROPIC_THINKING_ENABLED"
ANTHROPIC_THINKING_BUDGET_TOKENS_ENV = "ANTHROPIC_THINKING_BUDGET_TOKENS"
RESPONSES_OUTPUT_ITEMS_METADATA_KEY = "generic_oauth_responses_output_items"
ANTHROPIC_THINKING_BLOCKS_METADATA_KEY = "anthropic_thinking_blocks"
GEMINI_THOUGHT_SIGNATURE_METADATA_KEY = "gemini_thought_signature"
DEFAULT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS = 20000
DEFAULT_LLM_MAX_TOOL_RESULT_CHARS = 200000
DEFAULT_ANTHROPIC_MAX_TOKENS = 4096
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


ProgressSink = Callable[[int], Awaitable[None]] | None
_progress_sink_ctx: contextvars.ContextVar[ProgressSink] = contextvars.ContextVar(
    "progress_sink", default=None
)


@contextmanager
def llm_progress_sink(sink: Callable[[int], Awaitable[None]]) -> Iterator[None]:
    token = _progress_sink_ctx.set(sink)
    try:
        yield
    finally:
        _progress_sink_ctx.reset(token)


class LLMAdapter(ABC):
    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> LLMResponse:
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

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> LLMResponse:
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

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> LLMResponse:
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
            # Azure chat-completions can reject historical tool-result messages whose
            # matching tool calls are no longer in the provider's active context. For
            # that API shape we intentionally keep only the current trailing tool pair.
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
                provider = str(self.describe()["provider"])
                # OpenRouter may proxy to Azure and surface the same validation error;
                # retry once with the Azure-specific historical tool pruning.
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
                        provider,
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
                        logger.warning(
                            "Retried chat completion with Azure tool-history pruning failed provider=%s "
                            "base_url=%s",
                            provider,
                            self._base_url,
                        )
                        raise _provider_http_exception(
                            retry_exc,
                            provider=provider,
                            model=self._model,
                        ) from retry_exc
                    logger.info(
                        "Retried chat completion with Azure tool-history pruning succeeded provider=%s "
                        "base_url=%s",
                        provider,
                        self._base_url,
                    )
                    response = retry_response
                else:
                    raise _provider_http_exception(
                        exc,
                        provider=provider,
                        model=self._model,
                    ) from exc
            except httpx.HTTPError as exc:
                raise _provider_request_exception(
                    exc,
                    provider=str(self.describe()["provider"]),
                    model=self._model,
                ) from exc
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
        return await _post_json_with_progress(
            client,
            f"{self._base_url}/chat/completions",
            payload,
            headers,
        )


class GoogleGeminiAdapter(LLMAdapter):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._extra_headers = extra_headers or {}
        self._timeout = timeout
        self._transport = transport

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        tools = _stable_tool_order(tools)
        tool_name_map = _build_provider_tool_name_map(tools)
        payload = _messages_to_gemini_payload(
            messages,
            tools,
            tool_name_map,
            require_thought_signatures=_gemini_requires_thought_signatures(self._model),
            include_thoughts=_gemini_include_thought_summaries(self._model),
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
            **self._extra_headers,
        }
        model_path = self._model if self._model.startswith("models/") else f"models/{self._model}"
        _debug_log_llm_request(
            "google-gemini",
            payload,
            model=self._model,
            message_count=len(messages),
            tool_count=len(tools),
        )
        max_malformed_retries = 2
        for attempt in range(max_malformed_retries + 1):
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                try:
                    response = await _post_json_with_progress(
                        client,
                        f"{self._base_url}/{model_path}:generateContent",
                        payload,
                        headers,
                    )
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise _provider_http_exception(
                        exc, provider="gemini", model=self._model
                    ) from exc
                except httpx.HTTPError as exc:
                    raise _provider_request_exception(
                        exc, provider="gemini", model=self._model
                    ) from exc
            _debug_log_raw_llm_response(
                "google-gemini",
                response.text,
                content_type=response.headers.get("content-type"),
            )
            try:
                return _parse_gemini_response(response.json(), tool_name_map)
            except GeminiMalformedResponseError:
                if attempt < max_malformed_retries:
                    logger.warning(
                        "Gemini MALFORMED_RESPONSE, retrying attempt %d/%d",
                        attempt + 1,
                        max_malformed_retries,
                    )
                    continue
                logger.error("Gemini MALFORMED_RESPONSE after %d retries", max_malformed_retries)
                raise HTTPException(
                    status_code=502,
                    detail="Gemini provider returned malformed response after retries",
                ) from None
        # This should never be reached, but satisfies the type checker
        raise RuntimeError("Unexpected: loop completed without returning or raising")

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "google",
            "model": self._model,
            "base_url": self._base_url,
            "headers": sorted(self._extra_headers.keys()),
            "adapter": "GoogleGeminiAdapter",
        }


class AnthropicMessagesAdapter(LLMAdapter):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com/v1",
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_tokens: int = DEFAULT_ANTHROPIC_MAX_TOKENS,
        anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
        thinking_budget_tokens: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._extra_headers = extra_headers or {}
        self._timeout = timeout
        self._transport = transport
        self._max_tokens = max_tokens
        self._anthropic_version = anthropic_version
        self._thinking_budget_tokens = thinking_budget_tokens

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        tools = _stable_tool_order(tools)
        tool_name_map = _build_provider_tool_name_map(tools)
        system, anthropic_messages = _messages_to_anthropic_payload(messages, tool_name_map)
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            **self._extra_headers,
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        if self._thinking_budget_tokens is not None:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget_tokens,
            }
        if tools:
            payload["tools"] = [_tool_to_anthropic_payload(tool, tool_name_map) for tool in tools]
        _debug_log_llm_request(
            "anthropic",
            payload,
            model=self._model,
            message_count=len(messages),
            tool_count=len(tools),
        )
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await _post_json_with_progress(
                    client,
                    f"{self._base_url}/messages",
                    payload,
                    headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise _provider_http_exception(
                    exc, provider="anthropic", model=self._model
                ) from exc
            except httpx.HTTPError as exc:
                raise _provider_request_exception(
                    exc, provider="anthropic", model=self._model
                ) from exc
        _debug_log_raw_llm_response(
            "anthropic",
            response.text,
            content_type=response.headers.get("content-type"),
        )
        return _parse_anthropic_response(response.json(), tool_name_map)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "anthropic",
            "model": self._model,
            "base_url": self._base_url,
            "headers": sorted(self._extra_headers.keys()),
            "adapter": "AnthropicMessagesAdapter",
            "max_tokens": self._max_tokens,
            "thinking_budget_tokens": self._thinking_budget_tokens,
        }


def _provider_http_exception(
    exc: httpx.HTTPStatusError,
    *,
    provider: str,
    model: str,
) -> HTTPException:
    response = exc.response
    raw_body = response.text or str(exc)
    provider_error = _parse_provider_error_body(raw_body)
    error_obj = provider_error.get("error") if isinstance(provider_error.get("error"), dict) else {}
    upstream_code = _provider_error_field(error_obj, "code")
    upstream_type = _provider_error_field(error_obj, "type")
    upstream_status = _provider_error_field(error_obj, "status")
    retry_after_seconds = _extract_retry_after_seconds(response, error_obj)
    quota_violation = _extract_gemini_quota_violation(error_obj) if provider == "gemini" else {}
    error_type, status_code = _classify_provider_http_error(response.status_code, upstream_status)

    log_fields: dict[str, object] = {
        "event": error_type,
        "provider": provider,
        "model": model,
        "upstream_status": response.status_code,
    }
    if upstream_code is not None:
        log_fields["upstream_code"] = upstream_code
    if upstream_type:
        log_fields["upstream_error_type"] = upstream_type
    if upstream_status:
        log_fields["upstream_error_status"] = upstream_status
    if retry_after_seconds is not None:
        log_fields["retry_after_seconds"] = retry_after_seconds
    if quota_violation:
        log_fields.update(quota_violation)
    logger.warning("LLM provider request failed: %s", _format_log_fields(log_fields))
    if _env_bool(LLM_DEBUG_LOG_RESPONSES_ENV):
        logger.debug("LLM provider raw error response provider=%s body=%s", provider, raw_body)

    message = _provider_error_message(
        provider,
        error_type,
        retry_after_seconds=retry_after_seconds,
    )
    detail: dict[str, object] = {
        "type": error_type,
        "message": message,
        "provider": provider,
    }
    headers: dict[str, str] = {}
    if retry_after_seconds is not None:
        detail["retry_after_seconds"] = retry_after_seconds
        headers["Retry-After"] = str(retry_after_seconds)
    return HTTPException(status_code=status_code, detail=detail, headers=headers or None)


def _provider_request_exception(
    exc: httpx.HTTPError,
    *,
    provider: str,
    model: str,
) -> HTTPException:
    log_fields: dict[str, object] = {
        "event": "provider_request_failed",
        "provider": provider,
        "model": model,
        "exception_type": exc.__class__.__name__,
    }
    logger.warning("LLM provider request failed: %s", _format_log_fields(log_fields))
    if _env_bool(LLM_DEBUG_LOG_RESPONSES_ENV):
        logger.debug("LLM provider raw request error provider=%s error=%s", provider, exc)
    return HTTPException(
        status_code=502,
        detail={
            "type": "provider_request_failed",
            "message": f"{_provider_display_name(provider)} request failed.",
            "provider": provider,
        },
    )


def _classify_provider_http_error(
    upstream_status_code: int,
    upstream_status: object,
) -> tuple[str, int]:
    if upstream_status_code == 429 or upstream_status == "RESOURCE_EXHAUSTED":
        return "provider_rate_limited", 429
    if upstream_status_code in {401, 403}:
        return "provider_auth_failed", 502
    if upstream_status_code in {408, 504}:
        return "provider_timeout", 504
    if upstream_status_code >= 500:
        return "provider_unavailable", 503
    if upstream_status_code == 400:
        return "provider_bad_request", 502
    return "provider_error", 502


def _provider_error_message(
    provider: str,
    error_type: str,
    *,
    retry_after_seconds: int | None = None,
) -> str:
    display_name = _provider_display_name(provider)
    if error_type == "provider_rate_limited":
        if provider == "gemini":
            if retry_after_seconds is not None:
                return f"Gemini quota exceeded. Retry in about {retry_after_seconds}s."
            return "Gemini quota exceeded."
        if retry_after_seconds is not None:
            return f"{display_name} rate limit exceeded. Retry in about {retry_after_seconds}s."
        return f"{display_name} rate limit exceeded."
    if error_type == "provider_auth_failed":
        return f"{display_name} authentication failed. Check provider credentials."
    if error_type == "provider_timeout":
        return f"{display_name} request timed out."
    if error_type == "provider_unavailable":
        return f"{display_name} is temporarily unavailable."
    if error_type == "provider_bad_request":
        return f"{display_name} rejected the request."
    return f"{display_name} provider request failed."


def _provider_display_name(provider: str) -> str:
    return {
        "azure-openai": "Azure OpenAI",
        "gemini": "Gemini",
        "generic-oauth": "Generic OAuth LLM",
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "openai-compatible": "OpenAI-compatible provider",
        "openrouter": "OpenRouter",
    }.get(provider, provider)


def _provider_error_field(error_obj: object, field: str) -> object:
    if not isinstance(error_obj, dict):
        return None
    value = error_obj.get(field)
    if isinstance(value, str | int):
        return value
    return None


def _parse_provider_error_body(raw_body: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_retry_after_seconds(response: httpx.Response, error_obj: object) -> int | None:
    retry_after_header = response.headers.get("retry-after")
    if retry_after_header:
        try:
            return max(0, int(float(retry_after_header)))
        except ValueError:
            pass
    if not isinstance(error_obj, dict):
        return None
    details = error_obj.get("details")
    if not isinstance(details, list):
        return None
    for item in details:
        if not isinstance(item, dict):
            continue
        retry_delay = item.get("retryDelay")
        if isinstance(retry_delay, str):
            match = re.match(r"^(\d+(?:\.\d+)?)s$", retry_delay.strip())
            if match:
                return max(0, round(float(match.group(1))))
    return None


def _extract_gemini_quota_violation(error_obj: object) -> dict[str, object]:
    if not isinstance(error_obj, dict):
        return {}
    details = error_obj.get("details")
    if not isinstance(details, list):
        return {}
    for item in details:
        if not isinstance(item, dict):
            continue
        violations = item.get("violations")
        if not isinstance(violations, list) or not violations:
            continue
        violation = violations[0]
        if not isinstance(violation, dict):
            continue
        result: dict[str, object] = {}
        quota_metric = violation.get("quotaMetric")
        quota_id = violation.get("quotaId")
        quota_value = violation.get("quotaValue")
        if isinstance(quota_metric, str):
            result["quota_metric"] = quota_metric
        if isinstance(quota_id, str):
            result["quota_id"] = quota_id
        if isinstance(quota_value, str):
            result["quota_value"] = quota_value
        return result
    return {}


def _format_log_fields(fields: dict[str, object]) -> str:
    return " ".join(f"{key}={value!r}" for key, value in fields.items())


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

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> LLMResponse:
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
        request_messages = messages
        pruned_for_responses_tool_history = False
        if _is_chatgpt_codex_responses_url(self._url):
            # Responses input items are self-contained: a function_call_output must
            # have its matching function_call in the same request. Drop only malformed
            # orphaned tool messages; preserving complete historical pairs is important
            # so the model can see prior file reads and other workspace actions.
            request_messages = _prune_orphaned_responses_tool_messages(messages)
            pruned_for_responses_tool_history = request_messages != messages
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
            "instructions": _responses_instructions(request_messages),
            "input": _dedupe_responses_input_items(
                [
                    item
                    for message in request_messages
                    if message.role != MessageRole.SYSTEM
                    for item in _message_to_responses_payload(message, tool_name_map)
                ]
            ),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if tools:
            payload["tools"] = [_tool_to_responses_payload(tool, tool_name_map) for tool in tools]
        if _is_chatgpt_codex_responses_url(self._url):
            payload["include"] = ["reasoning.encrypted_content"]
            reasoning = _responses_request_reasoning_config()
            if reasoning is not None:
                payload["reasoning"] = reasoning
        prompt_cache_key = _prompt_cache_key_for_request(messages)
        if prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for attempt in range(2):
                _debug_log_llm_request(
                    "generic-oauth-responses",
                    payload,
                    model=self._model,
                    message_count=len(request_messages),
                    tool_count=len(tools),
                )
                if pruned_for_responses_tool_history:
                    logger.debug(
                        "Pruned orphaned tool messages from ChatGPT Codex Responses payload url=%s",
                        self._url,
                    )
                body, content_type = await _post_responses_request(
                    client,
                    self._url,
                    payload,
                    headers,
                    provider=GENERIC_OAUTH_PROVIDER,
                    model=self._model,
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


async def _emit_progress_chunk(chunk_len: int) -> None:
    sink = _progress_sink_ctx.get()
    if sink is not None:
        await sink(chunk_len)


async def _post_json_with_progress(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> httpx.Response:
    body_chunks: list[bytes] = []
    async with client.stream(
        "POST",
        url,
        json=payload,
        headers=headers,
    ) as response:
        async for chunk in response.aiter_bytes():
            body_chunks.append(chunk)
            await _emit_progress_chunk(len(chunk))
        return httpx.Response(
            response.status_code,
            headers=_headers_for_decoded_body(response.headers),
            content=b"".join(body_chunks),
            request=response.request,
            extensions=response.extensions,
        )


def _headers_for_decoded_body(headers: httpx.Headers) -> httpx.Headers:
    """Return headers safe for a response body already decoded by httpx streaming.

    httpx.Response.aiter_bytes()/aiter_text() yields decoded body bytes/text. If we
    rebuild a response with those decoded bytes while preserving compression headers,
    later response.text/response.json access can try to decode the body a second time
    and raise httpx.DecodingError.
    """
    decoded_headers = httpx.Headers(headers)
    for name in ("content-encoding", "content-length"):
        if name in decoded_headers:
            del decoded_headers[name]
    return decoded_headers


async def _post_responses_request(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    provider: str,
    model: str,
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
                request = response.request
                response = httpx.Response(
                    response.status_code,
                    headers=_headers_for_decoded_body(response.headers),
                    content=detail,
                    request=request,
                    extensions=response.extensions,
                )
                raise _provider_http_exception(
                    httpx.HTTPStatusError(
                        f"Provider returned HTTP {response.status_code}",
                        request=request,
                        response=response,
                    ),
                    provider=provider,
                    model=model,
                )
            try:
                async for chunk in response.aiter_text():
                    body_chunks.append(chunk)
                    await _emit_progress_chunk(len(chunk))
            except httpx.HTTPError as exc:
                read_error = exc
    except httpx.HTTPError as exc:
        raise _provider_request_exception(exc, provider=provider, model=model) from exc

    body = "".join(body_chunks)
    if read_error is not None and not body:
        raise _provider_request_exception(
            read_error, provider=provider, model=model
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
    if provider in {"google", "google-generative-ai", "gemini"}:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY or GOOGLE_API_KEY is required when MINIGENT_LLM_PROVIDER=google"
            )
        model = (
            os.getenv("GEMINI_MODEL")
            or os.getenv("GOOGLE_MODEL")
            or os.getenv("MINIGENT_LLM_MODEL")
            or "gemini-3.5-flash"
        )
        base_url = os.getenv(
            "GOOGLE_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta",
        )
        extra_headers = _json_string_map_env("MINIGENT_LLM_EXTRA_HEADERS")
        logger.info("LLM config: provider=%s model=%s base_url=%s", provider, model, base_url)
        return GoogleGeminiAdapter(
            api_key=api_key,
            model=model,
            base_url=base_url,
            extra_headers=extra_headers,
        )
    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when MINIGENT_LLM_PROVIDER=anthropic")
        model = (
            os.getenv("ANTHROPIC_MODEL") or os.getenv("MINIGENT_LLM_MODEL") or "claude-haiku-4-5"
        )
        base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        extra_headers = _json_string_map_env("MINIGENT_LLM_EXTRA_HEADERS")
        max_tokens = _env_int(ANTHROPIC_MAX_TOKENS_ENV, DEFAULT_ANTHROPIC_MAX_TOKENS)
        anthropic_version = os.getenv(ANTHROPIC_VERSION_ENV, DEFAULT_ANTHROPIC_VERSION)
        thinking_budget_tokens = _anthropic_thinking_budget_tokens_from_env()
        logger.info("LLM config: provider=%s model=%s base_url=%s", provider, model, base_url)
        return AnthropicMessagesAdapter(
            api_key=api_key,
            model=model,
            base_url=base_url,
            extra_headers=extra_headers,
            max_tokens=max_tokens,
            anthropic_version=anthropic_version,
            thinking_budget_tokens=thinking_budget_tokens,
        )

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
    serialized = json.dumps(result, ensure_ascii=True, default=str)
    return _truncate_tool_result_text(serialized)


def _max_tool_result_chars() -> int:
    return max(1024, _env_int(LLM_MAX_TOOL_RESULT_CHARS_ENV, DEFAULT_LLM_MAX_TOOL_RESULT_CHARS))


def _truncate_tool_result_text(text: str) -> str:
    max_chars = _max_tool_result_chars()
    if len(text) <= max_chars:
        return text
    marker = f"\n...[truncated tool result; original_chars={len(text)}]"
    return text[: max(0, max_chars - len(marker))] + marker


def _truncated_tool_result_payload(content: str) -> dict[str, Any] | None:
    max_chars = _max_tool_result_chars()
    if len(content) <= max_chars:
        return None
    return {
        "content": _truncate_tool_result_text(content),
        "truncated": True,
        "original_chars": len(content),
    }


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


def _anthropic_thinking_budget_tokens_from_env() -> int | None:
    enabled_raw = os.getenv(ANTHROPIC_THINKING_ENABLED_ENV, "").strip().lower()
    budget_raw = os.getenv(ANTHROPIC_THINKING_BUDGET_TOKENS_ENV, "").strip()
    if enabled_raw in {"0", "false", "no", "off", "none", "null"}:
        return None
    if not budget_raw:
        return 1024 if enabled_raw in {"1", "true", "yes", "on"} else None
    try:
        budget_tokens = int(budget_raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid integer value for %s", ANTHROPIC_THINKING_BUDGET_TOKENS_ENV
        )
        return None
    if budget_tokens <= 0:
        logger.warning("Ignoring non-positive value for %s", ANTHROPIC_THINKING_BUDGET_TOKENS_ENV)
        return None
    return budget_tokens


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


def _image_data_url(part: ImagePart) -> str:
    if part.url:
        return part.url
    if part.data:
        return f"data:{part.mime_type};base64,{part.data}"
    return ""


def _message_to_openai_content(message: Message) -> str | list[dict[str, Any]]:
    if not message.parts or message.role != MessageRole.USER:
        return message.content
    content: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            if part.text:
                content.append({"type": "text", "text": part.text})
            continue
        if isinstance(part, ImagePart):
            url = _image_data_url(part)
            if url:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": url, "detail": part.detail},
                    }
                )
    return content or message.content


def _gemini_parts_for_message(message: Message) -> list[dict[str, Any]]:
    if not message.parts:
        return [{"text": message.content}]
    parts: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            if part.text:
                parts.append({"text": part.text})
            continue
        if isinstance(part, ImagePart):
            if part.data:
                parts.append({"inline_data": {"mime_type": part.mime_type, "data": part.data}})
            elif part.url:
                parts.append({"file_data": {"mime_type": part.mime_type, "file_uri": part.url}})
    return parts or [{"text": message.content}]


def _message_to_chat_payload(message: Message, tool_name_map: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": message.role.value,
        "content": _message_to_openai_content(message),
    }
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


def _tool_to_anthropic_payload(tool: ToolSpec, tool_name_map: dict[str, str]) -> dict[str, Any]:
    return {
        "name": tool_name_map[tool.name],
        "description": tool.description,
        "input_schema": _canonical_jsonish(tool.input_schema),
    }


def _messages_to_anthropic_payload(
    messages: list[Message],
    tool_name_map: dict[str, str],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []
    for message in messages:
        if message.role == MessageRole.SYSTEM:
            if message.content:
                system_parts.append(message.content)
            continue
        if message.role == MessageRole.TOOL:
            anthropic_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id or "",
                            "content": message.content,
                        }
                    ],
                }
            )
            continue
        anthropic_messages.append(_message_to_anthropic_message(message, tool_name_map))
    return "\n\n".join(system_parts) or None, anthropic_messages


def _message_to_anthropic_message(
    message: Message,
    tool_name_map: dict[str, str],
) -> dict[str, Any]:
    role = "assistant" if message.role == MessageRole.ASSISTANT else "user"
    content = _message_to_anthropic_content(message)
    if message.role == MessageRole.ASSISTANT and message.tool_call_id and message.tool_name:
        thinking_blocks = _anthropic_thinking_blocks_from_metadata(message.metadata)
        if thinking_blocks:
            content = [*thinking_blocks, *content]
        if message.content and not any(part.get("type") == "text" for part in content):
            content.insert(len(thinking_blocks), {"type": "text", "text": message.content})
        content.append(
            {
                "type": "tool_use",
                "id": message.tool_call_id,
                "name": tool_name_map.get(
                    message.tool_name, _sanitize_tool_name(message.tool_name)
                ),
                "input": _canonical_jsonish(message.tool_arguments or {}),
            }
        )
    return {"role": role, "content": content or [{"type": "text", "text": message.content}]}


def _message_to_anthropic_content(message: Message) -> list[dict[str, Any]]:
    if not message.parts or message.role != MessageRole.USER:
        return [{"type": "text", "text": message.content}] if message.content else []
    content: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            if part.text:
                content.append({"type": "text", "text": part.text})
            continue
        if isinstance(part, ImagePart):
            image = _anthropic_image_block(part)
            if image:
                content.append(image)
    return content or ([{"type": "text", "text": message.content}] if message.content else [])


def _anthropic_image_block(part: ImagePart) -> dict[str, Any] | None:
    if part.data:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part.mime_type,
                "data": part.data,
            },
        }
    if part.url:
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": part.url,
            },
        }
    return None


def _parse_anthropic_response(
    payload: dict[str, Any], tool_name_map: dict[str, str]
) -> LLMResponse:
    content = payload.get("content") or []
    if not isinstance(content, list):
        logger.error("Anthropic response has invalid content: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail="Anthropic provider returned invalid content")
    reverse_tool_name_map = {value: key for key, value in tool_name_map.items()}
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
            continue
        if part_type == "tool_use":
            raw_name = part.get("name")
            if not isinstance(raw_name, str):
                continue
            raw_input = part.get("input") or {}
            if not isinstance(raw_input, dict):
                raise HTTPException(
                    status_code=502, detail="Anthropic provider returned non-object tool arguments"
                )
            tool_calls.append(
                ToolCall(
                    id=part.get("id") or f"anthropic-tool-call-{index}",
                    name=reverse_tool_name_map.get(raw_name, raw_name),
                    arguments=raw_input,
                )
            )
    usage = _normalize_llm_usage(payload.get("usage"))
    metadata = _anthropic_response_metadata(payload)
    if tool_calls:
        return LLMResponse(tool_calls=tool_calls, usage=usage, metadata=metadata)
    if text_parts:
        return LLMResponse(content="".join(text_parts), usage=usage, metadata=metadata)
    logger.error("Anthropic response missing text/tool content: %s", _truncate_json(payload))
    raise HTTPException(status_code=502, detail="Anthropic provider returned no message content")


def _anthropic_response_metadata(payload: dict[str, Any]) -> dict[str, Any] | None:
    metadata: dict[str, Any] = {}
    for key in ("id", "model", "role", "stop_reason", "stop_sequence", "type"):
        value = payload.get(key)
        if isinstance(value, str):
            metadata[key] = value
    content = payload.get("content")
    if isinstance(content, list):
        thinking_blocks = _anthropic_thinking_blocks_from_content(content)
        if thinking_blocks:
            metadata[ANTHROPIC_THINKING_BLOCKS_METADATA_KEY] = thinking_blocks
        reasoning_content = _anthropic_reasoning_content_from_blocks(thinking_blocks)
        if reasoning_content:
            metadata["reasoning_content"] = reasoning_content
    return metadata or None


def _anthropic_thinking_blocks_from_content(content: list[Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type not in {"thinking", "redacted_thinking"}:
            continue
        block = _canonical_jsonish(part)
        if isinstance(block, dict):
            blocks.append(block)
    return blocks


def _anthropic_reasoning_content_from_blocks(blocks: list[dict[str, Any]]) -> str | None:
    reasoning_parts: list[str] = []
    for block in blocks:
        if block.get("type") != "thinking":
            continue
        thinking = block.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            reasoning_parts.append(thinking.strip())
    return "\n\n".join(reasoning_parts) or None


def _anthropic_thinking_blocks_from_metadata(
    metadata: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not metadata:
        return []
    raw_blocks = metadata.get(ANTHROPIC_THINKING_BLOCKS_METADATA_KEY)
    if not isinstance(raw_blocks, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in raw_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"thinking", "redacted_thinking"}:
            blocks.append(_canonical_jsonish(block))
    return blocks


def _messages_to_gemini_payload(
    messages: list[Message],
    tools: list[ToolSpec],
    tool_name_map: dict[str, str],
    *,
    require_thought_signatures: bool = False,
    include_thoughts: bool = False,
) -> dict[str, Any]:
    system_parts: list[dict[str, str]] = []
    contents: list[dict[str, Any]] = []
    text_replayed_tool_call_ids: set[str] = set()
    for message in messages:
        if message.role == MessageRole.SYSTEM:
            if message.content:
                system_parts.append({"text": message.content})
            continue
        if message.role == MessageRole.USER:
            contents.append({"role": "user", "parts": _gemini_parts_for_message(message)})
            continue
        if message.role == MessageRole.ASSISTANT:
            if message.tool_name and message.tool_arguments is not None:
                function_call_part: dict[str, Any] = {
                    "functionCall": {
                        "name": tool_name_map.get(
                            message.tool_name,
                            _sanitize_tool_name(message.tool_name),
                        ),
                        "args": _canonical_jsonish(message.tool_arguments),
                    }
                }
                thought_signature = _gemini_thought_signature_from_metadata(message.metadata)
                if thought_signature is not None:
                    function_call_part["thoughtSignature"] = thought_signature
                elif require_thought_signatures:
                    if message.tool_call_id:
                        text_replayed_tool_call_ids.add(message.tool_call_id)
                    contents.append(
                        {
                            "role": "model",
                            "parts": [
                                {
                                    "text": _render_gemini_text_tool_call(
                                        message.tool_name,
                                        message.tool_arguments or {},
                                    )
                                }
                            ],
                        }
                    )
                    continue
                contents.append(
                    {
                        "role": "model",
                        "parts": [function_call_part],
                    }
                )
            elif message.content:
                contents.append({"role": "model", "parts": [{"text": message.content}]})
            continue
        if message.role == MessageRole.TOOL:
            if message.tool_call_id and message.tool_call_id in text_replayed_tool_call_ids:
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": _render_gemini_text_tool_result(message)}],
                    }
                )
                continue
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": tool_name_map.get(
                                    message.tool_name or "tool",
                                    _sanitize_tool_name(message.tool_name or "tool"),
                                ),
                                "response": _gemini_tool_response(message.content),
                            }
                        }
                    ],
                }
            )
    _ensure_gemini_contents_start_with_user(contents)
    payload: dict[str, Any] = {"contents": contents}
    if system_parts:
        payload["systemInstruction"] = {"parts": system_parts}
    if tools:
        payload["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool_name_map[tool.name],
                        "description": tool.description,
                        "parametersJsonSchema": _canonical_jsonish(tool.input_schema),
                    }
                    for tool in tools
                ]
            }
        ]
    if include_thoughts:
        payload["generationConfig"] = {"thinkingConfig": {"includeThoughts": True}}
    return payload


def _ensure_gemini_contents_start_with_user(contents: list[dict[str, Any]]) -> None:
    if not contents or contents[0].get("role") == "user":
        return
    contents.insert(
        0,
        {
            "role": "user",
            "parts": [
                {
                    "text": "Earlier thread history was compacted; continue from the summarized context."
                }
            ],
        },
    )


def _gemini_tool_response(content: str) -> dict[str, Any]:
    truncated = _truncated_tool_result_payload(content)
    if truncated is not None:
        return truncated
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"content": content}
    if isinstance(parsed, dict):
        return _canonical_jsonish(parsed)
    return {"result": parsed}


def _gemini_requires_thought_signatures(model: str) -> bool:
    normalized = model.removeprefix("models/").lower()
    return bool(re.match(r"gemini-3(?:\.|-)", normalized))


def _gemini_include_thought_summaries(model: str) -> bool:
    """Return whether Gemini requests should ask for displayable thought summaries."""
    summary = os.getenv(LLM_REASONING_SUMMARY_ENV, "auto").strip().lower()
    if summary in {"", "off", "none", "null", "false", "0"}:
        return False
    normalized = model.removeprefix("models/").lower()
    return bool(re.match(r"gemini-(?:3(?:\.|-)|2\.5(?:\.|-))", normalized))


def _render_gemini_text_tool_call(tool_name: str, arguments: dict[str, Any]) -> str:
    return "[tool_call]\n" + "\n".join(
        [
            f"name: {tool_name}",
            "arguments: " + json.dumps(arguments, ensure_ascii=True, sort_keys=True, default=str),
        ]
    )


def _render_gemini_text_tool_result(message: Message) -> str:
    lines = ["[tool_result]", f"name: {message.tool_name or 'unknown'}"]
    if message.tool_call_id:
        lines.append(f"id: {message.tool_call_id}")
    lines.append(_truncate_tool_result_text(message.content))
    return "\n".join(lines)


def _gemini_thought_signature_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    signature = metadata.get(GEMINI_THOUGHT_SIGNATURE_METADATA_KEY)
    return signature if isinstance(signature, str) and signature else None


def _gemini_tool_call_metadata(part: dict[str, Any]) -> dict[str, Any] | None:
    signature = part.get("thoughtSignature")
    if not isinstance(signature, str) or not signature:
        return None
    return {GEMINI_THOUGHT_SIGNATURE_METADATA_KEY: signature}


class GeminiMalformedResponseError(Exception):
    """Raised when Gemini returns a MALFORMED_RESPONSE finish reason."""

    pass


def _parse_gemini_response(payload: dict[str, Any], tool_name_map: dict[str, str]) -> LLMResponse:
    usage = _normalize_gemini_usage(payload.get("usageMetadata"))
    candidates = payload.get("candidates") or []
    if not candidates:
        logger.error("Gemini response missing candidates: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail="Gemini provider returned no candidates")
    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    if finish_reason == "MALFORMED_RESPONSE":
        logger.warning("Gemini returned MALFORMED_RESPONSE: %s", _truncate_json(payload))
        raise GeminiMalformedResponseError("Gemini returned MALFORMED_RESPONSE")
    content = candidate.get("content") or {}
    parts = content.get("parts") or []
    reverse_tool_name_map = {value: key for key, value in tool_name_map.items()}
    text_parts: list[str] = []
    thought_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            if part.get("thought") is True:
                thought_parts.append(text)
            else:
                text_parts.append(text)
        function_call = part.get("functionCall")
        if isinstance(function_call, dict):
            raw_name = function_call.get("name")
            raw_args = function_call.get("args") or {}
            if isinstance(raw_name, str) and isinstance(raw_args, dict):
                tool_calls.append(
                    ToolCall(
                        id=function_call.get("id") or f"gemini-tool-call-{index}",
                        name=reverse_tool_name_map.get(raw_name, raw_name),
                        arguments=raw_args,
                        metadata=_gemini_tool_call_metadata(part),
                    )
                )
    metadata = _gemini_response_metadata(thought_parts)
    if tool_calls:
        return LLMResponse(tool_calls=tool_calls, usage=usage, metadata=metadata)
    if text_parts:
        return LLMResponse(content="".join(text_parts), usage=usage, metadata=metadata)
    if thought_parts:
        return LLMResponse(content="", usage=usage, metadata=metadata)
    logger.error("Gemini response missing text/tool content: %s", _truncate_json(payload))
    raise HTTPException(status_code=502, detail="Gemini provider returned no message content")


def _gemini_response_metadata(thought_parts: list[str]) -> dict[str, Any] | None:
    reasoning_content = "".join(thought_parts).strip()
    if not reasoning_content:
        return None
    return {"reasoning_content": reasoning_content}


def _normalize_gemini_usage(raw_usage: Any) -> dict[str, int] | None:
    if not isinstance(raw_usage, dict):
        return None
    usage: dict[str, int] = {}
    prompt_tokens = _usage_int(raw_usage, "promptTokenCount")
    completion_tokens = _usage_int(raw_usage, "candidatesTokenCount")
    thoughts_tokens = _usage_int(raw_usage, "thoughtsTokenCount")
    total_tokens = _usage_int(raw_usage, "totalTokenCount")
    if prompt_tokens is not None:
        usage["prompt_tokens"] = prompt_tokens
        usage["input_tokens"] = prompt_tokens
    if completion_tokens is not None:
        output_tokens = completion_tokens + (thoughts_tokens or 0)
        usage["completion_tokens"] = output_tokens
        usage["output_tokens"] = output_tokens
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    elif prompt_tokens is not None and completion_tokens is not None:
        usage["total_tokens"] = prompt_tokens + completion_tokens + (thoughts_tokens or 0)
    return usage or None


def _is_chatgpt_codex_responses_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.netloc == "chatgpt.com" and parsed.path.rstrip("/") == "/backend-api/codex/responses"
    )


def _responses_request_reasoning_config() -> dict[str, str] | None:
    """Build optional Responses API reasoning settings for ChatGPT Codex.

    ChatGPT's Codex Responses endpoint defaults to hidden reasoning with no
    displayable summary. Request an automatic summary by default while allowing
    deployments to tune or disable this behavior via environment variables.
    """
    effort = os.getenv(LLM_REASONING_EFFORT_ENV, "medium").strip().lower()
    summary = os.getenv(LLM_REASONING_SUMMARY_ENV, "auto").strip().lower()

    reasoning: dict[str, str] = {}
    if effort not in {"", "off", "none", "null", "false", "0"}:
        reasoning["effort"] = effort
    if summary not in {"", "off", "none", "null", "false", "0"}:
        reasoning["summary"] = summary
    return reasoning or None


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


def _message_to_responses_content(message: Message) -> str | list[dict[str, Any]]:
    if not message.parts or message.role != MessageRole.USER:
        return message.content
    content: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            if part.text:
                content.append({"type": "input_text", "text": part.text})
            continue
        if isinstance(part, ImagePart):
            url = _image_data_url(part)
            if url:
                content.append({"type": "input_image", "image_url": url})
    return content or message.content


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
    payload_items.append({"role": role, "content": _message_to_responses_content(message)})
    return payload_items


def _dedupe_responses_input_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_reasoning_ids: set[str] = set()
    for item in items:
        item_id = item.get("id")
        if item.get("type") == "reasoning" and isinstance(item_id, str):
            if item_id in seen_reasoning_ids:
                continue
            seen_reasoning_ids.add(item_id)
        deduped.append(item)
    return deduped


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


def _prune_orphaned_responses_tool_messages(messages: list[Message]) -> list[Message]:
    # ChatGPT Codex Responses rejects a function_call_output if the same request does
    # not include the matching function_call. Unlike the Azure chat-completions helper
    # below, do not discard valid historical tool history here; doing so hides prior
    # tool use from the model and can make it repeat reads or deny visible tool calls.
    tool_call_counts: dict[str, dict[MessageRole, int]] = {}
    for message in messages:
        if not message.tool_call_id:
            continue
        counts = tool_call_counts.setdefault(message.tool_call_id, {})
        if message.role == MessageRole.ASSISTANT and message.tool_name:
            counts[MessageRole.ASSISTANT] = counts.get(MessageRole.ASSISTANT, 0) + 1
        elif message.role == MessageRole.TOOL:
            counts[MessageRole.TOOL] = counts.get(MessageRole.TOOL, 0) + 1

    complete_call_ids = {
        call_id
        for call_id, counts in tool_call_counts.items()
        if counts.get(MessageRole.ASSISTANT, 0) > 0 and counts.get(MessageRole.TOOL, 0) > 0
    }
    pruned: list[Message] = []
    dropped_messages = 0
    for message in messages:
        if message.tool_call_id and message.role in {MessageRole.ASSISTANT, MessageRole.TOOL}:
            if message.role == MessageRole.ASSISTANT and not message.tool_name:
                pruned.append(message)
                continue
            if message.tool_call_id not in complete_call_ids:
                dropped_messages += 1
                continue
        pruned.append(message)
    if dropped_messages:
        logger.debug(
            "Pruned %s orphaned tool-related message(s) from Responses payload",
            dropped_messages,
        )
    return pruned


def _prune_historical_tool_messages_for_azure(messages: list[Message]) -> list[Message]:
    # Azure chat-completions is stricter about old tool messages than the Responses
    # API path. This intentionally removes historical tool calls/results and keeps
    # only the active trailing pair, if the runtime is mid tool-response turn.
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
    current_reasoning_key: str | None = None
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
            if merged.get("type") == "reasoning":
                current_reasoning_key = item_key
        if event_type == "response.reasoning_summary_part.added":
            reasoning_key = item_key or current_reasoning_key
            part = event.get("part")
            if reasoning_key is not None and isinstance(part, dict):
                output_items_by_key.setdefault(reasoning_key, {"type": "reasoning"})
                output_items_by_key[reasoning_key].setdefault("summary", []).append(part)
                if reasoning_key not in output_item_order:
                    output_item_order.append(reasoning_key)
        if event_type == "response.reasoning_summary_text.delta" and isinstance(delta, str):
            reasoning_key = item_key or current_reasoning_key
            if reasoning_key is not None:
                reasoning_item = output_items_by_key.setdefault(
                    reasoning_key, {"type": "reasoning"}
                )
                summary = reasoning_item.setdefault("summary", [])
                if not isinstance(summary, list):
                    summary = []
                    reasoning_item["summary"] = summary
                if not summary or not isinstance(summary[-1], dict):
                    summary.append({"type": "summary_text", "text": ""})
                text = summary[-1].get("text")
                summary[-1]["text"] = (text if isinstance(text, str) else "") + delta
                if reasoning_key not in output_item_order:
                    output_item_order.append(reasoning_key)
        if event_type == "response.reasoning_summary_part.done":
            reasoning_key = item_key or current_reasoning_key
            part = event.get("part")
            if reasoning_key is not None and isinstance(part, dict):
                reasoning_item = output_items_by_key.setdefault(
                    reasoning_key, {"type": "reasoning"}
                )
                summary = reasoning_item.setdefault("summary", [])
                if isinstance(summary, list):
                    if summary and isinstance(summary[-1], dict):
                        summary[-1].update(part)
                    else:
                        summary.append(part)
                if reasoning_key not in output_item_order:
                    output_item_order.append(reasoning_key)
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
            if response.metadata is None:
                response.metadata = _responses_metadata_from_output(output_items)
            return response
        except HTTPException as exc:
            if not _is_no_responses_output_error(exc):
                raise
            if text_parts:
                return LLMResponse(
                    content="".join(text_parts),
                    usage=usage,
                    metadata=_responses_metadata_from_output(output_items),
                )
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
    if output_items and text_parts:
        return LLMResponse(
            content="".join(text_parts),
            usage=usage,
            metadata=_responses_metadata_from_output(output_items),
        )
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
    failure_detail = _responses_failure_detail(payload)
    if failure_detail is not None:
        logger.error("Responses payload failed: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail=failure_detail)

    output = payload.get("output") or []
    if not isinstance(output, list):
        logger.error("Responses payload output is not a list: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail="Generic OAuth LLM returned invalid output")

    reverse_tool_name_map = {value: key for key, value in tool_name_map.items()}
    metadata = _responses_metadata_from_output(output)
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
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
            tool_calls.append(
                ToolCall(
                    id=call_id,
                    name=reverse_tool_name_map.get(raw_name, raw_name),
                    arguments=arguments,
                )
            )
            continue
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
    if tool_calls:
        return LLMResponse(
            tool_calls=tool_calls,
            usage=_normalize_llm_usage(payload.get("usage")),
            metadata=metadata,
        )
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


def _responses_failure_detail(payload: dict[str, Any]) -> str | None:
    status = payload.get("status")
    if status not in {"failed", "incomplete", "cancelled"}:
        return None

    reason = ""
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        reason_parts = [part for part in (code, message) if isinstance(part, str) and part]
        reason = ": " + ": ".join(reason_parts) if reason_parts else ""

    incomplete_details = payload.get("incomplete_details")
    if not reason and isinstance(incomplete_details, dict):
        incomplete_reason = incomplete_details.get("reason")
        if isinstance(incomplete_reason, str) and incomplete_reason:
            reason = f": {incomplete_reason}"

    return f"Generic OAuth LLM response {status}{reason}"


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

    # Extract reasoning content if present (e.g., from DeepSeek R1 via OpenRouter)
    reasoning_content = message.get("reasoning")
    metadata: dict[str, Any] | None = None
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        metadata = {"reasoning_content": reasoning_content}

    if tool_calls:
        parsed_tool_calls: list[ToolCall] = []
        for raw_tool_call in tool_calls:
            if not isinstance(raw_tool_call, dict):
                continue
            function = raw_tool_call.get("function") or {}
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
            if not isinstance(parsed_arguments, dict):
                raise HTTPException(
                    status_code=502, detail="LLM returned non-object tool arguments"
                )
            parsed_tool_calls.append(
                ToolCall(
                    id=raw_tool_call.get("id"),
                    name=_resolve_internal_tool_name(function.get("name", ""), tool_name_map),
                    arguments=parsed_arguments,
                )
            )
        if parsed_tool_calls:
            return LLMResponse(tool_calls=parsed_tool_calls, usage=usage, metadata=metadata)

    content = _extract_text_content(message)
    if content is None:
        logger.error("LLM response missing message content: %s", _truncate_json(payload))
        raise HTTPException(status_code=502, detail="LLM provider returned no message content")
    return LLMResponse(content=str(content), usage=usage, metadata=metadata)


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
