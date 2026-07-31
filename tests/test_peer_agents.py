import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from app.peer_agents import (
    PEER_AGENTS_ENV,
    PeerAgentRegistry,
    PeerAgentSettings,
    load_peer_agent_configs_from_env,
    parse_peer_agent_configs,
    peer_agent_settings_from_env,
)


def test_peer_agent_settings_from_env_mapping_defaults_to_empty() -> None:
    assert PeerAgentSettings.from_env({}) == PeerAgentSettings(agents=[])


def test_peer_agent_settings_from_env_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        PEER_AGENTS_ENV,
        json.dumps([{"name": "codex", "base_url": "http://127.0.0.1:8010"}]),
    )

    assert peer_agent_settings_from_env().agents[0].name == "codex"


def test_parse_peer_agent_configs_accepts_valid_entries() -> None:
    configs = parse_peer_agent_configs(
        [
            {
                "name": "codex",
                "base_url": "http://127.0.0.1:8010/",
                "description": "Local coding-agent wrapper",
                "capabilities": ["codebase inspection"],
                "side_effects": ["runs local commands"],
                "version": "0.1.0",
            }
        ]
    )

    assert len(configs) == 1
    assert configs[0].name == "codex"
    assert configs[0].base_url == "http://127.0.0.1:8010"
    assert configs[0].description == "Local coding-agent wrapper"
    assert configs[0].capabilities == ("codebase inspection",)
    assert configs[0].side_effects == ("runs local commands",)
    assert configs[0].version == "0.1.0"


def test_load_peer_agent_configs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        PEER_AGENTS_ENV,
        json.dumps([{"name": "codex", "base_url": "http://127.0.0.1:8010"}]),
    )

    configs = load_peer_agent_configs_from_env()

    assert configs[0].name == "codex"


def test_parse_peer_agent_configs_rejects_duplicates() -> None:
    with pytest.raises(RuntimeError, match="duplicate agent name 'codex'"):
        parse_peer_agent_configs(
            [
                {"name": "codex", "base_url": "http://127.0.0.1:8010"},
                {"name": "codex", "base_url": "http://127.0.0.1:8011"},
            ]
        )


def test_peer_agent_registry_fetches_agent_card() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://codex-agent.test/agent-card"
        return httpx.Response(200, json={"name": "codex-coding-agent"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )

    card = asyncio.run(registry.agent_card("codex"))

    assert card == {"name": "codex-coding-agent"}


def test_peer_agent_registry_lists_agents_with_agent_card_summary() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://codex-agent.test/agent-card"
        return httpx.Response(
            200,
            json={
                "name": "codex-coding-agent",
                "description": "Codex CLI wrapper",
                "version": "0.1.0",
                "capabilities": ["repository analysis"],
                "side_effects": ["runs commands"],
            },
        )

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )

    agents = asyncio.run(registry.list_agents_with_cards())

    assert agents == [
        {
            "name": "codex",
            "base_url": "http://codex-agent.test",
            "links": {
                "agent_card": "/peer-agents/codex/agent-card",
                "tasks": "/peer-agents/codex/tasks",
            },
            "agent_card_name": "codex-coding-agent",
            "description": "Codex CLI wrapper",
            "version": "0.1.0",
            "capabilities": ["repository analysis"],
            "side_effects": ["runs commands"],
        }
    ]


def test_peer_agent_registry_lists_agent_card_errors_nonfatally() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://codex-agent.test/agent-card"
        return httpx.Response(503, json={"detail": "offline"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )

    agents = asyncio.run(registry.list_agents_with_cards())

    assert agents[0]["name"] == "codex"
    assert "agent_card_error" in agents[0]


def test_peer_agent_registry_creates_task() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://codex-agent.test/tasks"
        assert json.loads(request.content) == {
            "cwd": "/workspace/project",
            "prompt": "summarize this repo",
        }
        return httpx.Response(200, json={"task_id": "task_123", "status": "running"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        registry.create_task(
            "codex",
            {"cwd": "/workspace/project", "prompt": "summarize this repo"},
        )
    )

    assert response == {"task_id": "task_123", "status": "running"}


def test_peer_agent_registry_fetches_task_status() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://codex-agent.test/tasks/task_123"
        return httpx.Response(200, json={"task_id": "task_123", "status": "completed"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(registry.task("codex", "task_123"))

    assert response == {"task_id": "task_123", "status": "completed"}


def test_peer_agent_registry_cancels_task() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://codex-agent.test/tasks/task_123/cancel"
        return httpx.Response(200, json={"task_id": "task_123", "status": "canceled"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(registry.cancel_task("codex", "task_123"))

    assert response == {"task_id": "task_123", "status": "canceled"}


def test_peer_agent_registry_fetches_task_events_with_after() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://codex-agent.test/tasks/task_123/events?after=0"
        return httpx.Response(
            200,
            json={
                "task_id": "task_123",
                "next_index": 2,
                "events": [{"index": 1, "type": "message.completed"}],
            },
        )

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(registry.task_events("codex", "task_123", after=0))

    assert response == {
        "task_id": "task_123",
        "next_index": 2,
        "events": [{"index": 1, "type": "message.completed"}],
    }


def test_peer_agent_registry_fetches_task_artifact() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://codex-agent.test/tasks/task_123/artifacts/final-output"
        return httpx.Response(200, text="final text", headers={"content-type": "text/plain"})

    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )

    artifact = asyncio.run(registry.task_artifact("codex", "task_123", "final-output"))

    assert artifact.content == b"final text"
    assert artifact.media_type == "text/plain"


def test_peer_agent_registry_rejects_unknown_artifact_name() -> None:
    registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}])
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(registry.task_artifact("codex", "task_123", "unknown"))

    assert exc_info.value.status_code == 400


def test_peer_agent_registry_returns_404_for_unknown_agent() -> None:
    registry = PeerAgentRegistry([])

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(registry.agent_card("missing"))

    assert exc_info.value.status_code == 404


def test_peer_agent_registry_cancels_task_at_persisted_base_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://persisted-peer.test/tasks/task_123/cancel"
        return httpx.Response(200, json={"task_id": "task_123", "status": "canceled"})

    registry = PeerAgentRegistry([], transport=httpx.MockTransport(handler))

    response = asyncio.run(
        registry.cancel_task_at("removed-peer", "http://persisted-peer.test", "task_123")
    )

    assert response == {"task_id": "task_123", "status": "canceled"}


def test_peer_agent_registry_treats_missing_persisted_task_as_canceled() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "missing"})

    registry = PeerAgentRegistry([], transport=httpx.MockTransport(handler))

    response = asyncio.run(
        registry.cancel_task_at("removed-peer", "http://persisted-peer.test", "task_123")
    )

    assert response == {"status": "not_found_or_terminal"}
