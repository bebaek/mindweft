import asyncio
import json
from datetime import datetime

from fastapi import HTTPException

from app.execution import (
    InMemoryTenantExecutionResolver,
    TenantExecutionContext,
    build_tool_registry_for_capability_profile,
    build_tool_registry_for_skill,
    parse_tenant_execution_config,
)
from app.llm import LLMAdapter, MockLLMAdapter
from app.mcp import MCPServerConfig, MCPServerInfo
from app.models import (
    LLMResponse,
    Message,
    MessageRole,
    Principal,
    ThreadStatus,
    ToolCall,
    ToolSpec,
)
from app.runtime import RUNTIME_SYSTEM_PROMPT, AgentRuntime
from app.store import InMemoryThreadStore
from app.tools import build_local_tool_registry

PRINCIPAL = Principal(user_id="user-1", tenant_id="tenant-1")
OTHER_PRINCIPAL = Principal(user_id="user-2", tenant_id="tenant-2")


def test_runtime_returns_assistant_reply_for_plain_user_message() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry()
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="hello",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "Mock reply: hello"
    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Mock reply: hello"
    assert store._threads[thread.thread_id].status == ThreadStatus.IDLE


def test_runtime_sends_system_prompt_to_llm() -> None:
    seen_messages: list[Message] = []

    class InspectingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            nonlocal seen_messages
            seen_messages = messages
            return LLMResponse(content="ok")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store, llm_adapter=InspectingLLM(), tool_registry=build_local_tool_registry()
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="hello",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "ok"
    assert seen_messages[0].role == MessageRole.SYSTEM
    assert seen_messages[0].content == RUNTIME_SYSTEM_PROMPT
    assert seen_messages[1].role == MessageRole.USER
    assert seen_messages[1].content == "hello"


def test_runtime_summarizes_older_messages_and_keeps_recent_tail_verbatim() -> None:
    seen_messages: list[Message] = []

    class InspectingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            nonlocal seen_messages
            seen_messages = messages
            return LLMResponse(content="ok")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=InspectingLLM(),
        tool_registry=build_local_tool_registry(),
        recent_message_limit=4,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    for index in range(6):
        store.append_message(
            PRINCIPAL.tenant_id,
            Message(
                thread_id=thread.thread_id,
                role=MessageRole.USER,
                content=f"user-{index}",
                created_by=PRINCIPAL.user_id,
            ),
        )
        store.append_message(
            PRINCIPAL.tenant_id,
            Message(
                thread_id=thread.thread_id,
                role=MessageRole.ASSISTANT,
                content=f"assistant-{index}",
            ),
        )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "ok"
    assert seen_messages[0].role == MessageRole.SYSTEM
    assert seen_messages[1].role == MessageRole.SYSTEM
    assert seen_messages[1].content.startswith("Thread summary:\nUser: user-0\nAssistant: assistant-0")
    assert [message.content for message in seen_messages[2:]] == [
        "user-4",
        "assistant-4",
        "user-5",
        "assistant-5",
    ]
    context = store.get_thread_context(PRINCIPAL.tenant_id, thread.thread_id)
    assert context.summarized_message_count == 0
    assert "user-3" in context.summary
    assert "assistant-3" in context.summary
    assert [message.content for message in store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)] == [
        "user-4",
        "assistant-4",
        "user-5",
        "assistant-5",
        "ok",
    ]


