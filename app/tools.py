from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from app.mcp import MCPHTTPClient, load_mcp_server_configs_from_env
from app.models import ToolSpec
import logging

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Callable[[dict[str, Any]], Any]]] = {}
        self._mcp_servers: list[dict[str, Any]] = []

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
    ) -> None:
        self._tools[name] = (
            ToolSpec(name=name, description=description, input_schema=input_schema),
            handler,
        )

    def specs(self) -> list[ToolSpec]:
        return [tool[0] for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise HTTPException(status_code=400, detail=f"Unknown tool '{name}'")
        return tool[1](arguments)

    def set_mcp_servers(self, servers: list[dict[str, Any]]) -> None:
        self._mcp_servers = servers

    def mcp_servers(self) -> list[dict[str, Any]]:
        return list(self._mcp_servers)


def build_local_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def echo_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        text = str(arguments.get("text", ""))
        return {"echo": text}

    registry.register(
        name="echo",
        description="Return the provided text. Useful for verifying tool invocation.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=echo_tool,
    )
    return registry


def build_tool_registry_from_env() -> ToolRegistry:
    registry = build_local_tool_registry()

    mcp_servers: list[dict[str, Any]] = []
    for config in load_mcp_server_configs_from_env():
        try:
            client = MCPHTTPClient(config)
            specs = client.list_tools()
            for spec in specs:
                raw_tool_name = spec.name.split(".", 1)[1]
                registry.register(
                    name=spec.name,
                    description=spec.description,
                    input_schema=spec.input_schema,
                    handler=lambda arguments, c=client, tool_name=raw_tool_name: c.call_tool(tool_name, arguments),
                )
            server_info = client.server_info()
            mcp_servers.append(
                {
                    "name": server_info.name,
                    "url": server_info.url,
                    "protocol_version": server_info.protocol_version,
                    "session": bool(server_info.session_id),
                    "server_name": server_info.server_name,
                    "server_version": server_info.server_version,
                    "tool_count": len(specs),
                }
            )
        except Exception as exc:
            logger.warning("Skipping MCP server '%s': %s", config.name, exc)
    registry.set_mcp_servers(mcp_servers)
    return registry
