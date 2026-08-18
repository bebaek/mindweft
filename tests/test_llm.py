import asyncio
import gzip
import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app.llm import (
    AnthropicMessagesAdapter,
    GenericOAuthResponsesAdapter,
    GoogleGeminiAdapter,
    LLMSettings,
    MockLLMAdapter,
    OpenAICompatibleAdapter,
    _prune_historical_tool_messages_for_azure,
    build_llm_adapter_from_env,
    llm_progress_sink,
    load_provider_config,
    serialize_tool_result,
)
from app.models import Message, MessageRole, ToolSpec
from app.oauth import OAuthCredentials
from app.tools import build_local_tool_registry


class FakeOAuthProvider:
    async def get_credentials(self) -> OAuthCredentials:
        return OAuthCredentials(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_at=9999999999.0,
        )


def test_openai_compatible_adapter_returns_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        payload = request.read().decode()
        assert '"model":"test-model"' in payload
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello from provider"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_local_tool_registry().specs(),
        )
    )

    assert response.content == "hello from provider"
    assert response.tool_call is None


def test_anthropic_adapter_returns_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(request.read().decode())
        assert payload["model"] == "claude-test"
        assert payload["max_tokens"] == 123
        assert payload["cache_control"] == {"type": "ephemeral"}
        assert payload["system"] == "be concise"
        assert payload["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        ]
        return httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "hello from anthropic"}],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 1024,
                    "cache_creation_input_tokens": 256,
                },
                "stop_reason": "end_turn",
            },
        )

    adapter = AnthropicMessagesAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="claude-test",
        max_tokens=123,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.SYSTEM, content="be concise"),
                Message(thread_id="thread", role=MessageRole.USER, content="hello"),
            ],
            [],
        )
    )

    assert response.content == "hello from anthropic"
    assert response.tool_call is None
    assert response.usage == {
        "prompt_tokens": 7,
        "input_tokens": 7,
        "completion_tokens": 3,
        "output_tokens": 3,
        "total_tokens": 10,
        "cache_read_tokens": 1024,
        "cache_write_tokens": 256,
    }
    assert response.metadata["stop_reason"] == "end_turn"


def test_anthropic_adapter_can_disable_prompt_cache() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert "cache_control" not in payload
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    adapter = AnthropicMessagesAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="claude-test",
        prompt_cache_enabled=False,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate([Message(thread_id="thread", role=MessageRole.USER, content="hello")], [])
    )

    assert response.content == "ok"


def test_anthropic_adapter_sends_thinking_config_and_extracts_reasoning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1024}
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "I should answer briefly.",
                        "signature": "opaque-signature",
                    },
                    {"type": "text", "text": "brief answer"},
                ]
            },
        )

    adapter = AnthropicMessagesAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="claude-test",
        thinking_budget_tokens=1024,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate([Message(thread_id="thread", role=MessageRole.USER, content="hello")], [])
    )

    assert response.content == "brief answer"
    assert response.metadata["reasoning_content"] == "I should answer briefly."
    assert response.metadata["anthropic_thinking_blocks"] == [
        {
            "type": "thinking",
            "thinking": "I should answer briefly.",
            "signature": "opaque-signature",
        }
    ]


@pytest.mark.parametrize(
    "model",
    (
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-5",
        "anthropic/claude-opus-5",
    ),
)
def test_anthropic_modern_models_use_adaptive_thinking_and_effort(model: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert payload["thinking"] == {
            "type": "adaptive",
            "display": "summarized",
        }
        assert payload["output_config"] == {"effort": "medium"}
        assert "budget_tokens" not in payload["thinking"]
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": "A short summary.", "signature": "sig"},
                    {"type": "text", "text": "answer"},
                ]
            },
        )

    adapter = AnthropicMessagesAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model=model,
        thinking_budget_tokens=1024,
        thinking_effort="medium",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate([Message(thread_id="thread", role=MessageRole.USER, content="hello")], [])
    )

    assert response.content == "answer"
    assert response.metadata["reasoning_content"] == "A short summary."
    assert adapter.describe()["thinking_mode"] == "adaptive"


