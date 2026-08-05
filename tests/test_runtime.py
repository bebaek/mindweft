import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.admin_store import SQLiteTenantConfigStore
from app.execution import (
    DEFAULT_TENANT_KEY,
    FixedTenantExecutionResolver,
    InMemoryTenantExecutionResolver,
    StoreBackedTenantExecutionResolver,
    TenantExecutionConfig,
    TenantExecutionContext,
    TenantQualityConfig,
    build_tool_registry_for_capability_profile,
    build_tool_registry_for_constraints,
    build_tool_registry_for_skill,
    parse_tenant_execution_config,
)
from app.llm import LLMAdapter, MockLLMAdapter
from app.mcp import MCPPrivateToolResult, MCPPrivateValuePolicy, MCPServerConfig, MCPServerInfo
from app.models import (
    LLMResponse,
    Message,
    MessageRole,
    Principal,
    ThreadStatus,
    ToolCall,
    ToolSpec,
)
from app.peer_agents import PeerAgentConfig, PeerAgentRegistry
from app.private_consents import InMemoryPrivateValueConsentStore, PrivateValueDisclosure
from app.private_values import PII_PLACEHOLDER_PATTERN, InMemoryPrivateValueStore
from app.quality import QualityEnhancer
from app.runtime import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    RUNTIME_SYSTEM_PROMPT,
    AgentRuntime,
    RuntimeSettings,
    max_iterations_from_env,
)
from app.store import InMemoryThreadStore
from app.tools import ToolExecutionContext, ToolRegistry, build_local_tool_registry

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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "Mock reply: hello"
    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Mock reply: hello"
    assert store._threads[thread.thread_id].status == ThreadStatus.IDLE


