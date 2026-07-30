from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

from app.llm import LLMAdapter
from app.mcp import PRIVATE_VALUES_META_KEY, MCPHTTPClient, MCPServerConfig
from app.models import LLMResponse, Message, MessageRole, Principal, ToolCall, ToolSpec
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
CONTACTS = {
    "alice-ref": {
        "name": "Alice Smith",
        "name_ref": "alice-name-ref",
        "email": "alice@example.com",
        "email_ref": "alice-email-ref",
        "phone": "+1 555 0100",
        "phone_ref": "alice-phone-ref",
    },
    "bob-ref": {
        "name": "Bob Jones",
        "name_ref": "bob-name-ref",
        "email": "bob@example.com",
        "email_ref": "bob-email-ref",
        "phone": "+1 555 0101",
        "phone_ref": "bob-phone-ref",
    },
}


def _private_mcp_app() -> FastAPI:
    app = FastAPI()

    @app.post("/mcp", response_model=None)
    async def mcp(request: Request) -> Response | dict[str, Any]:
        payload = await request.json()
        method = payload.get("method")
        request_id = payload.get("id")
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            return _result(
                request_id,
                {
                    "protocolVersion": "2025-11-25",
                    "serverInfo": {"name": "private-contract-test", "version": "1"},
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            return _result(
                request_id,
                {
                    "tools": [
                        {
                            "name": "contacts_list",
                            "description": "List protected contacts.",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "contacts_get",
                            "description": "Get protected fields.",
                            "inputSchema": {"type": "object"},
                        },
                        {
                            "name": "contacts_protect_text",
                            "description": "Protect contact names.",
                            "inputSchema": {"type": "object"},
                        },
                    ]
                },
            )
        params = payload.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "contacts_list":
            private_values = {
                str(contact["name_ref"]): str(contact["name"]) for contact in CONTACTS.values()
            }
            contacts = [
                {
                    "contact_ref": contact_ref,
                    "name": f"{{{{pii:name:{contact['name_ref']}}}}}",
                    "available_fields": ["emails", "phones"],
                }
                for contact_ref, contact in CONTACTS.items()
            ]
            return _private_result(
                request_id, {"contacts": contacts, "truncated": False}, private_values
            )
        if name == "contacts_get":
            contact_ref = arguments["contact_ref"]
            contact = CONTACTS[contact_ref]
            return _private_result(
                request_id,
                {
                    "contact_ref": contact_ref,
                    "emails": [f"{{{{pii:email:{contact['email_ref']}}}}}"],
                    "phones": [f"{{{{pii:phone:{contact['phone_ref']}}}}}"],
                },
                {
                    str(contact["email_ref"]): str(contact["email"]),
                    str(contact["phone_ref"]): str(contact["phone"]),
                },
            )
        if name == "contacts_protect_text":
            text = str(arguments["text"])
            return _private_result(
                request_id,
                {"text": text.replace("Alice Smith", "{{pii:contact:alice-ref}}")},
                {"alice-ref": "Alice Smith"},
            )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": "Unknown tool"},
        }

    return app


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _private_result(
    request_id: Any,
    structured_content: dict[str, Any],
    private_values: dict[str, str],
) -> dict[str, Any]:
    return _result(
        request_id,
        {
            "content": [{"type": "text", "text": "Protected result."}],
            "structuredContent": structured_content,
            "_meta": {PRIVATE_VALUES_META_KEY: private_values},
        },
    )


def test_private_mcp_contract_keeps_pii_out_of_llm_history_and_events() -> None:
    async def run() -> None:
        client = MCPHTTPClient(
            MCPServerConfig(
                name="private-contacts",
                url="http://private-contacts.test/mcp",
                headers={},
                allowed_tools=["contacts_list", "contacts_get", "contacts_protect_text"],
            ),
            transport=httpx.ASGITransport(app=_private_mcp_app()),
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
                trusted_input_preprocessor=raw_tool_name == "contacts_protect_text",
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
                    f"{names[result['contact_ref']]} — {result['emails'][0]} — {result['phones'][0]}"
                    for result in tool_payloads[1:]
                ]
                return LLMResponse(content="\n".join(lines))

            def describe(self) -> dict[str, object]:
                return {"provider": "private-mcp-contract-test"}

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
            Message(thread_id=thread.thread_id, role=MessageRole.USER, content=protected_prompt),
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
        for kind in ("name", "email", "phone"):
            assert f"{{{{pii:{kind}:" in serialized_llm_requests
            assert f"{{{{pii:{kind}:" in stored_messages
            assert f"{{{{pii:{kind}:" in serialized_events

    asyncio.run(run())