def test_anthropic_adapter_replays_thinking_blocks_before_tool_use() -> None:
    seen_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        seen_payloads.append(payload)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "done"}]},
        )

    adapter = AnthropicMessagesAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="use a tool"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="current_time",
                    tool_call_id="toolu_123",
                    tool_arguments={"timezone": "UTC"},
                    metadata={
                        "anthropic_thinking_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "Need current time.",
                                "signature": "opaque-signature",
                            }
                        ]
                    },
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"time":"now"}',
                    tool_name="current_time",
                    tool_call_id="toolu_123",
                ),
            ],
            [],
        )
    )

    assert response.content == "done"
    assert seen_payloads[0]["messages"][1]["content"] == [
        {
            "type": "thinking",
            "thinking": "Need current time.",
            "signature": "opaque-signature",
        },
        {
            "type": "tool_use",
            "id": "toolu_123",
            "name": "current_time",
            "input": {"timezone": "UTC"},
        },
    ]


def test_anthropic_adapter_sends_image_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        content = payload["messages"][0]["content"]
        assert content == [
            {"type": "text", "text": "describe it"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "aGk=",
                },
            },
        ]
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "image reply"}]},
        )

    adapter = AnthropicMessagesAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.USER,
                    content="describe it",
                    parts=[
                        {"type": "text", "text": "describe it"},
                        {
                            "type": "image",
                            "mime_type": "image/png",
                            "data": "aGk=",
                            "detail": "low",
                        },
                    ],
                )
            ],
            [],
        )
    )

    assert response.content == "image reply"


def test_anthropic_adapter_returns_tool_call_and_replays_tool_result() -> None:
    seen_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        seen_payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_123",
                        "name": "current_time",
                        "input": {"timezone": "UTC"},
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )

    adapter = AnthropicMessagesAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="what time is it?"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="current_time",
                    tool_call_id="toolu_old",
                    tool_arguments={},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"time":"now"}',
                    tool_name="current_time",
                    tool_call_id="toolu_old",
                ),
            ],
            [
                ToolSpec(
                    name="current_time",
                    description="Return the current time",
                    input_schema={"type": "object", "properties": {}},
                )
            ],
        )
    )

    assert seen_payloads[0]["tools"] == [
        {
            "name": "current_time",
            "description": "Return the current time",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    assert seen_payloads[0]["messages"][1]["content"] == [
        {"type": "tool_use", "id": "toolu_old", "name": "current_time", "input": {}}
    ]
    assert seen_payloads[0]["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_old", "content": '{"time":"now"}'}
    ]
    assert response.tool_call is not None
    assert response.tool_call.id == "toolu_123"
    assert response.tool_call.name == "current_time"
    assert response.tool_call.arguments == {"timezone": "UTC"}


def test_anthropic_adapter_normalizes_rate_limit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "5"},
            json={"error": {"type": "rate_limit_error", "message": "slow down"}},
        )

    adapter = AnthropicMessagesAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.headers == {"Retry-After": "5"}
    assert exc.detail == {
        "type": "provider_rate_limited",
        "message": "Anthropic rate limit exceeded. Retry in about 5s.",
        "provider": "anthropic",
        "retry_after_seconds": 5,
    }


def test_openai_compatible_adapter_sends_image_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        content = payload["messages"][0]["content"]
        assert content == [
            {"type": "text", "text": "describe it"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,aGk=",
                    "detail": "low",
                },
            },
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "image reply"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.USER,
                    content="describe it",
                    parts=[
                        {"type": "text", "text": "describe it"},
                        {
                            "type": "image",
                            "mime_type": "image/png",
                            "data": "aGk=",
                            "detail": "low",
                        },
                    ],
                )
            ],
            [],
        )
    )

    assert response.content == "image reply"


def test_openai_compatible_adapter_emits_progress_for_response_chunks() -> None:
    progress: list[int] = []

    async def collect_progress(chunk_len: int) -> None:
        progress.append(chunk_len)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello from provider"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    async def run() -> None:
        with llm_progress_sink(collect_progress):
            await adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )

    asyncio.run(run())

    assert progress
    assert sum(progress) > 0


def test_openai_adapter_normalizes_provider_rate_limit_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "12"},
            json={
                "error": {
                    "message": "raw rate limit details",
                    "type": "rate_limit_error",
                    "code": "rate_limit_exceeded",
                }
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level("WARNING", logger="app.llm"):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                adapter.generate(
                    [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                    [],
                )
            )

    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.headers == {"Retry-After": "12"}
    assert exc.detail == {
        "type": "provider_rate_limited",
        "message": "OpenAI rate limit exceeded. Retry in about 12s.",
        "provider": "openai",
        "retry_after_seconds": 12,
    }
    assert "upstream_error_type='rate_limit_error'" in caplog.text
    assert "upstream_code='rate_limit_exceeded'" in caplog.text
    assert "raw rate limit details" not in caplog.text


def test_openrouter_adapter_normalizes_provider_unavailable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"message": "raw upstream outage", "code": "provider_unavailable"}},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "type": "provider_unavailable",
        "message": "OpenRouter is temporarily unavailable.",
        "provider": "openrouter",
    }