def test_runtime_executes_multiple_tool_calls_in_one_iteration() -> None:
    class MultiToolThenReplyLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            if messages[-1].role == MessageRole.TOOL:
                tool_results = [
                    message.content for message in messages if message.role == MessageRole.TOOL
                ]
                return LLMResponse(content=" | ".join(tool_results))
            return LLMResponse(
                tool_calls=[
                    ToolCall(id="call-a", name="echo", arguments={"text": "alpha"}),
                    ToolCall(id="call-b", name="echo", arguments={"text": "beta"}),
                ]
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=MultiToolThenReplyLLM(),
        tool_registry=build_local_tool_registry(allowed_tools=["echo"]),
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="use two tools"),
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert "alpha" in reply
    assert "beta" in reply
    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert [message.role for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert [message.tool_call_id for message in messages[1:5]] == [
        "call-a",
        "call-a",
        "call-b",
        "call-b",
    ]


def test_runtime_uses_redacted_tool_results_for_stream_store_and_llm_context() -> None:
    seen_tool_content: str | None = None

    class SecretToolThenReplyLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            nonlocal seen_tool_content
            if messages[-1].role == MessageRole.TOOL:
                seen_tool_content = messages[-1].content
                return LLMResponse(content=f"Tool result: {messages[-1].content}")
            return LLMResponse(
                tool_call=ToolCall(id="call-secret", name="secret_tool", arguments={})
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    registry = ToolRegistry()
    registry.register(
        name="secret_tool",
        description="Return a result containing sensitive values.",
        input_schema={"type": "object", "properties": {}},
        handler=lambda arguments, context=None: {
            "api_key": "sk-test-secret",
            "public": "visible",
        },
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=SecretToolThenReplyLLM(),
        tool_registry=registry,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="use secret tool"),
    )
    events: list[dict[str, object]] = []

    async def event_sink(event: dict[str, object]) -> None:
        events.append(event)

    reply, _metadata = asyncio.run(
        runtime.run_thread(PRINCIPAL, thread.thread_id, event_sink=event_sink)
    )

    expected_result = {
        "api_key": "<redacted>",
        "public": "visible",
    }
    expected_content = json.dumps(expected_result, ensure_ascii=True)
    assert reply == f"Tool result: {expected_content}"
    assert seen_tool_content == expected_content
    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert json.loads(messages[2].content) == expected_result
    tool_result_events = [event for event in events if event.get("type") == "tool.result"]
    assert tool_result_events == [
        {
            "type": "tool.result",
            "tool_call_id": "call-secret",
            "name": "secret_tool",
            "is_error": False,
            "result": expected_result,
        }
    ]


def test_runtime_keeps_private_mcp_values_out_of_model_history_and_events() -> None:
    placeholder = "{{pii:email:email-ref}}"
    seen_tool_content: str | None = None

    class PrivateContactThenReplyLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            nonlocal seen_tool_content
            if messages[-1].role == MessageRole.TOOL:
                seen_tool_content = messages[-1].content
                return LLMResponse(content=f"The contact email is {placeholder}.")
            return LLMResponse(
                tool_call=ToolCall(id="call-contact", name="contacts.list", arguments={})
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    registry = ToolRegistry()
    registry.register(
        name="contacts.list",
        description="List contacts with private values represented by placeholders.",
        input_schema={"type": "object", "properties": {}},
        handler=lambda arguments, context=None: MCPPrivateToolResult(
            model_content={"email": placeholder},
            private_values={"email-ref": "alice@example.com"},
        ),
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=PrivateContactThenReplyLLM(),
        tool_registry=registry,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="list contacts"),
    )
    events: list[dict[str, object]] = []

    async def event_sink(event: dict[str, object]) -> None:
        events.append(event)

    reply, _metadata = asyncio.run(
        runtime.run_thread(PRINCIPAL, thread.thread_id, event_sink=event_sink)
    )

    assert reply == "The contact email is alice@example.com."
    assert seen_tool_content is not None
    assert placeholder in seen_tool_content
    assert "alice@example.com" not in seen_tool_content
    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    stored_messages = json.dumps([message.content for message in messages])
    assert placeholder in stored_messages
    assert "alice@example.com" not in stored_messages
    rendered_messages = runtime.render_messages_for_user(
        PRINCIPAL,
        thread.thread_id,
        messages,
    )
    assert "alice@example.com" in json.dumps([message.content for message in rendered_messages])
    same_tenant_other_user = Principal(user_id="user-2", tenant_id=PRINCIPAL.tenant_id)
    other_user_messages = runtime.render_messages_for_user(
        same_tenant_other_user,
        thread.thread_id,
        messages,
    )
    assert "alice@example.com" not in json.dumps(
        [message.content for message in other_user_messages]
    )
    assert placeholder in json.dumps([message.content for message in other_user_messages])
    serialized_events = json.dumps(events)
    assert placeholder in serialized_events
    assert "alice@example.com" not in serialized_events


def test_runtime_protects_user_content_before_model_use() -> None:
    registry = ToolRegistry()
    registry.register(
        name="private-contacts.contacts_protect_text",
        description="Protect contact names.",
        input_schema={"type": "object"},
        handler=lambda arguments, context=None: MCPPrivateToolResult(
            model_content={
                "text": arguments["text"].replace("Alice Smith", "{{pii:contact:contact-ref}}"),
                "protected_contact_count": 1,
            },
            private_values={"contact-ref": "Alice Smith"},
        ),
        trusted_input_preprocessor=True,
    )
    runtime = AgentRuntime(
        store=InMemoryThreadStore(),
        llm_adapter=MockLLMAdapter(),
        tool_registry=registry,
    )

    protected = asyncio.run(
        runtime.protect_user_content(
            PRINCIPAL,
            "thread-1",
            "What is Alice Smith's email?",
        )
    )

    assert protected == "What is {{pii:contact:contact-ref}}'s email?"
    rendered = runtime.render_messages_for_user(
        PRINCIPAL,
        "thread-1",
        [Message(thread_id="thread-1", role=MessageRole.USER, content=protected)],
    )
    assert rendered[0].content == "What is Alice Smith's email?"


def test_runtime_hides_private_contact_preprocessor_from_model_tools() -> None:
    seen_tool_names: list[str] = []

    class InspectToolsLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            seen_tool_names.extend(tool.name for tool in tools)
            return LLMResponse(content="ok")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    registry = ToolRegistry()
    registry.register(
        name="private-contacts.contacts_protect_text",
        description="Protect contact names.",
        input_schema={"type": "object"},
        handler=lambda arguments, context=None: MCPPrivateToolResult(
            model_content={"text": arguments["text"], "protected_contact_count": 0},
            private_values={},
        ),
        trusted_input_preprocessor=True,
    )
    registry.register(
        name="private-contacts.contacts_list",
        description="List contacts.",
        input_schema={"type": "object"},
        handler=lambda arguments, context=None: {"contacts": []},
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=InspectToolsLLM(),
        tool_registry=registry,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    protected = asyncio.run(
        runtime.protect_user_content(PRINCIPAL, thread.thread_id, "list contacts")
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content=protected),
    )

    asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert seen_tool_names == ["private-contacts.contacts_list"]


def test_runtime_resolves_selected_private_values_only_at_trusted_tool_boundary() -> None:
    received: list[dict[str, object]] = []
    model_inputs: list[list[Message]] = []

    class SendThenReplyLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            model_inputs.append(messages)
            if messages[-1].role == MessageRole.TOOL:
                assert "private@example.com" not in json.dumps(
                    [message.model_dump(mode="json") for message in messages]
                )
                return LLMResponse(content="sent")
            placeholder = next(
                part for part in messages[-1].content.split() if part.startswith("{{pii:email:")
            )
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="send-1",
                        name="trusted.send",
                        arguments={"recipient": {"email": placeholder}},
                    )
                ]
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    registry = ToolRegistry()
    registry.register(
        "trusted.send",
        "Send a message.",
        {"type": "object"},
        lambda arguments, context=None: (
            received.append(arguments) or {"recipient": arguments["recipient"]}
        ),
        private_value_policy=MCPPrivateValuePolicy(
            mode="resolve_selected",
            argument_paths=("recipient.email",),
        ),
    )
    store = InMemoryThreadStore()
    consent_store = InMemoryPrivateValueConsentStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=SendThenReplyLLM(),
        tool_registry=registry,
        private_value_consent_store=consent_store,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    protected = asyncio.run(
        runtime.protect_user_content(
            PRINCIPAL,
            thread.thread_id,
            "Email private@example.com",
        )
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content=protected),
    )
    placeholder_match = PII_PLACEHOLDER_PATTERN.search(protected)
    assert placeholder_match is not None
    disclosure = PrivateValueDisclosure(
        path="recipient.email",
        kind=placeholder_match.group("kind"),
        reference=placeholder_match.group("reference"),
    )
    tool_arguments = {
        "recipient": {"email": placeholder_match.group(0)},
    }
    argument_fingerprint = hashlib.sha256(
        json.dumps(
            tool_arguments,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    try:
        consent_store.authorize_or_request(
            tenant_id=PRINCIPAL.tenant_id,
            user_id=PRINCIPAL.user_id,
            thread_id=thread.thread_id,
            tool_name="trusted.send",
            argument_fingerprint=argument_fingerprint,
            disclosures=(disclosure,),
        )
    except HTTPException as exc:
        assert exc.status_code == 428
    pending = runtime.pending_private_value_consents(PRINCIPAL, thread.thread_id)
    assert len(pending) == 1
    runtime.decide_private_value_consent(
        PRINCIPAL,
        thread.thread_id,
        str(pending[0]["consent_id"]),
        approve=True,
        one_shot=True,
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "sent"
    assert received == [{"recipient": {"email": "private@example.com"}}]
    stored = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert "private@example.com" not in json.dumps(
        [message.model_dump(mode="json") for message in stored]
    )
    rendered = runtime.render_messages_for_user(PRINCIPAL, thread.thread_id, stored)
    assert "private@example.com" in json.dumps(
        [message.model_dump(mode="json") for message in rendered]
    )
    assert len(model_inputs) == 2
    audit = runtime.private_value_disclosure_audit(PRINCIPAL, thread.thread_id)
    assert [record["event"] for record in audit] == [
        "requested",
        "approved",
        "disclosed",
    ]
    assert "private@example.com" not in json.dumps(audit)


def test_runtime_rejects_relabelled_private_placeholder_before_consent() -> None:
    received: list[dict[str, object]] = []

    class RelabelThenReplyLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            if messages[-1].role == MessageRole.TOOL:
                assert "kind does not match placeholder" in messages[-1].content
                return LLMResponse(content="blocked")
            placeholder = next(
                part for part in messages[-1].content.split() if part.startswith("{{pii:email:")
            )
            relabelled = placeholder.replace("{{pii:email:", "{{pii:phone:", 1)
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="send-invalid-kind",
                        name="trusted.send",
                        arguments={"recipient": {"email": relabelled}},
                    )
                ]
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    registry = ToolRegistry()
    registry.register(
        "trusted.send",
        "Send a message.",
        {"type": "object"},
        lambda arguments, context=None: received.append(arguments),
        private_value_policy=MCPPrivateValuePolicy(
            mode="resolve_selected",
            argument_paths=("recipient.email",),
        ),
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=RelabelThenReplyLLM(),
        tool_registry=registry,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    protected = asyncio.run(
        runtime.protect_user_content(
            PRINCIPAL,
            thread.thread_id,
            "Email private@example.com",
        )
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content=protected),
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "blocked"
    assert received == []
    assert runtime.pending_private_value_consents(PRINCIPAL, thread.thread_id) == []
    assert runtime.private_value_action_statuses(PRINCIPAL, thread.thread_id) == []
    assert runtime.private_value_disclosure_audit(PRINCIPAL, thread.thread_id) == []


def test_runtime_validates_expired_private_values_before_claiming_resumed_action() -> None:
    received: list[dict[str, object]] = []
    now = [100.0]

    class SendThenWaitLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            if messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content="approval required")
            placeholder = next(
                part for part in messages[-1].content.split() if part.startswith("{{pii:email:")
            )
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="send-expiring",
                        name="trusted.send",
                        arguments={"recipient": {"email": placeholder}},
                    )
                ]
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    registry = ToolRegistry()
    registry.register(
        "trusted.send",
        "Send a message.",
        {"type": "object"},
        lambda arguments, context=None: received.append(arguments),
        private_value_policy=MCPPrivateValuePolicy(
            mode="resolve_selected",
            argument_paths=("recipient.email",),
        ),
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=SendThenWaitLLM(),
        tool_registry=registry,
        private_value_store=InMemoryPrivateValueStore(
            ttl_seconds=5,
            clock=lambda: now[0],
        ),
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    protected = asyncio.run(
        runtime.protect_user_content(
            PRINCIPAL,
            thread.thread_id,
            "Email private@example.com",
        )
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content=protected),
    )
    asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))
    pending = runtime.pending_private_value_consents(PRINCIPAL, thread.thread_id)
    assert len(pending) == 1
    consent_id = str(pending[0]["consent_id"])
    runtime.decide_private_value_consent(
        PRINCIPAL,
        thread.thread_id,
        consent_id,
        approve=True,
        one_shot=True,
    )
    now[0] = 106.0

    with pytest.raises(HTTPException, match="missing or expired") as exc_info:
        asyncio.run(runtime.resume_private_value_consent(PRINCIPAL, thread.thread_id, consent_id))

    assert exc_info.value.status_code == 409
    assert received == []
    statuses = runtime.private_value_action_statuses(PRINCIPAL, thread.thread_id)
    assert len(statuses) == 1
    assert statuses[0]["consent_id"] == consent_id
    assert statuses[0]["tool_name"] == "trusted.send"
    assert statuses[0]["state"] == "pending"
    assert [
        record["event"]
        for record in runtime.private_value_disclosure_audit(PRINCIPAL, thread.thread_id)
    ] == ["requested", "approved"]


