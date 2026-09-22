"""Contract tests for optional personal-agent model preferences (no provider calls)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.admin_store import SQLiteTenantConfigStore
from app.llm import MockLLMAdapter
from app.main import create_app
from app.models import LLMResponse

HEADERS = {"X-Mindweft-Tenant-Id": "tenant-1", "X-Mindweft-User-Id": "user-1"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MINDWEFT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "llm_profiles": {
                        "fast": {"provider": "mock", "model": "fast-model"},
                        "deep": {"provider": "mock", "model": "deep-model"},
                    },
                    "default_llm_profile": "fast",
                },
                "tenant-2": {"llm": {"provider": "mock"}},
            }
        ),
    )
    app = create_app(admin_store=SQLiteTenantConfigStore(str(tmp_path / "admin.db")))
    with TestClient(app) as client:
        yield client


def save_agent(client, profile, headers=HEADERS):
    version = client.get("/me/agents", headers=headers).json()["version"] or 0
    result = client.put(
        "/me/agents/user:reviewer",
        headers=headers,
        json={
            "expected_version": version,
            "resource": {"id": "user:reviewer", "name": "Reviewer", "llm_profile": profile},
        },
    )
    assert result.status_code == 200, result.text


def create_thread(client, **selection):
    result = client.post(
        "/threads", headers=HEADERS, json={"agent_name": "user:reviewer", **selection}
    )
    assert result.status_code == 200, result.text
    return client.app.state.store.get_thread("tenant-1", result.json()["thread_id"])


def test_preference_inheritance_override_and_edits_are_snapshotted(client):
    save_agent(client, "deep")
    preferred = create_thread(client)
    overridden = create_thread(client, llm_profile="fast")
    assert preferred.llm_profile == "deep"
    assert overridden.llm_profile == "fast"
    save_agent(client, None)
    inherited = create_thread(client)
    assert inherited.llm_profile == "fast"
    assert client.app.state.store.get_thread("tenant-1", preferred.thread_id).llm_profile == "deep"
    context = client.app.state.execution_resolver.resolve("tenant-1")
    # Preferences remain optional; a changed default affects new conversations only.
    from dataclasses import replace

    object.__setattr__(context, "config", replace(context.config, default_llm_profile="deep"))
    assert create_thread(client).llm_profile == "deep"
    assert client.app.state.store.get_thread("tenant-1", inherited.thread_id).llm_profile == "fast"


def test_missing_profile_and_other_principal_do_not_fall_back(client):
    save_agent(client, "missing")
    response = client.post("/threads", headers=HEADERS, json={"agent_name": "user:reviewer"})
    assert response.status_code == 400
    assert "Unknown LLM profile" in response.text
    assert client.app.state.store.list_threads("tenant-1") == []
    save_agent(client, "deep")
    for headers in (
        {**HEADERS, "X-Mindweft-User-Id": "user-2"},
        {**HEADERS, "X-Mindweft-Tenant-Id": "tenant-2"},
    ):
        response = client.post("/threads", headers=headers, json={"agent_name": "user:reviewer"})
        assert response.status_code == 400
    other = {**HEADERS, "X-Mindweft-Tenant-Id": "tenant-2"}
    save_agent(client, "deep", other)
    assert (
        client.post("/threads", headers=other, json={"agent_name": "user:reviewer"}).status_code
        == 400
    )


def test_runtime_uses_selected_adapter_and_removed_profile_fails(client):
    class RecordingAdapter(MockLLMAdapter):
        calls = 0

        async def generate(self, messages, tools):
            self.calls += 1
            return LLMResponse(content="selected model reply")

    save_agent(client, "deep")
    thread = create_thread(client)
    context = client.app.state.execution_resolver.resolve("tenant-1")
    selected, other = RecordingAdapter(), RecordingAdapter()
    context.llm_adapters.update(deep=selected, fast=other)
    client.post(
        f"/threads/{thread.thread_id}/messages", headers=HEADERS, json={"content": "Review this"}
    )
    assert client.post(f"/threads/{thread.thread_id}/run", headers=HEADERS).status_code == 200
    assert selected.calls == 1
    assert other.calls == 0
    context.llm_adapters.pop("deep")
    response = client.post(f"/threads/{thread.thread_id}/run", headers=HEADERS)
    assert response.status_code == 400
    assert other.calls == 0


def test_profile_options_expose_only_public_model_metadata(client):
    profiles = client.get("/execution-options", headers=HEADERS).json()["llm_profiles"]
    assert profiles["effective_default"]["model"] == "fast-model"
    assert [(p["name"], p["provider"], p["model"]) for p in profiles["items"]] == [
        ("fast", "mock", "fast-model"),
        ("deep", "mock", "deep-model"),
    ]
    assert all("api_key" not in p and "headers" not in p for p in profiles["items"])