def test_generic_oauth_adapter_normalizes_provider_bad_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINDWEFT_LLM_ACCOUNT_ID_HEADER", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer access-token"
        return httpx.Response(
            400,
            json={"error": {"message": "raw invalid payload", "type": "invalid_request_error"}},
        )

    adapter = GenericOAuthResponsesAdapter(
        url="https://example.com/responses",
        model="test-model",
        oauth_provider=FakeOAuthProvider(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {
        "type": "provider_bad_request",
        "message": "Generic OAuth LLM rejected the request.",
        "provider": "generic-oauth",
    }


def test_generic_oauth_codex_responses_requests_reasoning_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINDWEFT_LLM_ACCOUNT_ID_HEADER", raising=False)
    monkeypatch.delenv("MINDWEFT_LLM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("MINDWEFT_LLM_REASONING_SUMMARY", raising=False)
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
                'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n'
            ),
        )

    adapter = GenericOAuthResponsesAdapter(
        url="https://chatgpt.com/backend-api/codex/responses",
        model="gpt-5.5",
        oauth_provider=FakeOAuthProvider(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "hello"
    assert captured_payload["include"] == ["reasoning.encrypted_content"]
    assert captured_payload["reasoning"] == {"effort": "medium", "summary": "auto"}


def test_generic_oauth_responses_extracts_streamed_reasoning_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINDWEFT_LLM_ACCOUNT_ID_HEADER", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request.read()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(
                [
                    'data: {"type":"response.output_item.added","item":{"id":"rs_1","type":"reasoning","encrypted_content":"opaque"}}\n\n',
                    'data: {"type":"response.reasoning_summary_part.added","item_id":"rs_1","part":{"type":"summary_text","text":""}}\n\n',
                    'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs_1","delta":"I checked"}\n\n',
                    'data: {"type":"response.reasoning_summary_part.done","item_id":"rs_1","part":{"type":"summary_text","text":"I checked"}}\n\n',
                    'data: {"type":"response.output_item.done","item":{"id":"rs_1","type":"reasoning","encrypted_content":"opaque","summary":[{"type":"summary_text","text":"I checked"}]}}\n\n',
                    'data: {"type":"response.output_text.delta","delta":"done"}\n\n',
                    'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n',
                ]
            ),
        )

    adapter = GenericOAuthResponsesAdapter(
        url="https://chatgpt.com/backend-api/codex/responses",
        model="gpt-5.5",
        oauth_provider=FakeOAuthProvider(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "done"
    assert response.metadata == {
        "generic_oauth_responses_output_items": [
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "I checked"}],
                "encrypted_content": "opaque",
                "id": "rs_1",
            }
        ]
    }


def test_google_gemini_adapter_returns_text_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/v1beta/models/gemini-test:generateContent"
        assert request.headers["x-goog-api-key"] == "test-key"
        payload = json.loads(request.read().decode())
        assert payload["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hello from gemini"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 7,
                },
            },
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "hello from gemini"
    assert response.tool_call is None
    assert response.usage == {
        "prompt_tokens": 3,
        "input_tokens": 3,
        "completion_tokens": 4,
        "output_tokens": 4,
        "total_tokens": 7,
    }


def test_google_gemini_adapter_handles_compressed_response() -> None:
    raw_body = json.dumps(
        {"candidates": [{"content": {"parts": [{"text": "compressed gemini"}]}}]}
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(raw_body),
            headers={"content-encoding": "gzip", "content-length": "999999"},
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "compressed gemini"


def test_google_gemini_adapter_requests_and_returns_thought_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert payload["generationConfig"] == {"thinkingConfig": {"includeThoughts": True}}
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "I checked the arithmetic.", "thought": True},
                                {"text": "The answer is 4."},
                            ]
                        }
                    }
                ]
            },
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-3.5-flash",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="2+2?")],
            [],
        )
    )

    assert response.content == "The answer is 4."
    assert response.metadata == {"reasoning_content": "I checked the arithmetic."}