def test_runtime_uses_prompt_budget_to_compact_more_than_default_tail() -> None:
    seen_messages: list[Message] = []

    class InspectingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            nonlocal seen_messages
            seen_messages = messages
            return LLMResponse(content="ok")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=InspectingLLM(),
        tool_registry=build_local_tool_registry(),
        recent_message_limit=8,
        min_recent_message_limit=2,
        target_prompt_tokens=80,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    long_text = "x" * 160
    for index in range(5):
        store.append_message(
            PRINCIPAL.tenant_id,
            Message(
                thread_id=thread.thread_id,
                role=MessageRole.USER,
                content=f"user-{index} {long_text}",
                created_by=PRINCIPAL.user_id,
            ),
        )
        store.append_message(
            PRINCIPAL.tenant_id,
            Message(
                thread_id=thread.thread_id,
                role=MessageRole.ASSISTANT,
                content=f"assistant-{index} {long_text}",
            ),
        )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "ok"
    assert seen_messages[1].role == MessageRole.SYSTEM
    tail_contents = [message.content for message in seen_messages[2:]]
    assert len(tail_contents) < 8
    assert tail_contents[-2:] == [
        f"user-4 {long_text}",
        f"assistant-4 {long_text}",
    ]
    context = store.get_thread_context(PRINCIPAL.tenant_id, thread.thread_id)
    assert context.summarized_message_count == 0
    assert "user-3" in context.summary
    assert "user-0" in context.summary


def test_runtime_drops_summarized_messages_from_store_after_multiple_turns() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(),
        recent_message_limit=4,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)

    for index in range(6):
        store.append_message(
            PRINCIPAL.tenant_id,
            Message(
                thread_id=thread.thread_id,
                role=MessageRole.USER,
                content=f"user-{index}",
                created_by=PRINCIPAL.user_id,
            ),
        )
        asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    stored_messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert len(stored_messages) <= 5
    assert stored_messages[-1].content == "Mock reply: user-5"

    context = store.get_thread_context(PRINCIPAL.tenant_id, thread.thread_id)
    assert context.summarized_message_count == 0
    assert "user-0" in context.summary
    assert "Mock reply: user-2" in context.summary


def test_runtime_executes_tool_and_stores_tool_message() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry()
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="/tool echo hello from tool",
            created_by=PRINCIPAL.user_id,
        )
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
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
    runtime = AgentRuntime(
        store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry()
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="/tool current_time",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
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

    class ToolThenReplyLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            if messages and messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content=f"Tool result: {messages[-1].content}")
            return LLMResponse(
                tool_call=ToolCall(
                    id="call-fetch",
                    name="fetch_url",
                    arguments={"url": "https://example.com/missing"},
                )
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=ToolThenReplyLLM(),
        tool_registry=FailingRegistry(),  # type: ignore[arg-type]
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="is airport open",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
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

    class RepeatFailingToolThenReplyLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            if self.calls < 2:
                self.calls += 1
                return LLMResponse(
                    tool_call=ToolCall(
                        id=f"call-search-{self.calls}",
                        name="tavily.tavily_search",
                        arguments={"query": "aus open"},
                    )
                )
            return LLMResponse(content=f"Tool result: {messages[-1].content}")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    registry = CountingFailingRegistry()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=RepeatFailingToolThenReplyLLM(),
        tool_registry=registry,  # type: ignore[arg-type]
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="is airport open",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
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

    class RepeatErrorResultToolThenReplyLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            if self.calls < 2:
                self.calls += 1
                return LLMResponse(
                    tool_call=ToolCall(
                        id=f"call-search-{self.calls}",
                        name="tavily.tavily_search",
                        arguments={"query": "aus open"},
                    )
                )
            return LLMResponse(content=f"Tool result: {messages[-1].content}")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    registry = CountingErrorResultRegistry()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=RepeatErrorResultToolThenReplyLLM(),
        tool_registry=registry,  # type: ignore[arg-type]
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="is airport open",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
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
    class LoopingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            return LLMResponse(
                tool_call=ToolCall(id="loop-call", name="echo", arguments={"text": "loop"})
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=LoopingLLM(),
        tool_registry=build_local_tool_registry(),
        max_iterations=2,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="loop",
            created_by=PRINCIPAL.user_id,
        ),
    )

    try:
        asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))
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

        class BlockingLLM(LLMAdapter):
            async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
                started.set()
                await release.wait()
                return LLMResponse(content="done")

            def describe(self) -> dict[str, object]:
                return {"provider": "test"}

        store = InMemoryThreadStore()
        runtime = AgentRuntime(
            store=store,
            llm_adapter=BlockingLLM(),
            tool_registry=build_local_tool_registry(),
        )
        thread = store.create_thread(PRINCIPAL.tenant_id)
        store.append_message(
            PRINCIPAL.tenant_id,
            Message(
                thread_id=thread.thread_id,
                role=MessageRole.USER,
                content="hello",
                created_by=PRINCIPAL.user_id,
            ),
        )

        first_run = asyncio.create_task(runtime.run_thread(PRINCIPAL, thread.thread_id))
        await started.wait()

        with_raise: HTTPException | None = None
        try:
            await runtime.run_thread(PRINCIPAL, thread.thread_id)
        except HTTPException as exc:
            with_raise = exc

        release.set()
        assert await first_run == "done"
        assert with_raise is not None
        assert with_raise.status_code == 409
        assert store._threads[thread.thread_id].status == ThreadStatus.IDLE

    asyncio.run(exercise())


