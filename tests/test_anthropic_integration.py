from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from app.llm import AnthropicMessagesAdapter
from app.main import create_app
from app.models import Message, MessageRole, ToolSpec
from app.tools import build_local_tool_registry

RUN_ANTHROPIC_INTEGRATION_ENV = "MINIGENT_RUN_ANTHROPIC_INTEGRATION_TESTS"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_MODEL_ENV = "ANTHROPIC_MODEL"
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
    client = TestClient(create_app(llm_adapter=adapter, tool_registry=build_local_tool_registry()))

    create_response = client.post("/threads", headers=AUTH_HEADERS)
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]

    add_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "Respond with exactly MINIGENT_ANTHROPIC_RUNTIME_OK."},
        headers=AUTH_HEADERS,
    )
    assert add_response.status_code == 200

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200
    assert "MINIGENT_ANTHROPIC_RUNTIME_OK" in run_response.json()["reply"]

    messages_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert [message["role"] for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert "MINIGENT_ANTHROPIC_RUNTIME_OK" in messages[1]["content"]