def test_runtime_runs_multiple_tool_calls_concurrently() -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def wait_tool(
        arguments: dict[str, object], context: ToolExecutionContext | None
    ) -> dict[str, object]:
        _ = context
        name = str(arguments["name"])
        started.append(name)
        if len(started) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1)
        return {"name": name}

    class MultiToolThenReplyLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            if messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content="done")
            return LLMResponse(
                tool_calls=[
                    ToolCall(id="call-a", name="wait", arguments={"name": "a"}),
                    ToolCall(id="call-b", name="wait", arguments={"name": "b"}),
                ]
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    registry = ToolRegistry()
    registry.register(
        "wait",
        "Wait until both calls have started.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        wait_tool,
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(store=store, llm_adapter=MultiToolThenReplyLLM(), tool_registry=registry)
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="use two tools"),
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "done"
    assert sorted(started) == ["a", "b"]


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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "ok"
    assert seen_messages[0].role == MessageRole.SYSTEM
    assert seen_messages[1].role == MessageRole.SYSTEM
    assert seen_messages[1].content.startswith(
        "Thread summary:\nUser: user-0\nAssistant: assistant-0"
    )
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
    assert [
        message.content for message in store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    ] == [
        "user-4",
        "assistant-4",
        "user-5",
        "assistant-5",
        "ok",
    ]


def test_runtime_can_disable_context_compaction_for_append_only_cache_prefixes() -> None:
    seen_messages: list[list[Message]] = []

    class InspectingLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            seen_messages.append(messages)
            return LLMResponse(content="ok")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=InspectingLLM(),
        tool_registry=build_local_tool_registry(),
        recent_message_limit=2,
        target_prompt_tokens=80,
        context_compaction_enabled=False,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    for index in range(4):
        store.append_message(
            PRINCIPAL.tenant_id,
            Message(
                thread_id=thread.thread_id,
                role=MessageRole.USER,
                content=f"user-{index} " + ("x" * 160),
                created_by=PRINCIPAL.user_id,
            ),
        )
        asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    context = store.get_thread_context(PRINCIPAL.tenant_id, thread.thread_id)
    assert context.summary == ""
    assert context.summarized_message_count == 0
    stored_messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert len(stored_messages) == 8
    assert [message.content for message in stored_messages[:2]] == [
        "user-0 " + ("x" * 160),
        "ok",
    ]
    assert [message.content for message in seen_messages[-1][1:]] == [
        message.content for message in stored_messages[:-1]
    ]


def test_runtime_manual_compaction_works_when_automatic_compaction_is_disabled() -> None:
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
        recent_message_limit=2,
        context_compaction_enabled=False,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    for index in range(3):
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

    context = runtime.compact_thread(PRINCIPAL, thread.thread_id)

    assert "user-0" in context.summary
    assert "assistant-1" in context.summary
    assert context.summarized_message_count == 0
    assert [
        message.content for message in store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    ] == ["user-2", "assistant-2"]

    asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert seen_messages[1].role == MessageRole.SYSTEM
    assert seen_messages[1].content.startswith("Thread summary:\n")
    assert [message.content for message in seen_messages[2:]] == ["user-2", "assistant-2"]


def test_runtime_manual_compaction_keeps_tool_call_pairs_together() -> None:
    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store,
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(),
        recent_message_limit=3,
        context_compaction_enabled=False,
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="old user"),
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.ASSISTANT,
            content="",
            tool_name="echo",
            tool_call_id="call_1",
            tool_arguments={"text": "hello"},
        ),
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.TOOL,
            content='{"echo":"hello"}',
            tool_name="echo",
            tool_call_id="call_1",
        ),
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.ASSISTANT, content="old answer"),
    )
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="new user"),
    )

    runtime.compact_thread(PRINCIPAL, thread.thread_id)

    retained = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert [message.role for message in retained] == [MessageRole.USER]
    assert retained[0].content == "new user"
    context = store.get_thread_context(PRINCIPAL.tenant_id, thread.thread_id)
    assert "Assistant requested tool echo" in context.summary
    assert 'Tool echo returned {"echo":"hello"}' in context.summary


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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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
        ),
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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