def test_runtime_hides_cross_tenant_thread_access() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry()
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="hello",
            created_by=PRINCIPAL.user_id,
        ),
    )

    try:
        asyncio.run(runtime.run_thread(OTHER_PRINCIPAL, thread.thread_id))
    except HTTPException as exc:
        assert exc.status_code == 404
        assert exc.detail == f"Thread '{thread.thread_id}' not found"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected HTTPException")


def test_runtime_appends_skill_prompt_to_system_prompt() -> None:
    seen_messages: list[Message] = []
    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["echo"]},
            "skills": {
                "items": [
                    {
                        "name": "support",
                        "system_prompt": "Answer as a concise support agent.",
                        "allowed_local_tools": ["echo"],
                    }
                ]
            },
        },
    )

    class InspectingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            nonlocal seen_messages
            seen_messages = messages
            return LLMResponse(content="ok")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    resolver = InMemoryTenantExecutionResolver(
        {PRINCIPAL.tenant_id: config},
        default_context=None,
    )
    resolver._contexts[PRINCIPAL.tenant_id] = TenantExecutionContext(
        llm_adapter=InspectingLLM(),
        tool_registry=build_local_tool_registry(allowed_tools=["echo"]),
        config=config,
    )
    runtime = AgentRuntime(store=store, execution_resolver=resolver)
    thread = store.create_thread(PRINCIPAL.tenant_id, skill_name="support")
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="hello",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "ok"
    assert seen_messages[0].content == (
        f"{RUNTIME_SYSTEM_PROMPT}\n\n[Skill: support]\nAnswer as a concise support agent."
    )


def test_runtime_appends_multiple_skill_prompts_in_order() -> None:
    seen_messages: list[Message] = []
    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "skills": {
                "items": [
                    {
                        "name": "support",
                        "system_prompt": "Answer as a concise support agent.",
                    },
                    {
                        "name": "safe-actions",
                        "system_prompt": "Require confirmation before risky actions.",
                    },
                ]
            },
        },
    )

    class InspectingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            nonlocal seen_messages
            seen_messages = messages
            return LLMResponse(content="ok")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    resolver = InMemoryTenantExecutionResolver(
        {PRINCIPAL.tenant_id: config},
        default_context=None,
    )
    resolver._contexts[PRINCIPAL.tenant_id] = TenantExecutionContext(
        llm_adapter=InspectingLLM(),
        tool_registry=build_local_tool_registry(),
        config=config,
    )
    runtime = AgentRuntime(store=store, execution_resolver=resolver)
    thread = store.create_thread(
        PRINCIPAL.tenant_id,
        skill_names=["support", "safe-actions"],
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="hello",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "ok"
    assert seen_messages[0].content == (
        f"{RUNTIME_SYSTEM_PROMPT}\n\n"
        "[Skill: support]\nAnswer as a concise support agent.\n\n"
        "[Skill: safe-actions]\nRequire confirmation before risky actions."
    )


def test_runtime_skill_can_narrow_tools_for_thread() -> None:
    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["echo", "calculator"]},
            "skills": {
                "items": [
                    {
                        "name": "math",
                        "system_prompt": "Prefer exact arithmetic.",
                        "allowed_local_tools": ["calculator"],
                    }
                ]
            },
        },
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        execution_resolver=InMemoryTenantExecutionResolver({PRINCIPAL.tenant_id: config}),
    )
    thread = store.create_thread(PRINCIPAL.tenant_id, skill_name="math")
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="/tool echo blocked by skill",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "Mock reply: /tool echo blocked by skill"


