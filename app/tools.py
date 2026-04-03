from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from app.models import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Callable[[dict[str, Any]], Any]]] = {}

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


def build_default_tool_registry() -> ToolRegistry:
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