def test_runtime_executes_direct_tool_command_without_llm_tool_planning() -> None:
    seen_user_prompts: list[str] = []

    class NonMockLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            seen_user_prompts.extend(
                message.content for message in messages if message.role == MessageRole.USER
            )
            if messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content=f"Tool result: {messages[-1].content}")
            raise AssertionError("direct /tool command should be executed before LLM planning")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    store = InMemoryThreadStore()
    runtime = AgentRuntime(
        store=store, llm_adapter=NonMockLLM(), tool_registry=build_local_tool_registry()
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content='/tool echo {"text":"hello from direct tool"}',
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert reply == 'Tool result: {"echo": "hello from direct tool"}'
    assert messages[1].tool_name == "echo"
    assert messages[1].tool_arguments == {"text": "hello from direct tool"}
    assert messages[1].tool_call_id == "direct-echo-call"
    assert seen_user_prompts == ['/tool echo {"text":"hello from direct tool"}']


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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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


def test_runtime_exposes_peer_routing_hints_to_llm_and_delegates() -> None:
    seen_peer_description: str | None = None

    class PeerAwareLLM(LLMAdapter):
        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            nonlocal seen_peer_description
            if messages and messages[-1].role == MessageRole.TOOL:
                return LLMResponse(content=f"Tool result: {messages[-1].content}")
            specs = {tool.name: tool for tool in tools}
            peer_spec = specs["peer_agent_task"]
            seen_peer_description = peer_spec.description
            assert "codex" in peer_spec.description
            assert "repository analysis" in peer_spec.description
            assert "runs local commands" in peer_spec.description
            return LLMResponse(
                tool_call=ToolCall(
                    id="call-peer",
                    name="peer_agent_task",
                    arguments={
                        "peer": "codex",
                        "cwd": "/workspace/project",
                        "prompt": "Summarize this repository. Do not edit files.",
                        "poll": False,
                    },
                )
            )

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    class FakePeerRegistry(PeerAgentRegistry):
        def __init__(self) -> None:
            super().__init__(
                [
                    PeerAgentConfig(
                        name="codex",
                        base_url="http://codex-agent.test",
                        description="Local coding-agent wrapper",
                        capabilities=("repository analysis",),
                        side_effects=("runs local commands",),
                    )
                ]
            )
            self.created_tasks: list[tuple[str, dict[str, object]]] = []

        async def create_task(self, name: str, payload: dict[str, object]) -> dict[str, object]:
            self.created_tasks.append((name, payload))
            return {"task_id": "task_123", "status": "running", "exit_code": None}

    peer_registry = FakePeerRegistry()
    registry = build_local_tool_registry(
        peer_agent_registry=peer_registry,
        enable_peer_agent_tool=True,
    )
    store = InMemoryThreadStore()
    runtime = AgentRuntime(store=store, llm_adapter=PeerAwareLLM(), tool_registry=registry)
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="summarize this repo",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert seen_peer_description is not None
    assert "Available peers:" in seen_peer_description
    assert reply.startswith('Tool result: {"peer": "codex", "task_id": "task_123"')
    assert peer_registry.created_tasks == [
        (
            "codex",
            {
                "cwd": "/workspace/project",
                "prompt": "Summarize this repository. Do not edit files.",
            },
        )
    ]
    messages = store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
    assert messages[1].tool_name == "peer_agent_task"
    assert messages[1].tool_call_id == "call-peer"
    assert messages[1].tool_arguments == {
        "peer": "codex",
        "cwd": "/workspace/project",
        "prompt": "Summarize this repository. Do not edit files.",
        "poll": False,
    }
    tool_payload = json.loads(messages[2].content)
    assert tool_payload["peer"] == "codex"
    assert tool_payload["task_id"] == "task_123"


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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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
        assert exc.detail == {
            "code": "max_iterations",
            "message": "Reached tool call limit (2). You can type 'continue' to keep going.",
        }
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected HTTPException")

    assert store._threads[thread.thread_id].status == ThreadStatus.ERROR


def test_runtime_settings_from_env_mapping_uses_defaults() -> None:
    settings = RuntimeSettings.from_env({})

    assert settings == RuntimeSettings(
        max_iterations=DEFAULT_MAX_ITERATIONS,
        tool_timeout_seconds=DEFAULT_TOOL_TIMEOUT_SECONDS,
        context_compaction_enabled=False,
    )


def test_runtime_settings_from_env_mapping_parses_values() -> None:
    settings = RuntimeSettings.from_env(
        {
            "MINIGENT_MAX_ITERATIONS": "24",
            "MINIGENT_TOOL_TIMEOUT_SECONDS": "2.5",
            "MINIGENT_CONTEXT_COMPACTION_ENABLED": "true",
        }
    )

    assert settings == RuntimeSettings(
        max_iterations=24,
        tool_timeout_seconds=2.5,
        context_compaction_enabled=True,
    )


def test_runtime_settings_from_env_mapping_rejects_invalid_values() -> None:
    try:
        RuntimeSettings.from_env(
            {
                "MINIGENT_MAX_ITERATIONS": "24",
                "MINIGENT_TOOL_TIMEOUT_SECONDS": "0",
                "MINIGENT_CONTEXT_COMPACTION_ENABLED": "true",
            }
        )
    except RuntimeError as exc:
        assert str(exc) == "MINIGENT_TOOL_TIMEOUT_SECONDS must be a positive number"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected RuntimeError")


def test_max_iterations_from_env_uses_practical_default(monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_MAX_ITERATIONS", raising=False)

    assert max_iterations_from_env() == DEFAULT_MAX_ITERATIONS
    assert DEFAULT_MAX_ITERATIONS == 16


def test_max_iterations_from_env_accepts_positive_integer(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_MAX_ITERATIONS", "24")

    assert max_iterations_from_env() == 24


def test_max_iterations_from_env_rejects_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_MAX_ITERATIONS", "0")

    try:
        max_iterations_from_env()
    except RuntimeError as exc:
        assert str(exc) == "MINIGENT_MAX_ITERATIONS must be a positive integer"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected RuntimeError")


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
        first_reply, _first_metadata = await first_run
        assert first_reply == "done"
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


def test_parse_tenant_execution_config_supports_tool_result_redaction_policy() -> None:
    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "tools": {
                "allowed_local_tools": ["echo"],
                "result_redaction": {
                    "mode": "full",
                    "sensitive_tools": ["echo"],
                },
                "mcp_servers": [
                    {
                        "name": "docs",
                        "url": "https://docs.example/mcp",
                        "headers": {},
                        "result_redaction": {"enabled": False},
                        "forward_identity": True,
                        "identity_audience": "private-dav",
                        "identity_scopes": ["dav:calendar:read"],
                    }
                ],
            },
        },
    )

    assert config.tools.result_redaction_policy.enabled is True
    assert config.tools.result_redaction_policy.mode == "full"
    assert config.tools.result_redaction_policy.sensitive_tools == frozenset({"echo"})
    assert config.tools.mcp_servers[0].result_redaction_policy.enabled is False
    assert config.tools.mcp_servers[0].forward_identity is True
    assert config.tools.mcp_servers[0].identity_audience == "private-dav"
    assert config.tools.mcp_servers[0].identity_scopes == ("dav:calendar:read",)


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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "ok"
    assert seen_messages[0].content == (
        f"{RUNTIME_SYSTEM_PROMPT}\n\n"
        "[Skill: support]\nAnswer as a concise support agent.\n\n"
        "[Skill: safe-actions]\nRequire confirmation before risky actions."
    )


def test_runtime_lazily_loads_active_agent_skill_body(tmp_path: Path) -> None:
    active_dir = tmp_path / "active"
    active_dir.mkdir()
    active_skill = active_dir / "SKILL.md"
    active_skill.write_text(
        "---\nname: agent-reviewer\ndescription: Reviews work.\n---\n\n"
        "Loaded active Agent Skill instructions.",
        encoding="utf-8",
    )
    inactive_dir = tmp_path / "inactive"
    inactive_dir.mkdir()
    inactive_skill = inactive_dir / "SKILL.md"
    inactive_skill.write_text(
        "---\nname: inactive\ndescription: Should stay unloaded.\n---\n\n"
        "Inactive Agent Skill instructions must not appear.",
        encoding="utf-8",
    )
    seen_messages: list[Message] = []
    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "skills": {
                "items": [
                    {
                        "name": "agent-reviewer",
                        "description": "Reviews work.",
                        "instruction_source": {
                            "type": "agent_skill",
                            "path": str(active_skill),
                        },
                    },
                    {
                        "name": "inactive",
                        "description": "Should stay unloaded.",
                        "instruction_source": {
                            "type": "agent_skill",
                            "path": str(inactive_skill),
                        },
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
    thread = store.create_thread(PRINCIPAL.tenant_id, skill_name="agent-reviewer")
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.USER,
            content="hello",
            created_by=PRINCIPAL.user_id,
        ),
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

    assert reply == "ok"
    assert "Loaded active Agent Skill instructions." in seen_messages[0].content
    assert "Inactive Agent Skill instructions must not appear." not in seen_messages[0].content


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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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
                ],
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

    personal_registry = build_tool_registry_for_constraints(
        config,
        profile_allowed_local_tools=["current_time"],
        profile_mcp_server_names={"home-assistant"},
        allowed_mcp_server_names={"docs"},
    )
    assert {spec.name for spec in personal_registry.specs()} == {"current_time"}
    assert personal_registry.mcp_servers() == []

    subject_registry = build_tool_registry_for_skill(
        config,
        "home-assistant",
        allowed_mcp_server_names={"docs"},
    )
    assert {spec.name for spec in subject_registry.specs()} == {"current_time"}
    assert subject_registry.mcp_servers() == []


def test_capability_profile_preserves_explicit_empty_tool_allowlists() -> None:
    config = parse_tenant_execution_config(
        PRINCIPAL.tenant_id,
        {
            "tools": {
                "allowed_local_tools": ["current_time"],
                "mcp_servers": [{"name": "docs", "url": "https://docs.example/mcp", "headers": {}}],
            },
            "capability_profiles": {
                "default_profile": "safe-default",
                "items": [
                    {
                        "name": "safe-default",
                        "allowedLocalTools": [],
                        "mcpServerNames": [],
                    }
                ],
            },
        },
    )

    profile = config.capability_profiles.items[0]
    assert profile.allowed_local_tools == []
    assert profile.mcp_server_names == []

    registry = build_tool_registry_for_capability_profile(config, "safe-default")
    assert registry.specs() == []
    assert registry.mcp_servers() == []


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

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id))

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
                ],
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