def test_google_gemini_adapter_sends_image_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert payload["contents"] == [
            {
                "role": "user",
                "parts": [
                    {"text": "describe it"},
                    {"inline_data": {"mime_type": "image/png", "data": "aGk="}},
                ],
            }
        ]
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "image reply"}]}}]},
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.USER,
                    content="describe it",
                    parts=[
                        {"type": "text", "text": "describe it"},
                        {"type": "image", "mime_type": "image/png", "data": "aGk="},
                    ],
                )
            ],
            [],
        )
    )

    assert response.content == "image reply"


def test_google_gemini_adapter_emits_progress_for_response_chunks() -> None:
    progress: list[int] = []

    async def collect_progress(chunk_len: int) -> None:
        progress.append(chunk_len)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "hello from gemini"}]}}]},
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    async def run() -> None:
        with llm_progress_sink(collect_progress):
            await adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )

    asyncio.run(run())

    assert progress
    assert sum(progress) > 0


def test_google_gemini_adapter_sanitizes_quota_errors(caplog: pytest.LogCaptureFixture) -> None:
    raw_error = {
        "error": {
            "code": 429,
            "message": "You exceeded your current quota, please check billing details.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                            "quotaValue": "5",
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "51s",
                },
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=raw_error)

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level("WARNING", logger="app.llm"):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                adapter.generate(
                    [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                    [],
                )
            )

    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.headers == {"Retry-After": "51"}
    assert exc.detail == {
        "type": "provider_rate_limited",
        "message": "Gemini quota exceeded. Retry in about 51s.",
        "provider": "gemini",
        "retry_after_seconds": 51,
    }
    assert "quota_metric='generativelang...<truncated>" in caplog.text
    assert "You exceeded your current quota" not in caplog.text


def test_google_gemini_adapter_sanitizes_non_rate_provider_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "raw upstream details",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {
        "type": "provider_bad_request",
        "message": "Gemini rejected the request.",
        "provider": "gemini",
    }


def test_google_gemini_adapter_returns_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        declarations = payload["tools"][0]["functionDeclarations"]
        assert declarations[0]["name"] == "calculator"
        assert declarations[0]["parametersJsonSchema"]["type"] == "object"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "I need a calculator.", "thought": True},
                                {
                                    "functionCall": {
                                        "name": "calculator",
                                        "args": {"expression": "2+2"},
                                    },
                                    "thoughtSignature": "signature-123",
                                },
                            ]
                        }
                    }
                ]
            },
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="models/gemini-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="calculate")],
            build_local_tool_registry(allowed_tools=["calculator"]).specs(),
        )
    )

    assert response.content is None
    assert response.tool_call is not None
    assert response.tool_call.name == "calculator"
    assert response.tool_call.arguments == {"expression": "2+2"}
    assert response.tool_call.metadata == {"gemini_thought_signature": "signature-123"}
    assert response.metadata == {"reasoning_content": "I need a calculator."}


def test_google_gemini_adapter_replays_tool_call_thought_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        function_call_part = payload["contents"][1]["parts"][0]
        assert function_call_part["thoughtSignature"] == "signature-123"
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "done"}]}}]},
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="calculator",
                    tool_call_id="gemini-tool-call-0",
                    tool_arguments={"expression": "2+2"},
                    metadata={"gemini_thought_signature": "signature-123"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"result":4}',
                    tool_name="calculator",
                    tool_call_id="gemini-tool-call-0",
                ),
            ],
            build_local_tool_registry(allowed_tools=["calculator"]).specs(),
        )
    )

    assert response.content == "done"


def test_google_gemini3_adapter_text_replays_tool_call_when_thought_signature_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        tool_call_content = payload["contents"][1]
        tool_result_content = payload["contents"][2]
        assert tool_call_content == {
            "role": "model",
            "parts": [{"text": '[tool_call]\nname: calculator\narguments: {"expression": "2+2"}'}],
        }
        assert tool_result_content == {
            "role": "user",
            "parts": [{"text": '[tool_result]\nname: calculator\nid: old-call\n{"result":4}'}],
        }
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "done"}]}}]},
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-3.5-flash",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="calculator",
                    tool_call_id="old-call",
                    tool_arguments={"expression": "2+2"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"result":4}',
                    tool_name="calculator",
                    tool_call_id="old-call",
                ),
            ],
            build_local_tool_registry(allowed_tools=["calculator"]).specs(),
        )
    )

    assert response.content == "done"


