from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.execution import FixedTenantExecutionResolver, parse_tenant_execution_config
from app.llm import LLMAdapter, LLMResponse
from app.main import create_app
from app.models import Message, MessageRole, ToolSpec
from app.store import InMemoryThreadStore, SQLiteThreadStore
from app.tools import build_local_tool_registry
from mindweft_client import cli
from mindweft_client.api_client import MindweftAPIClient
from mindweft_client.config import AgentPreset, ClientConfig, PrincipalConfig
from mindweft_client.state import ClientState as PersistentClientState


class RecordingAdapter(LLMAdapter):
    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    def describe(self) -> dict[str, Any]:
        return {"provider": "recording"}

    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.requests.append(messages)
        return LLMResponse(content="The project is named Violet.")


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("local_preset", [False, True])
def test_cli_switch_then_agent_preserves_history_in_next_model_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, local_preset: bool
) -> None:
    """Reproduce /switch <old> -> /agent quadx-office -> next prompt end to end."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = (
        InMemoryThreadStore()
        if store_kind == "memory"
        else SQLiteThreadStore(tmp_path / "threads.db")
    )
    source = store.create_thread("tenant-1")
    for role, content in [
        (MessageRole.USER, "Our project is named Violet."),
        (MessageRole.ASSISTANT, "I'll remember Violet."),
    ]:
        store.append_message(
            "tenant-1", Message(thread_id=source.thread_id, role=role, content=content)
        )
    # Include the actual tool-result tail, which the visible-message picker would omit.
    store.append_message(
        "tenant-1",
        Message(
            thread_id=source.thread_id,
            role=MessageRole.ASSISTANT,
            content="",
            tool_name="echo",
            tool_call_id="call-1",
            tool_arguments={"text": "Due Friday"},
        ),
    )
    last = store.append_message(
        "tenant-1",
        Message(
            thread_id=source.thread_id,
            role=MessageRole.TOOL,
            content="Due Friday",
            tool_name="echo",
            tool_call_id="call-1",
        ),
    )
    original = store.list_messages("tenant-1", source.thread_id)
    adapter = RecordingAdapter()
    execution_config = parse_tenant_execution_config(
        "tenant-1",
        {
            "llm": {"provider": "mock"},
            "skills": {
                "items": [{"name": "quadx-office", "system_prompt": "Use office workflows."}]
            },
            "capability_profiles": {
                "items": [{"name": "quadx-office", "allowed_local_tools": ["echo"]}]
            },
            "agents": {
                "items": [
                    {
                        "name": "quadx-office",
                        "skill_name": "quadx-office",
                        "capability_profile": "quadx-office",
                    }
                ]
            },
        },
    )
    app = create_app(
        thread_store=store,
        execution_resolver=FixedTenantExecutionResolver(
            adapter, build_local_tool_registry(), config=execution_config
        ),
    )
    config = ClientConfig(
        base_url="http://testserver",
        wake_phrase="hey mindweft",
        principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        agent_presets=(
            AgentPreset(
                name="quadx-office", skill_name="quadx-office", capability_profile="quadx-office"
            ),
        )
        if local_preset
        else (),
    )
    calls: list[tuple[str, str, Any]] = []
    with TestClient(app) as http:

        def request_json(self, method, url, *, payload=None):
            path = urlsplit(url).path
            calls.append((method, path, payload))
            response = http.request(
                method, path, json=payload, headers=config.principal.build_headers()
            )
            if response.status_code >= 400:
                raise RuntimeError(response.text)
            return response.json()

        monkeypatch.setattr(MindweftAPIClient, "request_json", request_json)
        output = StringIO()
        monkeypatch.setattr(
            cli.sys,
            "stdin",
            StringIO(
                f"/switch {source.thread_id}\n/agent quadx-office\nWhat is the project name?\n/exit\n"
            ),
        )
        monkeypatch.setattr(cli.sys, "stdout", output)
        assert cli.run_chat_loop(config) == 0
    assert "conversation history preserved" in output.getvalue()
    assert not any(method == "POST" and path == "/threads" for method, path, _ in calls)
    fork_payload = next(payload for method, path, payload in calls if path.endswith("/fork"))
    assert fork_payload["at_message_id"] == last.id
    if local_preset:
        assert fork_payload["skill_name"] == "quadx-office"
    else:
        assert fork_payload["agent_name"] == "shared:quadx-office"
    # Check the model request, not just copied rows or chat transcript visibility.
    request = next(
        messages
        for messages in adapter.requests
        if any("What is the project name?" in message.content for message in messages)
    )
    assert any(message.content == "Our project is named Violet." for message in request)
    assert any(message.content == "I'll remember Violet." for message in request)
    assert any(message.content == "Due Friday" for message in request)
    assert any(
        message.role == MessageRole.SYSTEM and "Use office workflows." in message.content
        for message in request
    )
    assert not any(message.content.startswith("/agent") for message in request)
    assert store.list_messages("tenant-1", source.thread_id) == original
    child_id = PersistentClientState.load().get_last_thread(cli.client_state_scope_key(config))
    assert child_id is not None and child_id != source.thread_id
    child = store.get_thread("tenant-1", child_id)
    assert child.parent_thread_id == source.thread_id
    assert child.fork_message_id == last.id
    assert child.skill_name == "quadx-office"
    assert child.agent_ref == (None if local_preset else "shared:quadx-office")


@pytest.mark.parametrize("failure", ["history", "fork", "invalid-tail", "missing-child-id"])
def test_cli_agent_switch_failure_never_falls_back_to_empty_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = ClientConfig(
        base_url="http://testserver",
        wake_phrase="hey mindweft",
        thread_id="source",
        agent_presets=(AgentPreset(name="office", skill_name="office"),),
    )
    calls = []

    def request_json(self, method, url, *, payload=None):
        calls.append((method, url, payload))
        if url.endswith("/execution-options"):
            return {}
        if url.endswith("/messages"):
            if failure == "history":
                raise RuntimeError("Cannot read history")
            return [{"role": "user", **({} if failure == "invalid-tail" else {"id": "message-1"})}]
        if url.endswith("/fork"):
            if failure == "missing-child-id":
                return {}
            raise RuntimeError("Cannot fork this thread")
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(MindweftAPIClient, "request_json", request_json)
    client = cli.RememberingMindweftAPIClient(MindweftAPIClient(config), config)
    cli.remember_client_thread(config, "source")
    output = StringIO()
    cli._handle_chat_agent("/agent office", client, config, output)
    assert "agent switch failed" in output.getvalue()
    assert client.thread_id == "source"
    assert client.active_agent_preset is None
    assert (
        PersistentClientState.load().get_last_thread(cli.client_state_scope_key(config)) == "source"
    )
    assert not any(method == "POST" and url.endswith("/threads") for method, url, _ in calls)


def test_cli_scoped_agents_disambiguate_shared_and_personal_presets() -> None:
    shared = AgentPreset(name="office", agent_ref="shared:office")
    personal = AgentPreset(name="office", agent_ref="user:office")
    merged = cli._merged_agent_presets((), (shared, personal))
    assert len(merged) == 2
    assert cli._find_agent_preset(merged, "office") is None
    assert cli._find_agent_preset(merged, "user:office") == personal
    assert cli._agent_preset_selection(personal) == {"agent_name": "user:office"}
    local = AgentPreset(name="office", skills=())
    merged = cli._merged_agent_presets((local,), merged)
    assert cli._find_agent_preset(merged, "office") == local
    assert cli._agent_preset_selection(local)["skills"] == []


@pytest.mark.parametrize("key", ["llm_profile", "llmProfile"])
def test_local_preset_preserves_model_profile(key: str) -> None:
    from mindweft_client.config import parse_agent_presets

    preset = parse_agent_presets([{"name": "office", key: "primary"}])[0]
    assert preset.llm_profile == "primary"
    assert cli._agent_preset_selection(preset)["llm_profile"] == "primary"
