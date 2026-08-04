from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.llm import MockLLMAdapter
from app.main import create_app
from app.tools import build_local_tool_registry

AUTH_HEADERS = {
    "X-Minigent-User-Id": "user-1",
    "X-Minigent-Tenant-Id": "tenant-1",
}


def test_add_message_masks_local_pii_in_storage_and_rehydrates_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_AUTH_MODE", "dev-headers")
    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    client = TestClient(app)
    thread_id = client.post("/threads", headers=AUTH_HEADERS).json()["thread_id"]
    private_message = (
        "Email Jane Doe at jane@example.com or call +1 (415) 555-0123 at 123 Main Street, Apt 4."
    )

    response = client.post(
        f"/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
        json={"content": private_message},
    )

    assert response.status_code == 200
    assert response.json()["content"] == private_message
    stored = app.state.store.list_messages("tenant-1", thread_id)[0].content
    assert "Jane Doe" not in stored
    assert "jane@example.com" not in stored
    assert "+1 (415) 555-0123" not in stored
    assert "123 Main Street, Apt 4" not in stored
    assert "{{pii:person:" in stored
    assert "{{pii:email:" in stored
    assert "{{pii:phone:" in stored
    assert "{{pii:address:" in stored
