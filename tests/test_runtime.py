from fastapi import HTTPException

from app.llm import MockLLMAdapter
from app.models import LLMResponse, Message, MessageRole, ThreadStatus, ToolCall
from app.runtime import AgentRuntime
from app.store import InMemoryThreadStore
from app.tools import build_default_tool_registry


def test_runtime_returns_assistant_reply_for_plain_user_message() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_default_tool_registry())
    thread = store.create_thread()
    store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="hello"))

    reply = runtime.run_thread(thread.thread_id)

    assert reply == "Mock reply: hello"
    messages = store.list_messages(thread.thread_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Mock reply: hello"
    assert store._threads[thread.thread_id].status == ThreadStatus.IDLE


def test_runtime_executes_tool_and_stores_tool_message() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(store=store, llm_adapter=MockLLMAdapter(), tool_registry=build_default_tool_registry())
    thread = store.create_thread()
    store.append_message(
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="/tool echo hello from tool")
    )

    reply = runtime.run_thread(thread.thread_id)

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


def test_runtime_marks_thread_error_when_max_iterations_exceeded() -> None:
    class LoopingLLM:
        def generate(self, messages: list[Message], tools: list[object]) -> LLMResponse:
            return LLMResponse(tool_call=ToolCall(id="loop-call", name="echo", arguments={"text": "loop"}))

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=LoopingLLM(),
        tool_registry=build_default_tool_registry(),
        max_iterations=2,
    )
    thread = store.create_thread()
    store.append_message(Message(thread_id=thread.thread_id, role=MessageRole.USER, content="loop"))

    try:
        runtime.run_thread(thread.thread_id)
    except HTTPException as exc:
        assert exc.status_code == 500
        assert exc.detail == "Agent exceeded maximum tool iterations"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected HTTPException")

    assert store._threads[thread.thread_id].status == ThreadStatus.ERROR
