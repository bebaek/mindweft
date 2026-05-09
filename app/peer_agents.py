from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException

PEER_AGENTS_ENV = "MINIGENT_PEER_AGENTS"


@dataclass(frozen=True)
class PeerAgentConfig:
    name: str
    base_url: str
    description: str | None = None

    def public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "base_url": self.base_url,
            "links": {
                "agent_card": f"/peer-agents/{self.name}/agent-card",
                "tasks": f"/peer-agents/{self.name}/tasks",
            },
        }
        if self.description is not None:
            payload["description"] = self.description
        return payload


class PeerAgentRegistry:
    def __init__(
        self,
        agents: list[PeerAgentConfig],
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._agents = {agent.name: agent for agent in agents}
        self._timeout = timeout
        self._transport = transport

    def list_agents(self) -> list[dict[str, object]]:
        return [self._agents[name].public_dict() for name in sorted(self._agents)]

    async def agent_card(self, name: str) -> dict[str, Any]:
        return await self._request_json(name, "GET", "/agent-card", response_label="agent card")

    async def create_task(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json(
            name,
            "POST",
            "/tasks",
            json_body=payload,
            response_label="task response",
        )

    async def task(self, name: str, task_id: str) -> dict[str, Any]:
        return await self._request_json(
            name,
            "GET",
            f"/tasks/{task_id}",
            response_label="task response",
        )

    async def _request_json(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        response_label: str,
    ) -> dict[str, Any]:
        agent = self._agent_or_404(name)
        url = f"{agent.base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(method, url, json=json_body)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Peer agent '{name}' {path} request failed with status "
                    f"{exc.response.status_code}"
                ),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Peer agent '{name}' {path} request failed: {exc}",
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Peer agent '{name}' returned invalid JSON {response_label}",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=502,
                detail=f"Peer agent '{name}' returned non-object {response_label}",
            )
        return payload

    def _agent_or_404(self, name: str) -> PeerAgentConfig:
        agent = self._agents.get(name)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Peer agent '{name}' not found")
        return agent


def load_peer_agent_configs_from_env() -> list[PeerAgentConfig]:
    raw = os.getenv(PEER_AGENTS_ENV, "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{PEER_AGENTS_ENV} must be valid JSON") from exc
    return parse_peer_agent_configs(payload)


def parse_peer_agent_configs(payload: object) -> list[PeerAgentConfig]:
    if not isinstance(payload, list):
        raise RuntimeError(f"{PEER_AGENTS_ENV} must be a JSON array")

    configs: list[PeerAgentConfig] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError(f"{PEER_AGENTS_ENV} entries must be JSON objects")
        name = str(item.get("name", "")).strip()
        base_url = str(item.get("base_url", "")).strip().rstrip("/")
        description_value = item.get("description")
        description = (
            str(description_value).strip()
            if description_value is not None and str(description_value).strip()
            else None
        )
        if not name:
            raise RuntimeError(f"{PEER_AGENTS_ENV} entries require a non-empty name")
        if name in seen:
            raise RuntimeError(f"{PEER_AGENTS_ENV} contains duplicate agent name '{name}'")
        if not base_url:
            raise RuntimeError(f"{PEER_AGENTS_ENV} entry '{name}' requires base_url")
        configs.append(PeerAgentConfig(name=name, base_url=base_url, description=description))
        seen.add(name)
    return configs


def build_peer_agent_registry_from_env() -> PeerAgentRegistry:
    return PeerAgentRegistry(load_peer_agent_configs_from_env())
