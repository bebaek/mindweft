import asyncio
import json
from datetime import datetime

from fastapi import HTTPException

from app.llm import MockLLMAdapter
from app.models import LLMResponse, Message, MessageRole, ThreadStatus, ToolCall
from app.runtime import AgentRuntime
from app.store import InMemoryThreadStore
from app.tools import build_local_tool_registry


def test_runtime_returns_assistant_reply_for_plain_user_message() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    thread = store.create_thread()
    store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="hello"))

    reply = asyncio.run(runtime.run_thread(thread.thread_id))

    assert reply == "Mock reply: hello"
    messages = store.list_messages(thread.thread_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Mock reply: hello"
    assert store._threads[thread.thread_id].status == ThreadStatus.IDLE


def test_runtime_executes_tool_and_stores_tool_message() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    thread = store.create_thread()
    store.append_message(
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="/tool echo hello from tool")
    )

    reply = asyncio.run(runtime.run_thread(thread.thread_id))

    messages = store.list_messages(thread.thread_id)
    assert reply == 'Tool result: {"echo": "hello from tool"}'
    assert [message.role for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert messages[1].tool_name == "echo"
    assert messages[1].tool_call_id == "mock-echo-call"
    assert messages[1].tool_arguments == {"text": "hello from tool"}
    assert messages[2].tool_name == "echo"
    assert messages[2].tool_call_id == "mock-echo-call"
    assert messages[2].content == '{"echo": "hello from tool"}'


def test_runtime_executes_current_time_tool() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    thread = store.create_thread()
    store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="/tool current_time"))

    reply = asyncio.run(runtime.run_thread(thread.thread_id))

    messages = store.list_messages(thread.thread_id)
    assert reply.startswith('Tool result: {"current_time": "')
    assert [message.role for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert messages[1].tool_name == "current_time"
    assert messages[1].tool_call_id == "mock-current_time-call"
    payload = json.loads(messages[2].content)
    datetime.fromisoformat(payload["current_time"])


def test_runtime_stores_tool_error_and_continues() -> None:
    class FailingRegistry:
        def specs(self) -> list[object]:
            return []

        async def execute(self, name: str, arguments: dict[str, object]) -> object:
            raise HTTPException(status_code=502, detail="fetch_url failed with status 404")

    class ToolThenReplyLLM:
        async def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
            if messages and messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content=f"Tool result: {messages[-1].content}")
            return LLMResponse(
                tool_call=ToolCall(id="call-fetch", name="fetch_url", arguments={"url": "https://example.com/missing"})
            )

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=ToolThenReplyLLM(),
        tool_registry=FailingRegistry(),  # type: ignore[arg-type]
    )
    thread = store.create_thread()
    store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="is airport open"))

    reply = asyncio.run(runtime.run_thread(thread.thread_id))

    messages = store.list_messages(thread.thread_id)
    assert reply == (
        'Tool result: {"error": {"tool_name": "fetch_url", "status_code": 502, '
        '"detail": "fetch_url failed with status 404"}}'
    )
    assert messages[2].role == MessageRole.TOOL
    assert messages[2].tool_name == "fetch_url"
    assert json.loads(messages[2].content) == {
        "error": {
            "tool_name": "fetch_url",
            "status_code": 502,
            "detail": "fetch_url failed with status 404",
        }
    }
    assert store._threads[thread.thread_id].status == ThreadStatus.IDLE


def test_runtime_blocks_repeated_identical_failed_tool_calls() -> None:
    class CountingFailingRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def specs(self) -> list[object]:
            return []

        async def execute(self, name: str, arguments: dict[str, object]) -> object:
            self.calls += 1
            raise HTTPException(status_code=429, detail="Search failed")

    class RepeatFailingToolThenReplyLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
            if self.calls < 2:
                self.calls += 1
                return LLMResponse(
                    tool_call=ToolCall(id=f"call-search-{self.calls}", name="tavily.tavily_search", arguments={"query": "aus open"})
                )
            return LLMResponse(content=f"Tool result: {messages[-1].content}")

    store = InMemoryThreadStore()
    registry = CountingFailingRegistry()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=RepeatFailingToolThenReplyLLM(),
        tool_registry=registry,  # type: ignore[arg-type]
    )
    thread = store.create_thread()
    store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="is airport open"))

    reply = asyncio.run(runtime.run_thread(thread.thread_id))

    messages = store.list_messages(thread.thread_id)
    assert registry.calls == 1
    assert reply == (
        'Tool result: {"error": {"tool_name": "tavily.tavily_search", "status_code": 409, '
        '"detail": "Repeated failed tool call blocked for identical arguments", "blocked": true}}'
    )
    assert json.loads(messages[4].content) == {
        "error": {
            "tool_name": "tavily.tavily_search",
            "status_code": 409,
            "detail": "Repeated failed tool call blocked for identical arguments",
            "blocked": True,
        }
    }