def test_google_gemini_adapter_prepends_user_context_for_compacted_leading_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        assert payload["contents"][0] == {
            "role": "user",
            "parts": [
                {
                    "text": "Earlier thread history was compacted; continue from the summarized context."
                }
            ],
        }
        assert payload["contents"][1]["role"] == "model"
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "done"}]}}]},
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-3.5-flash",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="previous answer",
                ),
                Message(thread_id="thread", role=MessageRole.USER, content="next prompt"),
            ],
            [],
        )
    )

    assert response.content == "done"


def test_google_gemini_adapter_truncates_large_tool_result_for_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINDWEFT_LLM_MAX_TOOL_RESULT_CHARS", "1024")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        response_payload = payload["contents"][2]["parts"][0]["functionResponse"]["response"]

        assert response_payload["truncated"] is True
        assert response_payload["original_chars"] > 1024
        assert len(response_payload["content"]) <= 1024
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "done"}]}}]},
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="search_files",
                    tool_call_id="call-1",
                    tool_arguments={"pattern": "*"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content=json.dumps({"content": "x" * 2000}),
                    tool_name="search_files",
                    tool_call_id="call-1",
                ),
            ],
            [],
        )
    )

    assert response.content == "done"


def test_serialize_tool_result_truncates_large_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWEFT_LLM_MAX_TOOL_RESULT_CHARS", "1024")

    serialized = serialize_tool_result({"content": "x" * 2000})

    assert len(serialized) <= 1024
    assert "truncated tool result" in serialized


def test_mock_llm_adapter_supports_json_tool_arguments() -> None:
    adapter = MockLLMAdapter()

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.USER,
                    content='/tool retrieve_knowledge {"query":"token refresh","top_k":3}',
                )
            ],
            build_local_tool_registry(allowed_tools=["retrieve_knowledge"]).specs(),
        )
    )

    assert response.content is None
    assert response.tool_call is not None
    assert response.tool_call.name == "retrieve_knowledge"
    assert response.tool_call.arguments == {"query": "token refresh", "top_k": 3}


def test_mock_llm_adapter_maps_plain_retrieve_knowledge_payload_to_query() -> None:
    adapter = MockLLMAdapter()

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="thread",
                    role=MessageRole.USER,
                    content="/tool retrieve_knowledge token refresh",
                )
            ],
            build_local_tool_registry(allowed_tools=["retrieve_knowledge"]).specs(),
        )
    )

    assert response.content is None
    assert response.tool_call is not None
    assert response.tool_call.name == "retrieve_knowledge"
    assert response.tool_call.arguments == {"query": "token refresh"}


def test_openai_compatible_adapter_returns_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text":"hello from tool"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_local_tool_registry().specs(),
        )
    )

    assert response.content is None
    assert response.tool_call is not None
    assert response.tool_call.id == "call_123"
    assert response.tool_call.name == "echo"
    assert response.tool_call.arguments == {"text": "hello from tool"}


def test_openai_compatible_adapter_returns_multiple_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"text":"one"}',
                                    },
                                },
                                {
                                    "id": "call_2",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": '{"expression":"1+1"}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_local_tool_registry().specs(),
        )
    )

    assert [tool_call.id for tool_call in response.tool_calls] == ["call_1", "call_2"]
    assert [tool_call.name for tool_call in response.tool_calls] == ["echo", "calculator"]
    assert response.tool_call == response.tool_calls[0]


def test_load_provider_config_for_openrouter_includes_optional_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://example.com")
    monkeypatch.setenv("OPENROUTER_APP_NAME", "minigent")

    config = load_provider_config("openrouter")

    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.extra_headers == {
        "HTTP-Referer": "https://example.com",
        "X-OpenRouter-Title": "minigent",
    }


def test_llm_settings_prefer_mindweft_and_accept_legacy_env() -> None:
    preferred = LLMSettings.from_env(
        {
            "MINDWEFT_LLM_PROVIDER": "google",
            "MINIGENT_LLM_PROVIDER": "openai",
            "MINDWEFT_LLM_REASONING_EFFORT": "high",
            "MINIGENT_LLM_REASONING_EFFORT": "low",
        }
    )
    legacy = LLMSettings.from_env(
        {
            "MINIGENT_LLM_PROVIDER": "openai",
            "MINIGENT_LLM_REASONING_EFFORT": "low",
        }
    )

    assert preferred.provider == "google"
    assert preferred.runtime.reasoning_effort == "high"
    assert legacy.provider == "openai"
    assert legacy.runtime.reasoning_effort == "low"


