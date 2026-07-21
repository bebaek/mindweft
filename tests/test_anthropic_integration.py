from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app.llm import AnthropicMessagesAdapter
from app.main import create_app
from app.models import Message, MessageRole, ToolSpec
from app.tools import ToolRegistry

RUN_ANTHROPIC_INTEGRATION_ENV = "MINIGENT_RUN_ANTHROPIC_INTEGRATION_TESTS"
RUN_ANTHROPIC_REASONING_ENV = "MINIGENT_RUN_ANTHROPIC_REASONING_TESTS"
RUN_ANTHROPIC_CACHE_ENV = "MINIGENT_RUN_ANTHROPIC_CACHE_TESTS"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_MODEL_ENV = "ANTHROPIC_MODEL"
ANTHROPIC_REASONING_MODEL_ENV = "ANTHROPIC_REASONING_MODEL"
ANTHROPIC_CACHE_MODEL_ENV = "ANTHROPIC_CACHE_MODEL"
AUTH_HEADERS = {
    "X-Minigent-User-Id": "anthropic-integration-user",
    "X-Minigent-Tenant-Id": "anthropic-integration-tenant",
}

pytestmark = pytest.mark.integration


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.mark.skipif(
    not _truthy_env(RUN_ANTHROPIC_INTEGRATION_ENV) or not os.getenv(ANTHROPIC_API_KEY_ENV),
    reason=(
        f"Set {RUN_ANTHROPIC_INTEGRATION_ENV}=true and {ANTHROPIC_API_KEY_ENV} "
        "to run live Anthropic integration tests"
    ),
)
def test_live_anthropic_messages_adapter_returns_text() -> None:
    api_key = os.environ[ANTHROPIC_API_KEY_ENV]
    model = os.getenv(ANTHROPIC_MODEL_ENV, "claude-haiku-4-5")
    adapter = AnthropicMessagesAdapter(
        api_key=api_key,
        model=model,
        max_tokens=32,
        timeout=60.0,
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="anthropic-integration",
                    role=MessageRole.SYSTEM,
                    content="You are a concise integration-test responder.",
                ),
                Message(
                    thread_id="anthropic-integration",
                    role=MessageRole.USER,
                    content="Respond with exactly MINIGENT_ANTHROPIC_OK and no other text.",
                ),
            ],
            [],
        )
    )

    assert response.tool_call is None
    assert response.content is not None
    assert "MINIGENT_ANTHROPIC_OK" in response.content
    assert response.usage is None or response.usage.get("total_tokens", 0) > 0


@pytest.mark.skipif(
    not _truthy_env(RUN_ANTHROPIC_INTEGRATION_ENV) or not os.getenv(ANTHROPIC_API_KEY_ENV),
    reason=(
        f"Set {RUN_ANTHROPIC_INTEGRATION_ENV}=true and {ANTHROPIC_API_KEY_ENV} "
        "to run live Anthropic integration tests"
    ),
)
def test_live_anthropic_messages_adapter_uses_and_replays_tool_result() -> None:
    api_key = os.environ[ANTHROPIC_API_KEY_ENV]
    model = os.getenv(ANTHROPIC_MODEL_ENV, "claude-haiku-4-5")
    adapter = AnthropicMessagesAdapter(
        api_key=api_key,
        model=model,
        max_tokens=128,
        timeout=60.0,
    )
    tool = ToolSpec(
        name="add_numbers",
        description="Add two integers and return their sum.",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "The first integer."},
                "b": {"type": "integer", "description": "The second integer."},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    )
    initial_messages = [
        Message(
            thread_id="anthropic-integration-tool",
            role=MessageRole.SYSTEM,
            content=(
                "You are testing tool use. You must call the add_numbers tool exactly once "
                "before answering arithmetic questions."
            ),
        ),
        Message(
            thread_id="anthropic-integration-tool",
            role=MessageRole.USER,
            content="Use the tool to add 2 and 3.",
        ),
    ]

    tool_response = asyncio.run(adapter.generate(initial_messages, [tool]))

    assert tool_response.tool_call is not None
    assert tool_response.tool_call.name == "add_numbers"
    assert isinstance(tool_response.tool_call.arguments, dict)

    final_response = asyncio.run(
        adapter.generate(
            [
                *initial_messages,
                Message(
                    thread_id="anthropic-integration-tool",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name=tool_response.tool_call.name,
                    tool_call_id=tool_response.tool_call.id,
                    tool_arguments=tool_response.tool_call.arguments,
                ),
                Message(
                    thread_id="anthropic-integration-tool",
                    role=MessageRole.TOOL,
                    content="5",
                    tool_name=tool_response.tool_call.name,
                    tool_call_id=tool_response.tool_call.id,
                ),
            ],
            [tool],
        )
    )

    assert final_response.tool_call is None
    assert final_response.content is not None
    assert "5" in final_response.content