def test_runtime_blocks_repeated_identical_error_results() -> None:
    class CountingErrorResultRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def specs(self) -> list[object]:
            return []

        async def execute(self, name: str, arguments: dict[str, object]) -> object:
            self.calls += 1
            return {
                "error": "Search failed",
                "status": 429,
                "detail": {"error": "Rate limited"},
                "documentation": "https://docs.tavily.com/documentation/api-reference/endpoint/search",
            }

    class RepeatErrorResultToolThenReplyLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
            if self.calls < 2:
                self.calls += 1
                return LLMResponse(
                    tool_call=ToolCall(id=f"call-search-{self.calls}", name="tavily.tavily_search", arguments={"query": "aus open"})
                )
            return LLMResponse(content=f"Tool result: {messages[-1].content}")

    store = InMemoryThreadStore()
    registry = CountingErrorResultRegistry()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=RepeatErrorResultToolThenReplyLLM(),
        tool_registry=registry,  # type: ignore[arg-type]
    )
    thread = store.create_thread()
    store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="is airport open"))

    reply = asyncio.run(runtime.run_thread(thread.thread_id))

    messages = store.list_messages(thread.thread_id)
    assert registry.calls == 1
    assert reply == (
        'Tool result: {"error": {"tool_name": "tavily.tavily_search", "status_code": 409, '
        '"detail": "Repeated failed tool call blocked for identical arguments", "blocked": true}}'
    )
    assert json.loads(messages[2].content) == {
        "error": {
            "tool_name": "tavily.tavily_search",
            "status_code": 429,
            "detail": {"error": "Rate limited"},
            "message": "Search failed",
            "documentation": "https://docs.tavily.com/documentation/api-reference/endpoint/search",
        }
    }
    assert json.loads(messages[4].content) == {
        "error": {
            "tool_name": "tavily.tavily_search",
            "status_code": 409,
            "detail": "Repeated failed tool call blocked for identical arguments",
            "blocked": True,
        }
    }


def test_runtime_marks_thread_error_when_max_iterations_exceeded() -> None:
    class LoopingLLM:
        async def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
            return LLMResponse(tool_call=ToolCall(id="loop-call", name="echo", arguments={"text": "loop"}))

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=LoopingLLM(),
        tool_registry=build_local_tool_registry(),
        max_iterations=2,
    )
    thread = store.create_thread()
    store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="loop"))

    try:
        asyncio.run(runtime.run_thread(thread.thread_id))
    except HTTPException as exc:
        assert exc.status_code == 500
        assert exc.detail == "Agent exceeded maximum tool iterations"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected HTTPException")

    assert store._threads[thread.thread_id].status == ThreadStatus.ERROR


def test_runtime_rejects_concurrent_runs_for_same_thread() -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingLLM:
            async def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
                started.set()
                await release.wait()
                return LLMResponse(content="done")

        store = InMemoryThreadStore()
        runtime = AgentRuntime(
            store=store,
            llm_adapter=BlockingLLM(),
            tool_registry=build_local_tool_registry(),
        )
        thread = store.create_thread()
        store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="hello"))

        first_run = asyncio.create_task(runtime.run_thread(thread.thread_id))
        await started.wait()

        with_raise: HTTPException | None = None
        try:
            await runtime.run_thread(thread.thread_id)
        except HTTPException as exc:
            with_raise = exc

        release.set()
        assert await first_run == "done"
        assert with_raise is not None
        assert with_raise.status_code == 409
        assert store._threads[thread.thread_id].status == ThreadStatus.IDLE

    asyncio.run(exercise())