def test_build_llm_adapter_from_env_supports_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWEFT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    adapter = build_llm_adapter_from_env()

    assert isinstance(adapter, OpenAICompatibleAdapter)


def test_build_llm_adapter_from_env_supports_google(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWEFT_LLM_PROVIDER", "google")
    monkeypatch.setenv("GEMINI_API_KEY", "google-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")

    adapter = build_llm_adapter_from_env()

    assert isinstance(adapter, GoogleGeminiAdapter)
    assert adapter.describe()["model"] == "gemini-test"


def test_build_llm_adapter_from_env_supports_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWEFT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "99")
    monkeypatch.setenv("ANTHROPIC_THINKING_BUDGET_TOKENS", "1024")
    monkeypatch.setenv("ANTHROPIC_THINKING_EFFORT", "medium")
    monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE_ENABLED", "false")

    adapter = build_llm_adapter_from_env()

    assert isinstance(adapter, AnthropicMessagesAdapter)
    assert adapter.describe()["model"] == "claude-test"
    assert adapter.describe()["max_tokens"] == 99
    assert adapter.describe()["thinking_budget_tokens"] == 1024
    assert adapter.describe()["thinking_effort"] == "medium"
    assert adapter.describe()["prompt_cache_enabled"] is False


def test_build_llm_adapter_from_env_rejects_missing_anthropic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINDWEFT_LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_llm_adapter_from_env()


def test_build_llm_adapter_from_env_rejects_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWEFT_LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_llm_adapter_from_env()


def test_openai_compatible_adapter_raises_for_invalid_tool_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "echo",
                                        "arguments": "{bad json",
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException, match="invalid tool arguments"):
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                build_local_tool_registry().specs(),
            )
        )


def test_openai_compatible_adapter_supports_list_content_parts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "output_text", "text": "Hello"},
                                {"type": "output_text", "text": " from parts"},
                            ]
                        }
                    }
                ]
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_local_tool_registry().specs(),
        )
    )

    assert response.content == "Hello from parts"


def test_openai_compatible_adapter_supports_output_text_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"output_text": "Hello from output_text"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_local_tool_registry().specs(),
        )
    )

    assert response.content == "Hello from output_text"


def test_openai_compatible_adapter_sends_tool_call_id_for_tool_messages() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="hello"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="echo",
                    tool_call_id="call_123",
                    tool_arguments={"text": "hello"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"echo": "hello"}',
                    tool_name="echo",
                    tool_call_id="call_123",
                ),
            ],
            build_local_tool_registry().specs(),
        )
    )

    assert seen_payload["messages"][1]["tool_calls"][0]["id"] == "call_123"
    assert seen_payload["messages"][1]["tool_calls"][0]["function"]["name"] == "echo"
    assert seen_payload["messages"][2]["tool_call_id"] == "call_123"
    assert "name" not in seen_payload["messages"][2]


def test_openai_compatible_adapter_prunes_historical_tool_messages_for_azure() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example-resource.openai.azure.com/openai/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="weather today"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="echo",
                    tool_call_id="call_123",
                    tool_arguments={"text": "tool input"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"echo": "tool output"}',
                    tool_name="echo",
                    tool_call_id="call_123",
                ),
                Message(
                    thread_id="thread", role=MessageRole.ASSISTANT, content="It is warm today."
                ),
                Message(
                    thread_id="thread", role=MessageRole.USER, content="current home temperature"
                ),
            ],
            build_local_tool_registry().specs(),
        )
    )

    assert [message["role"] for message in seen_payload["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert seen_payload["messages"][1]["content"] == "It is warm today."


def test_openai_compatible_adapter_keeps_active_tool_messages_for_azure() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example-resource.openai.azure.com/openai/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="weather today"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="echo",
                    tool_call_id="call_123",
                    tool_arguments={"text": "tool input"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"echo": "tool output"}',
                    tool_name="echo",
                    tool_call_id="call_123",
                ),
            ],
            build_local_tool_registry().specs(),
        )
    )

    assert [message["role"] for message in seen_payload["messages"]] == [
        "user",
        "assistant",
        "tool",
    ]
    assert seen_payload["messages"][1]["tool_calls"][0]["id"] == "call_123"
    assert seen_payload["messages"][2]["tool_call_id"] == "call_123"