@pytest.mark.skipif(
    not _truthy_env(RUN_ANTHROPIC_INTEGRATION_ENV)
    or not _truthy_env(RUN_ANTHROPIC_REASONING_ENV)
    or not os.getenv(ANTHROPIC_API_KEY_ENV),
    reason=(
        f"Set {RUN_ANTHROPIC_INTEGRATION_ENV}=true, "
        f"{RUN_ANTHROPIC_REASONING_ENV}=true, and {ANTHROPIC_API_KEY_ENV} "
        "to run live Anthropic reasoning integration tests"
    ),
)
def test_live_anthropic_messages_adapter_returns_reasoning_metadata() -> None:
    api_key = os.environ[ANTHROPIC_API_KEY_ENV]
    model = (
        os.getenv(ANTHROPIC_REASONING_MODEL_ENV)
        or os.getenv(ANTHROPIC_MODEL_ENV)
        or "claude-sonnet-4-5"
    )
    adapter = AnthropicMessagesAdapter(
        api_key=api_key,
        model=model,
        max_tokens=2048,
        thinking_budget_tokens=1024,
        timeout=60.0,
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(
                    thread_id="anthropic-integration-reasoning",
                    role=MessageRole.USER,
                    content=(
                        "Solve this carefully and explain your reasoning briefly: find the least "
                        "positive integer n that leaves remainder 1 modulo 2, remainder 2 modulo "
                        "3, remainder 3 modulo 5, and remainder 4 modulo 7."
                    ),
                )
            ],
            [],
        )
    )

    assert response.tool_call is None
    assert response.content is not None
    assert response.metadata is not None
    thinking_blocks = response.metadata.get("anthropic_thinking_blocks")
    assert isinstance(thinking_blocks, list)
    assert thinking_blocks
    assert all(isinstance(block, dict) for block in thinking_blocks)
    assert all(block.get("type") in {"thinking", "redacted_thinking"} for block in thinking_blocks)
    reasoning_content = response.metadata.get("reasoning_content")
    assert reasoning_content is None or isinstance(reasoning_content, str)


@pytest.mark.skipif(
    not _truthy_env(RUN_ANTHROPIC_INTEGRATION_ENV)
    or not _truthy_env(RUN_ANTHROPIC_CACHE_ENV)
    or not os.getenv(ANTHROPIC_API_KEY_ENV),
    reason=(
        f"Set {RUN_ANTHROPIC_INTEGRATION_ENV}=true, "
        f"{RUN_ANTHROPIC_CACHE_ENV}=true, and {ANTHROPIC_API_KEY_ENV} "
        "to run live Anthropic prompt-cache integration tests"
    ),
)
def test_live_anthropic_messages_adapter_uses_prompt_cache() -> None:
    api_key = os.environ[ANTHROPIC_API_KEY_ENV]
    model = (
        os.getenv(ANTHROPIC_CACHE_MODEL_ENV) or os.getenv(ANTHROPIC_MODEL_ENV) or "claude-haiku-4-5"
    )
    adapter = AnthropicMessagesAdapter(
        api_key=api_key,
        model=model,
        max_tokens=16,
        timeout=60.0,
    )
    stable_prompt = "\n".join(
        [
            "You are a prompt-cache integration test fixture.",
            *(
                f"Stable cache line {index}: The deterministic value is 42."
                for index in range(1, 500)
            ),
        ]
    )
    messages = [
        Message(
            thread_id="anthropic-integration-cache",
            role=MessageRole.SYSTEM,
            content=stable_prompt,
        ),
        Message(
            thread_id="anthropic-integration-cache",
            role=MessageRole.USER,
            content="Reply with only: cache ok",
        ),
    ]

    first_response = asyncio.run(adapter.generate(messages, []))
    second_response = asyncio.run(adapter.generate(messages, []))

    assert first_response.content is not None
    assert second_response.content is not None
    assert first_response.usage is not None
    assert second_response.usage is not None
    first_cache_activity = max(
        first_response.usage.get("cache_read_tokens", 0),
        first_response.usage.get("cache_write_tokens", 0),
    )
    assert first_cache_activity > 0
    assert second_response.usage.get("cache_read_tokens", 0) > 0


@pytest.mark.skipif(
    not _truthy_env(RUN_ANTHROPIC_INTEGRATION_ENV) or not os.getenv(ANTHROPIC_API_KEY_ENV),
    reason=(
        f"Set {RUN_ANTHROPIC_INTEGRATION_ENV}=true and {ANTHROPIC_API_KEY_ENV} "
        "to run live Anthropic integration tests"
    ),
)
def test_live_anthropic_runtime_api_smoke() -> None:
    api_key = os.environ[ANTHROPIC_API_KEY_ENV]
    model = os.getenv(ANTHROPIC_MODEL_ENV, "claude-haiku-4-5")
    adapter = AnthropicMessagesAdapter(
        api_key=api_key,
        model=model,
        max_tokens=48,
        timeout=60.0,
    )
    client = TestClient(create_app(llm_adapter=adapter, tool_registry=ToolRegistry()))

    create_response = client.post("/threads", headers=AUTH_HEADERS)
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]

    add_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "What is 2 + 2? Answer with the numeral only."},
        headers=AUTH_HEADERS,
    )
    assert add_response.status_code == 200

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert "4" in run_response.json()["reply"]

    messages_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert [message["role"] for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert "4" in messages[1]["content"]
