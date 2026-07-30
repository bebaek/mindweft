from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.llm import LLMAdapter
from app.main import create_app
from app.mcp import MCPPrivateValuePolicy
from app.models import LLMResponse, Message, MessageRole, ToolCall, ToolSpec
from app.tools import ToolRegistry

AUTH_HEADERS = {
    "X-Minigent-User-Id": "user-1",
    "X-Minigent-Tenant-Id": "tenant-1",
}


class ConsentFlowLLM(LLMAdapter):
    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        if messages[-1].role == MessageRole.TOOL:
            result = json.loads(messages[-1].content)
            if result.get("error", {}).get("status_code") == 428:
                return LLMResponse(content="Approval is required.")
            return LLMResponse(content="Sent.")
        placeholder = next(
            token
            for message in messages
            if message.role == MessageRole.USER
            for token in message.content.split()
            if token.startswith("{{pii:email:")
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


def test_private_value_consent_api_approves_one_shot_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_AUTH_MODE", "dev-headers")
    received: list[dict[str, object]] = []
    registry = ToolRegistry()
    registry.register(
        "trusted.send",
        "Send a message.",
        {"type": "object"},
        lambda arguments, context=None: received.append(arguments) or {"sent": True},
        private_value_policy=MCPPrivateValuePolicy(
            mode="resolve_selected",
            argument_paths=("recipient.email",),
        ),
    )
    app = create_app(llm_adapter=ConsentFlowLLM(), tool_registry=registry)
    client = TestClient(app)
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
        json={"content": "Email private@example.com"},
    ).raise_for_status()

    first_run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert first_run.status_code == 200
    assert first_run.json() == {"reply": "Approval is required."}
    assert received == []
    pending_response = client.get(
        f"/threads/{thread_id}/private-value-consents/pending",
        headers=AUTH_HEADERS,
    )
    pending_response.raise_for_status()
    pending = pending_response.json()
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "trusted.send"
    assert pending[0]["disclosures"] == [{"path": "recipient.email", "kind": "email", "count": 1}]
    assert "private@example.com" not in pending_response.text
    actions_response = client.get(
        f"/threads/{thread_id}/private-value-actions",
        headers=AUTH_HEADERS,
    )
    actions_response.raise_for_status()
    assert actions_response.json() == [
        {
            "consent_id": pending[0]["consent_id"],
            "thread_id": thread_id,
            "tool_name": "trusted.send",
            "state": "pending",
            "expires_at": pending[0]["expires_at"],
        }
    ]
    assert "private@example.com" not in actions_response.text

    approve_response = client.post(
        f"/threads/{thread_id}/private-value-consents/{pending[0]['consent_id']}",
        headers=AUTH_HEADERS,
        json={"approve": True, "one_shot": True},
    )
    approve_response.raise_for_status()
    resume_response = client.post(
        f"/threads/{thread_id}/private-value-consents/{pending[0]['consent_id']}/resume",
        headers=AUTH_HEADERS,
    )

    assert resume_response.status_code == 200
    assert resume_response.json() == {"reply": "Sent."}
    assert received == [{"recipient": {"email": "private@example.com"}}]
    assert (
        client.get(
            f"/threads/{thread_id}/private-value-actions",
            headers=AUTH_HEADERS,
        ).json()
        == []
    )
    audit_response = client.get(
        f"/threads/{thread_id}/private-value-disclosures/audit",
        headers=AUTH_HEADERS,
    )
    audit_response.raise_for_status()
    audit = audit_response.json()
    assert [record["event"] for record in audit] == [
        "requested",
        "approved",
        "disclosed",
    ]
    assert "private@example.com" not in audit_response.text

    discarded_thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    client.post(
        f"/threads/{discarded_thread_id}/messages",
        headers=AUTH_HEADERS,
        json={"content": "Email discard@example.com"},
    ).raise_for_status()
    client.post(f"/threads/{discarded_thread_id}/run", headers=AUTH_HEADERS).raise_for_status()
    discarded_pending = client.get(
        f"/threads/{discarded_thread_id}/private-value-consents/pending",
        headers=AUTH_HEADERS,
    ).json()
    discard_response = client.delete(
        f"/threads/{discarded_thread_id}/private-value-actions/"
        f"{discarded_pending[0]['consent_id']}",
        headers=AUTH_HEADERS,
    )
    discard_response.raise_for_status()
    assert discard_response.json()["discarded"] is True
    assert (
        client.get(
            f"/threads/{discarded_thread_id}/private-value-actions",
            headers=AUTH_HEADERS,
        ).json()
        == []
    )
    discarded_audit = client.get(
        f"/threads/{discarded_thread_id}/private-value-disclosures/audit",
        headers=AUTH_HEADERS,
    ).json()
    assert [record["event"] for record in discarded_audit] == ["requested", "discarded"]
    assert "discard@example.com" not in json.dumps(discarded_audit)
