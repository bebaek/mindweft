import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from app.llm import (
    MockLLMAdapter,
    OpenAICompatibleAdapter,
    build_llm_adapter_from_env,
    load_provider_config,
)
from app.models import Message, MessageRole, ToolSpec
from app.tools import build_local_tool_registry


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
            build_local_tool_registry().specs(),
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
            build_local_tool_registry().specs(),
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


def test_build_llm_adapter_from_env_supports_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    adapter = build_llm_adapter_from_env()

    assert isinstance(adapter, OpenAICompatibleAdapter)


def test_build_llm_adapter_from_env_rejects_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", "openrouter")
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
                Message(thread_id="thread", role=MessageRole.ASSISTANT, content="It is warm today."),
                Message(thread_id="thread", role=MessageRole.USER, content="current home temperature"),
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
                Message(thread_id="thread", role=MessageRole.ASSISTANT, content="It is warm today."),
                Message(thread_id="thread", role=MessageRole.USER, content="current home temperature"),
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
                Message(thread_id="thread", role=MessageRole.ASSISTANT, content="It is warm today."),
                Message(thread_id="thread", role=MessageRole.USER, content="current home temperature"),
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

    with pytest.raises(HTTPException, match="Some other provider validation error"):
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

    assert len(seen_payloads) == 1


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
