from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException

from minigent_config.unified_config import normalize_mindweft_env

MINDWEFT_PEER_AGENTS_ENV = "MINDWEFT_PEER_AGENTS"
PEER_AGENTS_ENV = "MINIGENT_PEER_AGENTS"
PEER_AGENT_ARTIFACT_NAMES = frozenset(
    {
        "final-output",
        "stdout-tail",
        "stderr-tail",
        "events",
    }
)


@dataclass(frozen=True)
class PeerAgentConfig:
    name: str
    base_url: str
    description: str | None = None
    capabilities: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    version: str | None = None

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
        if self.capabilities:
            payload["capabilities"] = list(self.capabilities)
        if self.side_effects:
            payload["side_effects"] = list(self.side_effects)
        if self.version is not None:
            payload["version"] = self.version
        return payload


@dataclass(frozen=True)
class PeerAgentSettings:
    agents: list[PeerAgentConfig]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PeerAgentSettings:
        lookup = normalize_mindweft_env(dict(os.environ if env is None else env))
        return cls(agents=_parse_peer_agent_configs_env_value(lookup.get(PEER_AGENTS_ENV, "")))


@dataclass(frozen=True)
class PeerAgentArtifact:
    content: bytes
    media_type: str


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

    def agent_base_url(self, name: str) -> str:
        return self._agent_or_404(name).base_url

    async def list_agents_with_cards(self) -> list[dict[str, object]]:
        agents: list[dict[str, object]] = []
        for name in sorted(self._agents):
            payload = self._agents[name].public_dict()
            try:
                card = await self.agent_card(name)
            except HTTPException as exc:
                payload["agent_card_error"] = str(exc.detail)
            else:
                _merge_agent_card_summary(payload, card)
            agents.append(payload)
        return agents

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

    async def cancel_task(self, name: str, task_id: str) -> dict[str, Any]:
        return await self._request_json(
            name,
            "POST",
            f"/tasks/{task_id}/cancel",
            response_label="task response",
        )

    async def cancel_task_at(self, name: str, base_url: str, task_id: str) -> dict[str, Any]:
        path = f"/tasks/{task_id}/cancel"
        url = f"{base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(url)
                if response.status_code in {404, 409, 410}:
                    return {"status": "not_found_or_terminal"}
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
        except ValueError:
            return {"status": "canceled"}
        return payload if isinstance(payload, dict) else {"status": "canceled"}

    async def task_events(
        self,
        name: str,
        task_id: str,
        *,
        after: int | None = None,
    ) -> dict[str, Any]:
        params = {"after": after} if after is not None else None
        return await self._request_json(
            name,
            "GET",
            f"/tasks/{task_id}/events",
            query_params=params,
            response_label="task events response",
        )

    async def task_artifact(
        self,
        name: str,
        task_id: str,
        artifact_name: str,
    ) -> PeerAgentArtifact:
        if artifact_name not in PEER_AGENT_ARTIFACT_NAMES:
            allowed = ", ".join(sorted(PEER_AGENT_ARTIFACT_NAMES))
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported peer task artifact '{artifact_name}'. Allowed: {allowed}",
            )
        response = await self._request(
            name,
            "GET",
            f"/tasks/{task_id}/artifacts/{artifact_name}",
        )
        media_type = response.headers.get("content-type", "application/octet-stream")
        return PeerAgentArtifact(content=response.content, media_type=media_type)

    async def _request_json(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        response_label: str,
    ) -> dict[str, Any]:
        response = await self._request(
            name,
            method,
            path,
            json_body=json_body,
            query_params=query_params,
        )
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

    async def _request(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        agent = self._agent_or_404(name)
        url = f"{agent.base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    json=json_body,
                    params=query_params,
                )
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

        return response

    def _agent_or_404(self, name: str) -> PeerAgentConfig:
        agent = self._agents.get(name)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"Peer agent '{name}' not found")
        return agent


def load_peer_agent_configs_from_env() -> list[PeerAgentConfig]:
    return PeerAgentSettings.from_env().agents


def peer_agent_settings_from_env() -> PeerAgentSettings:
    return PeerAgentSettings.from_env()


def _parse_peer_agent_configs_env_value(raw_value: str) -> list[PeerAgentConfig]:
    raw = raw_value.strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{MINDWEFT_PEER_AGENTS_ENV} must be valid JSON") from exc
    return parse_peer_agent_configs(payload)


def parse_peer_agent_configs(payload: object) -> list[PeerAgentConfig]:
    if not isinstance(payload, list):
        raise RuntimeError(f"{MINDWEFT_PEER_AGENTS_ENV} must be a JSON array")

    configs: list[PeerAgentConfig] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError(f"{MINDWEFT_PEER_AGENTS_ENV} entries must be JSON objects")
        name = str(item.get("name", "")).strip()
        base_url = str(item.get("base_url", "")).strip().rstrip("/")
        description_value = item.get("description")
        description = (
            str(description_value).strip()
            if description_value is not None and str(description_value).strip()
            else None
        )
        capabilities = _optional_string_tuple(
            item.get("capabilities"), f"agent '{name}' capabilities"
        )
        side_effects = _optional_string_tuple(
            item.get("side_effects"), f"agent '{name}' side_effects"
        )
        version_value = item.get("version")
        version = (
            str(version_value).strip()
            if version_value is not None and str(version_value).strip()
            else None
        )
        if not name:
            raise RuntimeError(f"{MINDWEFT_PEER_AGENTS_ENV} entries require a non-empty name")
        if name in seen:
            raise RuntimeError(f"{MINDWEFT_PEER_AGENTS_ENV} contains duplicate agent name '{name}'")
        if not base_url:
            raise RuntimeError(f"{MINDWEFT_PEER_AGENTS_ENV} entry '{name}' requires base_url")
        configs.append(
            PeerAgentConfig(
                name=name,
                base_url=base_url,
                description=description,
                capabilities=capabilities,
                side_effects=side_effects,
                version=version,
            )
        )
        seen.add(name)
    return configs


def build_peer_agent_registry(settings: PeerAgentSettings) -> PeerAgentRegistry:
    return PeerAgentRegistry(settings.agents)


def build_peer_agent_registry_from_env() -> PeerAgentRegistry:
    return build_peer_agent_registry(peer_agent_settings_from_env())


def _optional_string_tuple(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"{MINDWEFT_PEER_AGENTS_ENV} {label} must be an array of strings")
    return tuple(item.strip() for item in value if item.strip())


def _merge_agent_card_summary(payload: dict[str, object], card: dict[str, Any]) -> None:
    name = card.get("name")
    if isinstance(name, str) and name.strip():
        payload["agent_card_name"] = name.strip()
    description = card.get("description")
    if "description" not in payload and isinstance(description, str) and description.strip():
        payload["description"] = description.strip()
    version = card.get("version")
    if "version" not in payload and isinstance(version, str) and version.strip():
        payload["version"] = version.strip()
    capabilities = card.get("capabilities")
    if "capabilities" not in payload and isinstance(capabilities, list):
        payload["capabilities"] = [str(item) for item in capabilities if str(item).strip()]
    side_effects = card.get("side_effects")
    if "side_effects" not in payload and isinstance(side_effects, list):
        payload["side_effects"] = [str(item) for item in side_effects if str(item).strip()]
