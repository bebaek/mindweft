from __future__ import annotations

import asyncio
import json

import httpx

from app.llm import LLMAdapter
from app.mcp import MCPHTTPClient, MCPServerConfig
from app.models import LLMResponse, Message, MessageRole, Principal, ToolCall, ToolSpec
from app.private_contacts_mcp_demo import create_app
from app.runtime import AgentRuntime
from app.store import InMemoryThreadStore
from app.tools import ToolRegistry

PRINCIPAL = Principal(user_id="private-contact-user", tenant_id="private-contact-tenant")
RAW_PRIVATE_VALUES = (
    "Alice Smith",
    "alice@example.com",
    "+1 555 0100",
    "Bob Jones",
    "bob@example.com",
    "+1 555 0101",
)


def test_private_contacts_mcp_keeps_pii_out_of_llm_history_and_events() -> None:
    async def run() -> None:
        client = MCPHTTPClient(
            MCPServerConfig(
                name="private-contacts",
                url="http://private-contacts.test/mcp",
                headers={},
                allowed_tools=[
                    "contacts_list",
                    "contacts_get",
                    "contacts_protect_text",
                ],
            ),
            transport=httpx.ASGITransport(app=create_app()),
        )
        specs = await client.list_tools()
        registry = ToolRegistry()
        for spec in specs:
            raw_tool_name = spec.name.split(".", 1)[1]
            registry.register(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
                handler=lambda arguments, context=None, tool_name=raw_tool_name: client.call_tool(
                    tool_name, arguments
                ),
            )

        seen_llm_messages: list[list[Message]] = []

        class ContactsThenReplyLLM(LLMAdapter):
            async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
                seen_llm_messages.append(messages)
                tool_payloads = [
                    json.loads(message.content)
                    for message in messages
                    if message.role == MessageRole.TOOL
                ]
                if not tool_payloads:
                    return LLMResponse(
                        tool_call=ToolCall(
                            id="contacts-list-call",
                            name="private-contacts.contacts_list",
                            arguments={},
                        )
                    )
                listed_contacts = tool_payloads[0].get("contacts")
                if listed_contacts is not None and len(tool_payloads) == 1:
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id=f"contacts-get-{index}",
                                name="private-contacts.contacts_get",
                                arguments={
                                    "contact_ref": contact["contact_ref"],
                                    "fields": ["emails", "phones"],
                                },
                            )
                            for index, contact in enumerate(listed_contacts)
                        ]
                    )
                names = {
                    contact["contact_ref"]: contact["name"] for contact in listed_contacts or []
                }
                lines = [
                    (
                        f"{names[result['contact_ref']]} — {result['emails'][0]} — "
                        f"{result['phones'][0]}"
                    )
                    for result in tool_payloads[1:]
                ]
                return LLMResponse(content="\n".join(lines))

            def describe(self) -> dict[str, object]:
                return {"provider": "private-contacts-integration-test"}

        store = InMemoryThreadStore()
        runtime = AgentRuntime(
            store=store,
            llm_adapter=ContactsThenReplyLLM(),
            tool_registry=registry,
        )
        thread = store.create_thread(PRINCIPAL.tenant_id)
        protected_prompt = await runtime.protect_user_content(
            PRINCIPAL,
            thread.thread_id,
            "List email addresses and phone numbers for Alice Smith.",
        )
        store.append_message(
            PRINCIPAL.tenant_id,
            Message(
                thread_id=thread.thread_id,
                role=MessageRole.USER,
                content=protected_prompt,
            ),
        )
        events: list[dict[str, object]] = []

        async def event_sink(event: dict[str, object]) -> None:
            events.append(event)

        reply, _metadata = await runtime.run_thread(
            PRINCIPAL,
            thread.thread_id,
            event_sink=event_sink,
        )

        assert reply == (
            "Alice Smith — alice@example.com — +1 555 0100\n"
            "Bob Jones — bob@example.com — +1 555 0101"
        )
        serialized_llm_requests = json.dumps(
            [
                [message.model_dump(mode="json") for message in messages]
                for messages in seen_llm_messages
            ]
        )
        stored_messages = json.dumps(
            [
                message.model_dump(mode="json")
                for message in store.list_messages(PRINCIPAL.tenant_id, thread.thread_id)
            ]
        )
        serialized_events = json.dumps(events)
        for private_value in RAW_PRIVATE_VALUES:
            assert private_value not in serialized_llm_requests
            assert private_value not in stored_messages
            assert private_value not in serialized_events
        assert "{{pii:name:" in serialized_llm_requests
        assert "{{pii:email:" in serialized_llm_requests
        assert "{{pii:phone:" in serialized_llm_requests
        assert "{{pii:name:" in stored_messages
        assert "{{pii:email:" in stored_messages
        assert "{{pii:phone:" in stored_messages
        assert "{{pii:name:" in serialized_events
        assert "{{pii:email:" in serialized_events
        assert "{{pii:phone:" in serialized_events

    asyncio.run(run())
