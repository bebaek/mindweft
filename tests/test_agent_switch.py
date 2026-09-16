import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Message, MessageRole, ThreadExecutionSelection
from app.store import InMemoryThreadStore, SQLiteThreadStore

HEADERS = {"X-Minigent-User-Id": "user-1", "X-Minigent-Tenant-Id": "tenant-1"}


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    return (
        InMemoryThreadStore()
        if request.param == "memory"
        else SQLiteThreadStore(tmp_path / "threads.db")
    )


@pytest.fixture
def client(store, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
        json.dumps(
            {
                "tenant-1": {
                    "llm": {"provider": "mock"},
                    "llm_profiles": {"alternate": {"provider": "mock"}},
                    "skills": {"items": [{"name": "review", "system_prompt": "Review carefully."}]},
                    "capability_profiles": {
                        "items": [{"name": "restricted", "allowed_local_tools": []}]
                    },
                    "agents": {
                        "items": [
                            {
                                "name": "reviewer",
                                "skills": ["review"],
                                "capability_profile": "restricted",
                                "llm_profile": "alternate",
                            },
                            {"name": "plain", "skills": []},
                        ]
                    },
                }
            }
        ),
    )
    with TestClient(create_app(thread_store=store)) as client:
        yield client


def source_thread(store):
    source = store.create_thread(
        "tenant-1",
        execution_user_id="original-user",
        agent_ref="shared:old",
        skill_names=["old"],
        capability_profile="old",
        llm_profile="old",
    )
    message = store.append_message(
        "tenant-1",
        Message(thread_id=source.thread_id, role=MessageRole.USER, content="Keep this context"),
    )
    return store.get_thread("tenant-1", source.thread_id), message


def test_agent_fork_resolves_settings_and_preserves_source(client, store):
    source, message = source_thread(store)
    response = client.post(
        f"/threads/{source.thread_id}/fork",
        json={"at_message_id": message.id, "agent_name": "shared:reviewer"},
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    child_id = response.json()["thread_id"]
    child = store.get_thread("tenant-1", child_id)
    assert child.agent_ref == "shared:reviewer"
    assert child.execution_user_id == "user-1"
    assert child.skill_names == ["review"]
    assert child.capability_profile == "restricted"
    assert child.llm_profile == "alternate"
    assert child.parent_thread_id == source.thread_id
    assert store.get_thread("tenant-1", source.thread_id) == source
    assert [m.content for m in store.list_messages("tenant-1", child_id)] == [message.content]
    assert (
        client.get(f"/threads/{child_id}/lineage", headers=HEADERS).json()["thread"]["agent_ref"]
        == "shared:reviewer"
    )
    if isinstance(store, SQLiteThreadStore):
        # Re-open through the same database path to check JSON payload persistence.
        reopened = SQLiteThreadStore(store._db_path)
        assert reopened.get_thread("tenant-1", child_id).agent_ref == "shared:reviewer"


def test_agent_fork_clears_source_settings_and_ordinary_fork_inherits(client, store):
    source, message = source_thread(store)
    for extra in ({}, {"agent_name": "plain"}):
        response = client.post(
            f"/threads/{source.thread_id}/fork",
            json={"at_message_id": message.id, **extra},
            headers=HEADERS,
        )
        assert response.status_code == 201, response.text
        child = store.get_thread("tenant-1", response.json()["thread_id"])
        if extra:
            assert child.agent_ref == "shared:plain"
            assert child.skill_name is None
            assert child.skill_names is None
            assert child.capability_profile is None
            assert child.llm_profile is None
        else:
            assert child.agent_ref == source.agent_ref
            assert child.skill_names == source.skill_names
            assert child.execution_user_id == source.execution_user_id


@pytest.mark.parametrize("agent", ["missing", "user:someone-elses-agent", ""])
def test_invalid_agent_leaves_no_child(client, store, agent):
    source, message = source_thread(store)
    response = client.post(
        f"/threads/{source.thread_id}/fork",
        json={"at_message_id": message.id, "agent_name": agent},
        headers=HEADERS,
    )
    assert response.status_code in (400, 422)
    assert len(client.get("/threads", headers=HEADERS).json()["threads"]) == 1


def test_running_thread_cannot_switch(client, store):
    source, message = source_thread(store)
    store.start_run("tenant-1", source.thread_id)
    response = client.post(
        f"/threads/{source.thread_id}/fork",
        json={"at_message_id": message.id, "agent_name": "plain"},
        headers=HEADERS,
    )
    assert response.status_code == 409

    with pytest.raises(HTTPException) as exc:
        store.fork_thread(
            "tenant-1",
            source.thread_id,
            at_message_id=message.id,
            execution_selection=ThreadExecutionSelection(agent_ref="shared:plain"),
        )
    assert exc.value.status_code == 409


def test_creation_records_canonical_agent_ref(client, store):
    response = client.post("/threads", json={"agent_name": "reviewer"}, headers=HEADERS)
    assert response.status_code == 200
    assert store.get_thread("tenant-1", response.json()["thread_id"]).agent_ref == "shared:reviewer"


@pytest.mark.parametrize(
    "modality,mime",
    [("image", "image/png"), ("audio", "audio/wav"), ("document", "application/pdf")],
)
def test_incompatible_history_is_rejected_before_copying(client, store, modality, mime):
    source, _ = source_thread(store)
    message = store.append_message(
        "tenant-1",
        Message(
            thread_id=source.thread_id,
            role=MessageRole.USER,
            content="attachment",
            parts=[
                {
                    "type": modality,
                    "mime_type": mime,
                    "attachment_id": "not-copied",
                    **({"filename": "test"} if modality != "image" else {}),
                }
            ],
        ),
    )
    response = client.post(
        f"/threads/{source.thread_id}/fork",
        json={"at_message_id": message.id, "agent_name": "plain"},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert (
        response.json()["detail"] == f"Target agent cannot accept {modality} history in this thread"
    )
    assert len(client.get("/threads", headers=HEADERS).json()["threads"]) == 1


def test_agent_switch_does_not_cross_tenants(client, store):
    source = store.create_thread("tenant-2")
    message = store.append_message(
        "tenant-2", Message(thread_id=source.thread_id, role=MessageRole.USER, content="private")
    )
    response = client.post(
        f"/threads/{source.thread_id}/fork",
        json={"at_message_id": message.id, "agent_name": "reviewer"},
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert client.get("/threads", headers=HEADERS).json()["threads"] == []