def test_store_backed_resolvers_refresh_cached_contexts_from_shared_versions(
    tmp_path: Path,
) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_raw_config(
        DEFAULT_TENANT_KEY,
        {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["echo"]},
        },
    )
    writer_resolver = StoreBackedTenantExecutionResolver(store)
    other_replica_resolver = StoreBackedTenantExecutionResolver(store)

    writer_resolver.resolve(PRINCIPAL.tenant_id)
    original = other_replica_resolver.resolve(PRINCIPAL.tenant_id)
    assert _local_tool_names(original) == {"echo"}

    store.upsert_raw_config(
        DEFAULT_TENANT_KEY,
        {
            "llm": {
                "provider": "openrouter",
                "model": "openrouter/test-model",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "test-key",
            },
            "tools": {"allowed_local_tools": ["calculator"]},
        },
    )
    writer_resolver.invalidate(PRINCIPAL.tenant_id)

    refreshed_default = other_replica_resolver.resolve(PRINCIPAL.tenant_id)
    assert refreshed_default is not original
    assert refreshed_default.llm_adapter.describe()["provider"] == "openrouter"
    assert refreshed_default.llm_adapter.describe()["model"] == "openrouter/test-model"
    assert _local_tool_names(refreshed_default) == {"calculator"}

    store.upsert_raw_config(
        PRINCIPAL.tenant_id,
        {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["current_time"]},
        },
    )
    direct = other_replica_resolver.resolve(PRINCIPAL.tenant_id)
    assert _local_tool_names(direct) == {"current_time"}

    assert store.delete_config(PRINCIPAL.tenant_id) is True
    restored_default = other_replica_resolver.resolve(PRINCIPAL.tenant_id)
    assert restored_default.llm_adapter.describe()["provider"] == "openrouter"
    assert _local_tool_names(restored_default) == {"calculator"}