def test_build_tool_registry_for_skill_can_narrow_mcp_servers(monkeypatch) -> None:
    class FakeMCPClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self._config = config

        async def list_tools(self) -> list[ToolSpec]:
            return [
                ToolSpec(
                    name=f"{self._config.name}.ping",
                    description=f"Ping {self._config.name}",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            return {"server": self._config.name, "tool_name": tool_name, "arguments": arguments}

        def server_info(self) -> MCPServerInfo:
            return MCPServerInfo(
                name=self._config.name,
                url=self._config.url,
                protocol_version=self._config.protocol_version,
                session_id="session-123",
                server_name=f"{self._config.name}-server",
                server_version="1.0.0",
            )

    monkeypatch.setattr("app.tools.MCPHTTPClient", FakeMCPClient)

    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "tools": {
                "allowed_local_tools": ["current_time"],
                "mcp_servers": [
                    {"name": "home-assistant", "url": "https://ha.example/mcp", "headers": {}},
                    {"name": "docs", "url": "https://docs.example/mcp", "headers": {}},
                ]
            },
            "skills": {
                "items": [
                    {
                        "name": "home-assistant",
                        "system_prompt": "Use Home Assistant safely.",
                        "allowed_local_tools": ["current_time"],
                        "mcp_server_names": ["home-assistant"],
                    }
                ]
            },
        },
    )

    registry = build_tool_registry_for_skill(config, "home-assistant")

    assert {spec.name for spec in registry.specs()} == {"current_time", "home-assistant.ping"}
    assert [server["name"] for server in registry.mcp_servers()] == ["home-assistant"]


def test_runtime_capability_profile_can_narrow_tools_for_thread() -> None:
    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["echo", "calculator"]},
            "capability_profiles": {
                "items": [
                    {
                        "name": "math",
                        "allowed_local_tools": ["calculator"],
                    }
                ]
            },
        },
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        execution_resolver=InMemoryTenantExecutionResolver({PRINCIPAL.tenant_id: config}),
    )
    thread = store.create_thread(PRINCIPAL.tenant_id, capability_profile="math")
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="/tool echo blocked by capability profile",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "Mock reply: /tool echo blocked by capability profile"


def test_build_tool_registry_for_capability_profile_can_narrow_mcp_servers(monkeypatch) -> None:
    class FakeMCPClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self._config = config

        async def list_tools(self) -> list[ToolSpec]:
            return [
                ToolSpec(
                    name=f"{self._config.name}.ping",
                    description=f"Ping {self._config.name}",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            return {"server": self._config.name, "tool_name": tool_name, "arguments": arguments}

        def server_info(self) -> MCPServerInfo:
            return MCPServerInfo(
                name=self._config.name,
                url=self._config.url,
                protocol_version=self._config.protocol_version,
                session_id="session-123",
                server_name=f"{self._config.name}-server",
                server_version="1.0.0",
            )

    monkeypatch.setattr("app.tools.MCPHTTPClient", FakeMCPClient)

    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "tools": {
                "allowed_local_tools": ["current_time"],
                "mcp_servers": [
                    {"name": "home-assistant", "url": "https://ha.example/mcp", "headers": {}},
                    {"name": "docs", "url": "https://docs.example/mcp", "headers": {}},
                ]
            },
            "capability_profiles": {
                "items": [
                    {
                        "name": "home-assistant",
                        "allowed_local_tools": ["current_time"],
                        "mcp_server_names": ["home-assistant"],
                    }
                ]
            },
        },
    )

    registry = build_tool_registry_for_capability_profile(config, "home-assistant")

    assert {spec.name for spec in registry.specs()} == {"current_time", "home-assistant.ping"}
    assert [server["name"] for server in registry.mcp_servers()] == ["home-assistant"]
