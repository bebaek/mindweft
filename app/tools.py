from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import importlib
import inspect
import logging
import operator
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from app.mcp import MCPHTTPClient, MCPServerConfig, load_mcp_server_configs_from_env
from app.models import ToolSpec
from app.redaction import sanitize_value_for_logging

logger = logging.getLogger(__name__)

MINIGENT_MINIRAG_DB_PATH_ENV = "MINIGENT_MINIRAG_DB_PATH"
MINIGENT_MINIRAG_BACKEND_ENV = "MINIGENT_MINIRAG_BACKEND"
MINIGENT_MINIRAG_EMBEDDING_PROVIDER_ENV = "MINIGENT_MINIRAG_EMBEDDING_PROVIDER"
MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT_ENV = "MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT"
MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT_ENV = "MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT"
LOCAL_TOOL_NAMES = {
    "echo",
    "current_time",
    "fetch_url",
    "sleep",
    "calculator",
    "retrieve_knowledge",
}

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


@dataclass(frozen=True)
class ToolExecutionContext:
    tenant_id: str | None = None
    thread_id: str | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[
            str,
            tuple[
                ToolSpec,
                Callable[[dict[str, Any], ToolExecutionContext | None], Any],
            ],
        ] = {}
        self._mcp_servers: list[dict[str, Any]] = []

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any], ToolExecutionContext | None], Any],
    ) -> None:
        self._tools[name] = (
            ToolSpec(name=name, description=description, input_schema=input_schema),
            handler,
        )

    def specs(self) -> list[ToolSpec]:
        return [tool[0] for tool in self._tools.values()]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise HTTPException(status_code=400, detail=f"Unknown tool '{name}'")
        sanitized_arguments = _sanitize_tool_arguments(arguments)
        started_at = time.perf_counter()
        logger.info("tool.start name=%s arguments=%s", name, sanitized_arguments)
        try:
            result = tool[1](arguments, context)
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


def build_local_tool_registry(allowed_tools: list[str] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    allowed_tool_set = set(allowed_tools) if allowed_tools is not None else None

    def register_local_tool(
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any], ToolExecutionContext | None], Any],
    ) -> None:
        if allowed_tool_set is not None and name not in allowed_tool_set:
            return
        registry.register(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )

    def echo_tool(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, Any]:
        _ = context
        text = str(arguments.get("text", ""))
        return {"echo": text}

    register_local_tool(
        name="echo",
        description="Return the provided text. Useful for verifying tool invocation.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=echo_tool,
    )

    def current_time_tool(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, Any]:
        _ = arguments, context
        return {"current_time": datetime.now(timezone.utc).isoformat()}

    register_local_tool(
        name="current_time",
        description="Return the current UTC time in ISO 8601 format.",
        input_schema={"type": "object", "properties": {}},
        handler=current_time_tool,
    )

    async def fetch_url_tool(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, Any]:
        _ = context
        url = str(arguments.get("url", "")).strip()
        timeout = float(arguments.get("timeout_seconds", 10.0))
        if not url:
            raise HTTPException(status_code=400, detail="fetch_url requires a url")
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502, detail=f"fetch_url failed with status {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"fetch_url request failed: {exc}") from exc
        content_type = response.headers.get("content-type", "")
        return {
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "text": response.text,
        }

    register_local_tool(
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

    async def sleep_tool(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, Any]:
        _ = context
        seconds = float(arguments.get("seconds", 0))
        if seconds < 0:
            raise HTTPException(status_code=400, detail="sleep requires seconds >= 0")
        await asyncio.sleep(seconds)
        return {"slept_seconds": seconds}

    register_local_tool(
        name="sleep",
        description="Pause execution for the requested number of seconds.",
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": "number", "minimum": 0}},
            "required": ["seconds"],
        },
        handler=sleep_tool,
    )

    def calculator_tool(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, Any]:
        _ = context
        expression = str(arguments.get("expression", "")).strip()
        if not expression:
            raise HTTPException(status_code=400, detail="calculator requires an expression")
        return {"expression": expression, "result": _evaluate_calculator_expression(expression)}

    register_local_tool(
        name="calculator",
        description="Evaluate a basic arithmetic expression safely.",
        input_schema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        handler=calculator_tool,
    )

    def retrieve_knowledge_tool(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, Any]:
        return _execute_retrieve_knowledge(arguments, context)

    register_local_tool(
        name="retrieve_knowledge",
        description="Search tenant-scoped ingested knowledge and return matching chunks.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
        handler=retrieve_knowledge_tool,
    )

    return registry


