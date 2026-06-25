from __future__ import annotations

import asyncio
import os

import pytest

from app.llm import AnthropicMessagesAdapter
from app.models import Message, MessageRole

RUN_ANTHROPIC_INTEGRATION_ENV = "MINIGENT_RUN_ANTHROPIC_INTEGRATION_TESTS"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_MODEL_ENV = "ANTHROPIC_MODEL"

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
