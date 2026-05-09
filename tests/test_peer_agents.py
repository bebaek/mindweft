import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from app.peer_agents import (
    PEER_AGENTS_ENV,
    PeerAgentRegistry,
    load_peer_agent_configs_from_env,
    parse_peer_agent_configs,
)


def test_parse_peer_agent_configs_accepts_valid_entries() -> None:
    configs = parse_peer_agent_configs(
        [
            {
                "name": "codex",
                "base_url": "http://127.0.0.1:8010/",
                "description": "Local Codex wrapper",
            }
        ]
    )

    assert len(configs) == 1
    assert configs[0].name == "codex"
    assert configs[0].base_url == "http://127.0.0.1:8010"
    assert configs[0].description == "Local Codex wrapper"


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


def test_peer_agent_registry_returns_404_for_unknown_agent() -> None:
    registry = PeerAgentRegistry([])

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(registry.agent_card("missing"))

    assert exc_info.value.status_code == 404
