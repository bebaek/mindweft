from __future__ import annotations

import asyncio
import ast
import concurrent.futures
import inspect
import logging
import operator
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from app.mcp import MCPHTTPClient, load_mcp_server_configs_from_env
from app.models import ToolSpec
from app.redaction import sanitize_value_for_logging

logger = logging.getLogger(__name__)

_CALCULATOR_BINARY_OPERATORS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CALCULATOR_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


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

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise HTTPException(status_code=400, detail=f"Unknown tool '{name}'")
        sanitized_arguments = _sanitize_tool_arguments(arguments)
        started_at = time.perf_counter()
        logger.info("tool.start name=%s arguments=%s", name, sanitized_arguments)
        try:
            result = tool[1](arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "tool.error name=%s duration_ms=%s error_type=%s detail=%s",
                name,
                duration_ms,
                type(exc).__name__,
                _tool_error_detail(exc),
            )
            raise
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.info("tool.ok name=%s duration_ms=%s", name, duration_ms)
        return result

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

    def current_time_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        _ = arguments
        return {"current_time": datetime.now(timezone.utc).isoformat()}

    registry.register(
        name="current_time",
        description="Return the current UTC time in ISO 8601 format.",
        input_schema={"type": "object", "properties": {}},
        handler=current_time_tool,
    )

    async def fetch_url_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        url = str(arguments.get("url", "")).strip()
        timeout = float(arguments.get("timeout_seconds", 10.0))
        if not url:
            raise HTTPException(status_code=400, detail="fetch_url requires a url")
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"fetch_url failed with status {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"fetch_url request failed: {exc}") from exc
        content_type = response.headers.get("content-type", "")
        return {
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "text": response.text,
        }

    registry.register(
        name="fetch_url",
        description="Fetch a URL and return its response text with basic metadata.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 0.1},
            },
            "required": ["url"],
        },
        handler=fetch_url_tool,
    )

    async def sleep_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        seconds = float(arguments.get("seconds", 0))
        if seconds < 0:
            raise HTTPException(status_code=400, detail="sleep requires seconds >= 0")
        await asyncio.sleep(seconds)
        return {"slept_seconds": seconds}

    registry.register(
        name="sleep",
        description="Pause execution for the requested number of seconds.",
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": "number", "minimum": 0}},
            "required": ["seconds"],
        },
        handler=sleep_tool,
    )

    def calculator_tool(arguments: dict[str, Any]) -> dict[str, Any]:
        expression = str(arguments.get("expression", "")).strip()
        if not expression:
            raise HTTPException(status_code=400, detail="calculator requires an expression")
        return {"expression": expression, "result": _evaluate_calculator_expression(expression)}

    registry.register(
        name="calculator",
        description="Evaluate a basic arithmetic expression safely.",
        input_schema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        handler=calculator_tool,
    )

    return registry


def build_tool_registry_from_env() -> ToolRegistry:
    registry = build_local_tool_registry()

    mcp_servers: list[dict[str, Any]] = []
    for config in load_mcp_server_configs_from_env():
        try:
            client = MCPHTTPClient(config)
            specs = _run_awaitable_sync(client.list_tools())
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


def _evaluate_calculator_expression(expression: str) -> float | int:
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise HTTPException(status_code=400, detail="calculator expression is invalid") from exc
    return _evaluate_calculator_node(parsed.body)


def _evaluate_calculator_node(node: ast.AST) -> float | int:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.BinOp):
        operator_fn = _CALCULATOR_BINARY_OPERATORS.get(type(node.op))
        if operator_fn is None:
            raise HTTPException(status_code=400, detail="calculator operator is not supported")
        left = _evaluate_calculator_node(node.left)
        right = _evaluate_calculator_node(node.right)
        try:
            return operator_fn(left, right)
        except ZeroDivisionError as exc:
            raise HTTPException(status_code=400, detail="calculator division by zero") from exc
    if isinstance(node, ast.UnaryOp):
        operator_fn = _CALCULATOR_UNARY_OPERATORS.get(type(node.op))
        if operator_fn is None:
            raise HTTPException(status_code=400, detail="calculator operator is not supported")
        return operator_fn(_evaluate_calculator_node(node.operand))
    raise HTTPException(status_code=400, detail="calculator expression contains unsupported syntax")


def _sanitize_tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: sanitize_value_for_logging(key, value) for key, value in arguments.items()}


def _tool_error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def _run_awaitable_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()
