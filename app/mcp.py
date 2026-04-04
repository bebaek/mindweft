from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException

from app.models import ToolSpec

logger = logging.getLogger(__name__)

DEFAULT_MCP_PROTOCOL_VERSION = "2025-11-25"


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    url: str
    headers: dict[str, str]
    protocol_version: str = DEFAULT_MCP_PROTOCOL_VERSION


@dataclass(frozen=True)
class MCPServerInfo:
    name: str
    url: str
    protocol_version: str
    session_id: str | None
    server_name: str | None
    server_version: str | None


class MCPHTTPClient:
    def __init__(
        self,
        config: MCPServerConfig,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._config = config
        self._transport = transport
        self._timeout = timeout
        self._session_id: str | None = None
        self._negotiated_protocol_version: str = config.protocol_version
        self._initialized = False
        self._request_id = 0
        self._server_name: str | None = None
        self._server_version: str | None = None

    def server_info(self) -> MCPServerInfo:
        return MCPServerInfo(
            name=self._config.name,
            url=self._config.url,
            protocol_version=self._negotiated_protocol_version,
            session_id=self._session_id,
            server_name=self._server_name,
            server_version=self._server_version,
        )

    def list_tools(self) -> list[ToolSpec]:
        self._ensure_initialized()
        tools: list[ToolSpec] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params or None)
            for tool in result.get("tools", []):
                namespaced_name = f"{self._config.name}.{tool['name']}"
                description = tool.get("description") or f"MCP tool {tool['name']} from {self._config.name}"
                tools.append(
                    ToolSpec(
                        name=namespaced_name,
                        description=description,
                        input_schema=tool.get("inputSchema") or {"type": "object"},
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        self._ensure_initialized()
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        if result.get("isError"):
            raise HTTPException(
                status_code=502,
                detail=f"MCP tool '{self._config.name}.{tool_name}' returned an error: {json.dumps(result, ensure_ascii=True)}",
            )
        if "structuredContent" in result:
            return result["structuredContent"]
        if "content" in result:
            return {"content": result["content"]}
        return result

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        result, headers = self._request_raw(
            "initialize",
            {
                "protocolVersion": self._config.protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": "minigent",
                    "version": "0.1.0",
                },
            },
            use_protocol_header=False,
        )
        self._session_id = headers.get("MCP-Session-Id")
        self._negotiated_protocol_version = result.get("protocolVersion", self._config.protocol_version)
        server_info = result.get("serverInfo") or {}
        self._server_name = server_info.get("name")
        self._server_version = server_info.get("version")
        self._notify("notifications/initialized")
        self._initialized = True
        logger.info(
            "MCP initialized: server=%s url=%s protocol=%s session=%s remote=%s@%s",
            self._config.name,
            self._config.url,
            self._negotiated_protocol_version,
            bool(self._session_id),
            self._server_name,
            self._server_version,
        )

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        headers = self._build_headers(include_protocol=True)
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            response = client.post(
                self._config.url,
                json=payload,
                headers=headers,
            )
        if response.status_code not in (200, 202):
            raise HTTPException(
                status_code=502,
                detail=f"MCP notification failed for server '{self._config.name}': {response.text}",
            )

    def _request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        result, _ = self._request_raw(method, params, use_protocol_header=True)
        return result

    def _request_raw(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        use_protocol_header: bool,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        self._request_id += 1
        headers = self._build_headers(include_protocol=use_protocol_header)
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = client.post(
                    self._config.url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"MCP server '{self._config.name}' HTTP error: {exc.response.text}",
                ) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"MCP server '{self._config.name}' request failed: {exc}",
                ) from exc

        if "application/json" not in response.headers.get("content-type", ""):
            raise HTTPException(
                status_code=502,
                detail=f"MCP server '{self._config.name}' returned unsupported content type '{response.headers.get('content-type')}'",
            )
        body = response.json()
        if "error" in body:
            raise HTTPException(
                status_code=502,
                detail=f"MCP server '{self._config.name}' protocol error: {json.dumps(body['error'], ensure_ascii=True)}",
            )
        result = body.get("result")
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=502,
                detail=f"MCP server '{self._config.name}' returned invalid JSON-RPC result",
            )
        return result, response.headers

    def _build_headers(self, *, include_protocol: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self._config.headers,
        }
        if include_protocol:
            headers["MCP-Protocol-Version"] = self._negotiated_protocol_version
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        return headers


def load_mcp_server_configs_from_env() -> list[MCPServerConfig]:
    raw = os.getenv("MINIGENT_MCP_SERVERS", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MINIGENT_MCP_SERVERS must be valid JSON") from exc
    if not isinstance(parsed, list):
        raise RuntimeError("MINIGENT_MCP_SERVERS must be a JSON array")

    configs: list[MCPServerConfig] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            raise RuntimeError("Each MINIGENT_MCP_SERVERS entry must be an object")
        name = entry.get("name")
        url = entry.get("url")
        headers = entry.get("headers") or {}
        protocol_version = entry.get("protocolVersion") or DEFAULT_MCP_PROTOCOL_VERSION
        if not isinstance(name, str) or not name:
            raise RuntimeError("Each MINIGENT_MCP_SERVERS entry must include a non-empty 'name'")
        if not isinstance(url, str) or not url:
            raise RuntimeError("Each MINIGENT_MCP_SERVERS entry must include a non-empty 'url'")
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            raise RuntimeError(f"MCP server '{name}' has invalid headers")
        configs.append(
            MCPServerConfig(
                name=name,
                url=url,
                headers=headers,
                protocol_version=str(protocol_version),
            )
        )
    return configs
