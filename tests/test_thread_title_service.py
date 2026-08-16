from __future__ import annotations

import asyncio

from app.execution import TenantExecutionConfig, TenantExecutionContext
from app.llm import LLMAdapter
from app.models import LLMResponse, Message, MessageRole, ToolSpec
from app.store import InMemoryThreadStore
from app.thread_title_service import generate_semantic_thread_title, normalize_semantic_title
from app.tools import ToolRegistry


class TitleLLMAdapter(LLMAdapter):
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.requests: list[list[Message]] = []

    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        assert tools == []
        self.requests.append(messages)
        return LLMResponse(content=self.responses.pop(0))

    def describe(self) -> dict[str, object]:
        return {"provider": "test", "model": "title-test"}


def execution(adapter: LLMAdapter) -> TenantExecutionContext:
    return TenantExecutionContext(
        llm_adapter=adapter,
        tool_registry=ToolRegistry(),
        config=TenantExecutionConfig(tenant_id="tenant-a"),
    )


def test_semantic_title_waits_for_concrete_context_then_persists() -> None:
    asyncio.run(_semantic_title_waits_for_concrete_context_then_persists())


async def _semantic_title_waits_for_concrete_context_then_persists() -> None:
    store = InMemoryThreadStore()
    thread = store.create_thread("tenant-a")
    store.append_message(
        "tenant-a", Message(thread_id=thread.thread_id, role=MessageRole.USER, content="hey")
    )
    store.append_message(
        "tenant-a",
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.ASSISTANT,
            content="Hey! What can I help you with?",
        ),
    )
    adapter = TitleLLMAdapter(["INSUFFICIENT_CONTEXT", "Austin weather today"])

    first = await generate_semantic_thread_title(
        store=store,
        execution=execution(adapter),
        tenant_id="tenant-a",
        thread_id=thread.thread_id,
    )
    assert first.status == "skipped"
    assert first.reason == "insufficient_context"
    assert store.get_thread("tenant-a", thread.thread_id).title == "Hey"

    store.append_message(
        "tenant-a",
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="what's the weather in Austin today?",
        ),
    )
    store.append_message(
        "tenant-a",
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.ASSISTANT,
            content="It will be hot and sunny.",
        ),
    )
    second = await generate_semantic_thread_title(
        store=store,
        execution=execution(adapter),
        tenant_id="tenant-a",
        thread_id=thread.thread_id,
    )

    assert second.status == "updated"
    assert second.title == "Austin weather today"
    updated = store.get_thread("tenant-a", thread.thread_id)
    assert updated.title == "Austin weather today"
    assert updated.title_source == "semantic"
    transcript = adapter.requests[-1][-1].content
    assert "user: hey" in transcript
    assert "user: what's the weather in Austin today?" in transcript


def test_semantic_title_never_overwrites_manual_title() -> None:
    asyncio.run(_semantic_title_never_overwrites_manual_title())


async def _semantic_title_never_overwrites_manual_title() -> None:
    store = InMemoryThreadStore()
    thread = store.create_thread("tenant-a")
    store.append_message(
        "tenant-a",
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="weather in Austin"),
    )
    store.set_thread_title("tenant-a", thread.thread_id, title="My weather", source="manual")
    adapter = TitleLLMAdapter(["Austin weather today"])

    result = await generate_semantic_thread_title(
        store=store,
        execution=execution(adapter),
        tenant_id="tenant-a",
        thread_id=thread.thread_id,
    )

    assert result.status == "skipped"
    assert result.reason == "manual_title"
    assert adapter.requests == []
    assert store.get_thread("tenant-a", thread.thread_id).title == "My weather"


def test_normalize_semantic_title_rejects_sentinel_and_unbounded_output() -> None:
    assert normalize_semantic_title("INSUFFICIENT_CONTEXT") is None
    assert normalize_semantic_title('Title: "Austin weather today."') == "Austin weather today"
    assert normalize_semantic_title("one") is None
    assert normalize_semantic_title("word " * 20) is None