def test_openai_compatible_adapter_keeps_historical_tool_messages_for_non_azure() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="weather today"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="echo",
                    tool_call_id="call_123",
                    tool_arguments={"text": "tool input"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"echo": "tool output"}',
                    tool_name="echo",
                    tool_call_id="call_123",
                ),
                Message(
                    thread_id="thread", role=MessageRole.ASSISTANT, content="It is warm today."
                ),
                Message(
                    thread_id="thread", role=MessageRole.USER, content="current home temperature"
                ),
            ],
            build_local_tool_registry().specs(),
        )
    )

    assert [message["role"] for message in seen_payload["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]


def test_openai_compatible_adapter_retries_openrouter_with_azure_tool_history_pruning() -> None:
    seen_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Provider returned error",
                        "code": 400,
                        "metadata": {
                            "raw": (
                                '{\n  "error": {\n    "message": '
                                '"No tool call found for function call output with call_id '
                                'call_123.",\n    "type": "invalid_request_error",\n    '
                                '"param": "input",\n    "code": null\n  }\n}'
                            ),
                            "provider_name": "Azure",
                            "is_byok": False,
                        },
                    }
                },
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok after retry"}}]},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="weather today"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="echo",
                    tool_call_id="call_123",
                    tool_arguments={"text": "tool input"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"echo": "tool output"}',
                    tool_name="echo",
                    tool_call_id="call_123",
                ),
                Message(
                    thread_id="thread", role=MessageRole.ASSISTANT, content="It is warm today."
                ),
                Message(
                    thread_id="thread", role=MessageRole.USER, content="current home temperature"
                ),
            ],
            build_local_tool_registry().specs(),
        )
    )

    assert response.content == "ok after retry"
    assert len(seen_payloads) == 2
    assert [message["role"] for message in seen_payloads[0]["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert [message["role"] for message in seen_payloads[1]["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert seen_payloads[1]["messages"][1]["content"] == "It is warm today."


def test_prune_historical_tool_messages_for_azure_drops_orphaned_tool_results() -> None:
    pruned = _prune_historical_tool_messages_for_azure(
        [
            Message(thread_id="thread", role=MessageRole.USER, content="weather today"),
            Message(
                thread_id="thread",
                role=MessageRole.TOOL,
                content='{"echo": "tool output"}',
                tool_name="echo",
                tool_call_id="call_orphaned",
            ),
            Message(thread_id="thread", role=MessageRole.ASSISTANT, content="It is warm today."),
            Message(thread_id="thread", role=MessageRole.USER, content="current home temperature"),
        ]
    )

    assert [message.role for message in pruned] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
    ]


def test_prune_historical_tool_messages_for_azure_keeps_only_active_trailing_tool_pair() -> None:
    pruned = _prune_historical_tool_messages_for_azure(
        [
            Message(thread_id="thread", role=MessageRole.USER, content="weather today"),
            Message(
                thread_id="thread",
                role=MessageRole.ASSISTANT,
                content="",
                tool_name="echo",
                tool_call_id="call_123",
                tool_arguments={"text": "tool input"},
            ),
            Message(thread_id="thread", role=MessageRole.ASSISTANT, content="Intervening note."),
            Message(
                thread_id="thread",
                role=MessageRole.TOOL,
                content='{"echo": "tool output"}',
                tool_name="echo",
                tool_call_id="call_123",
            ),
            Message(
                thread_id="thread",
                role=MessageRole.ASSISTANT,
                content="",
                tool_name="echo",
                tool_call_id="call_456",
                tool_arguments={"text": "latest tool input"},
            ),
            Message(
                thread_id="thread",
                role=MessageRole.TOOL,
                content='{"echo": "latest tool output"}',
                tool_name="echo",
                tool_call_id="call_456",
            ),
        ]
    )

    assert [message.role for message in pruned] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    assert pruned[1].content == "Intervening note."
    assert pruned[2].tool_call_id == "call_456"
    assert pruned[3].tool_call_id == "call_456"


def test_openai_compatible_adapter_does_not_retry_unrelated_provider_errors() -> None:
    seen_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode())
        seen_payloads.append(payload)
        return httpx.Response(
            400,
            json={"error": {"message": "Some other provider validation error"}},
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [
                    Message(thread_id="thread", role=MessageRole.USER, content="weather today"),
                    Message(
                        thread_id="thread",
                        role=MessageRole.ASSISTANT,
                        content="",
                        tool_name="echo",
                        tool_call_id="call_123",
                        tool_arguments={"text": "tool input"},
                    ),
                    Message(
                        thread_id="thread",
                        role=MessageRole.TOOL,
                        content='{"echo": "tool output"}',
                        tool_name="echo",
                        tool_call_id="call_123",
                    ),
                    Message(
                        thread_id="thread",
                        role=MessageRole.ASSISTANT,
                        content="It is warm today.",
                    ),
                    Message(
                        thread_id="thread",
                        role=MessageRole.USER,
                        content="current home temperature",
                    ),
                ],
                build_local_tool_registry().specs(),
            )
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {
        "type": "provider_bad_request",
        "message": "OpenRouter rejected the request.",
        "provider": "openrouter",
    }
    assert len(seen_payloads) == 1