def build_tool_registry_from_env() -> ToolRegistry:
    return build_tool_registry(mcp_server_configs=load_mcp_server_configs_from_env())


def build_tool_registry(
    *,
    mcp_server_configs: list[MCPServerConfig] | None = None,
    allowed_local_tools: list[str] | None = None,
) -> ToolRegistry:
    registry = build_local_tool_registry(allowed_tools=allowed_local_tools)

    mcp_servers: list[dict[str, Any]] = []
    for config in mcp_server_configs or []:
        try:
            client = MCPHTTPClient(config)
            specs = _run_awaitable_sync(client.list_tools())
            for spec in specs:
                raw_tool_name = spec.name.split(".", 1)[1]
                registry.register(
                    name=spec.name,
                    description=spec.description,
                    input_schema=spec.input_schema,
                    handler=lambda arguments, context=None, c=client, tool_name=raw_tool_name: c.call_tool(
                        tool_name, arguments
                    ),
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


def _execute_retrieve_knowledge(
    arguments: dict[str, Any], context: ToolExecutionContext | None
) -> dict[str, Any]:
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise HTTPException(status_code=400, detail="retrieve_knowledge requires a query")

    top_k = arguments.get("top_k", 8)
    if not isinstance(top_k, int):
        raise HTTPException(status_code=400, detail="retrieve_knowledge top_k must be an integer")
    if top_k <= 0:
        raise HTTPException(status_code=400, detail="retrieve_knowledge top_k must be >= 1")

    if context is None or not context.tenant_id:
        raise HTTPException(status_code=500, detail="retrieve_knowledge requires tenant context")

    db_path = os.getenv(MINIGENT_MINIRAG_DB_PATH_ENV, "").strip()
    if not db_path:
        raise HTTPException(
            status_code=503,
            detail=f"retrieve_knowledge requires {MINIGENT_MINIRAG_DB_PATH_ENV} to be configured",
        )

    try:
        minirag_retrieve = importlib.import_module("minirag.retrieve")
        minirag_tool = importlib.import_module("minirag.tool")
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="retrieve_knowledge requires the minirag package to be installed",
        ) from exc

    MiniRAG = minirag_retrieve.MiniRAG
    build_backend = minirag_retrieve.build_backend
    retrieve_knowledge = minirag_tool.retrieve_knowledge
    backend_name = os.getenv(MINIGENT_MINIRAG_BACKEND_ENV, "lexical").strip().lower()
    embedding_provider_name = os.getenv(MINIGENT_MINIRAG_EMBEDDING_PROVIDER_ENV, "").strip() or None
    hybrid_lexical_weight_raw = (
        os.getenv(MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT_ENV, "").strip() or None
    )
    hybrid_dense_weight_raw = (
        os.getenv(MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT_ENV, "").strip() or None
    )
    rag = MiniRAG(
        db_path=db_path,
        backend=build_backend(
            backend_name,
            embedding_provider_name=embedding_provider_name,
            hybrid_lexical_weight=(
                float(hybrid_lexical_weight_raw)
                if hybrid_lexical_weight_raw is not None
                else None
            ),
            hybrid_dense_weight=(
                float(hybrid_dense_weight_raw)
                if hybrid_dense_weight_raw is not None
                else None
            ),
        ),
    )
    return retrieve_knowledge(rag, query=query, tenant_id=context.tenant_id, top_k=top_k)


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