def _local_tool_names(context: TenantExecutionContext) -> set[str]:
    return {spec.name for spec in context.tool_registry.specs() if "." not in spec.name}


def test_runtime_applies_enabled_remote_quality_critique() -> None:
    events: list[dict[str, object]] = []

    class DraftThenRevisionLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
            _ = tools
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="Draft with /Users/alice and alice@example.com")
            critique_message = next(
                message.content for message in messages if "Advisory critique" in message.content
            )
            assert "[PATH]" in critique_message
            assert "[EMAIL]" in critique_message
            assert "/Users/alice" not in critique_message
            assert "alice@example.com" not in critique_message
            synthesis_system = next(
                message.content
                for message in messages
                if message.role == MessageRole.SYSTEM
                and "remote critique may refer to sanitized placeholders" in message.content
            )
            assert "Preserve concrete details from the original local draft" in synthesis_system
            return LLMResponse(content="Revised final answer")

        def describe(self) -> dict[str, object]:
            return {"provider": "test"}

    async def emit(event: dict[str, object]) -> None:
        events.append(event)

    store = InMemoryThreadStore()
    llm = DraftThenRevisionLLM()
    registry = build_local_tool_registry()
    resolver = FixedTenantExecutionResolver(
        llm,
        registry,
        config=TenantExecutionConfig(
            tenant_id=PRINCIPAL.tenant_id,
            quality=TenantQualityConfig(enabled=True, provider="mock"),
        ),
    )
    runtime = AgentRuntime(
        store=store,
        execution_resolver=resolver,
        quality_enhancer=QualityEnhancer(),
    )
    thread = store.create_thread(PRINCIPAL.tenant_id)
    store.append_message(
        PRINCIPAL.tenant_id,
        Message(thread_id=thread.thread_id, role=MessageRole.USER, content="hello"),
    )

    reply, _metadata = asyncio.run(runtime.run_thread(PRINCIPAL, thread.thread_id, event_sink=emit))

    assert reply == "Revised final answer"
    assert llm.calls == 2
    assert store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)[-1].content == reply
    event_types = [event["type"] for event in events]
    assert "quality.sanitized" in event_types
    assert "quality.remote_request" in event_types
    assert "quality.applied" in event_types