def test_openai_compatible_adapter_normalizes_openrouter_cache_write_tokens() -> None:
    adapter = OpenAICompatibleAdapter(
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 1200,
                        "completion_tokens": 12,
                        "total_tokens": 1212,
                        "prompt_tokens_details": {
                            "cached_tokens": 1024,
                            "cache_write_tokens": 0,
                        },
                    },
                },
            )
        ),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.usage is not None
    assert response.usage["cache_read_tokens"] == 1024
    assert response.usage["cache_write_tokens"] == 0


def test_openai_compatible_adapter_sorts_and_canonicalizes_tools() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    tools = [
        ToolSpec(
            name="z.tool",
            description="Z",
            input_schema={
                "type": "object",
                "properties": {"z": {"type": "string"}, "a": {"type": "string"}},
            },
        ),
        ToolSpec(
            name="a.tool",
            description="A",
            input_schema={"type": "object", "properties": {}},
        ),
    ]

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            tools,
        )
    )

    assert response.content == "ok"
    sent_tools = seen_payload["tools"]
    assert isinstance(sent_tools, list)
    assert sent_tools[0]["function"]["name"] == "a_tool"
    assert sent_tools[1]["function"]["name"] == "z_tool"
    assert list(sent_tools[1]["function"]["parameters"]) == ["properties", "type"]
    assert list(sent_tools[1]["function"]["parameters"]["properties"]) == ["a", "z"]


def test_openai_compatible_adapter_writes_request_hash_debug_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("MINDWEFT_LLM_DEBUG_REQUEST_LOG_PATH", str(request_log))

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
        ),
    )

    asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    record = json.loads(request_log.read_text().splitlines()[-1])
    assert record["adapter"] == "openai-compatible"
    assert record["model"] == "test-model"
    assert record["message_count"] == 1
    assert record["tool_count"] == 0
    assert len(record["raw_sha256"]) == 64
    assert len(record["canonical_sha256"]) == 64
    assert record["payload"]["model"] == "test-model"


def test_openai_compatible_adapter_omits_empty_tools_array() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "ok"
    assert "tools" not in seen_payload


def test_openai_compatible_adapter_sanitizes_provider_tool_names() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_456",
                                    "function": {
                                        "name": "tavily_tavily_search",
                                        "arguments": '{"query":"weather"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    adapter = OpenAICompatibleAdapter(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )

    tools = [
        ToolSpec(
            name="tavily.tavily_search",
            description="Search the web",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )
    ]
    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="weather")],
            tools,
        )
    )

    assert seen_payload["tools"][0]["function"]["name"] == "tavily_tavily_search"
    assert response.tool_call is not None
    assert response.tool_call.name == "tavily.tavily_search"


def test_google_gemini_adapter_retries_malformed_response() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            # First two calls return MALFORMED_RESPONSE
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {},
                            "finishReason": "MALFORMED_RESPONSE",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 100,
                        "totalTokenCount": 100,
                    },
                },
            )
        # Third call returns valid response
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "success after retry"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 10,
                    "totalTokenCount": 110,
                },
            },
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "success after retry"
    assert call_count == 3  # 2 malformed + 1 success


def test_google_gemini_adapter_fails_after_max_malformed_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {},
                        "finishReason": "MALFORMED_RESPONSE",
                        "index": 0,
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "totalTokenCount": 100,
                },
            },
        )

    adapter = GoogleGeminiAdapter(
        base_url="https://example.com/v1beta",
        api_key="test-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    assert exc_info.value.status_code == 502
    assert "malformed response" in exc_info.value.detail.lower()
