from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import hashlib
import importlib
import inspect
import ipaddress
import json
import logging
import operator
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from app.mcp import (
    MCPHTTPClient,
    MCPPrivateValuePolicy,
    MCPServerConfig,
    load_mcp_server_configs_from_env,
)
from app.mcp_manager import MCPRegistrySnapshot
from app.models import ToolSpec
from app.peer_agents import PeerAgentRegistry, build_peer_agent_registry_from_env
from app.private_consents import PrivateValueDisclosure
from app.private_values import PII_PLACEHOLDER_PATTERN
from app.redaction import (
    ToolResultRedactionPolicy,
    sanitize_tool_result,
    sanitize_value_for_logging,
)

logger = logging.getLogger(__name__)

MINIGENT_MINIRAG_DB_PATH_ENV = "MINIGENT_MINIRAG_DB_PATH"
MINIGENT_MINIRAG_BACKEND_ENV = "MINIGENT_MINIRAG_BACKEND"
MINIGENT_MINIRAG_EMBEDDING_PROVIDER_ENV = "MINIGENT_MINIRAG_EMBEDDING_PROVIDER"
MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT_ENV = "MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT"
MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT_ENV = "MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT"
MINIGENT_ENABLE_PEER_AGENT_TOOL_ENV = "MINIGENT_ENABLE_PEER_AGENT_TOOL"
DEFAULT_LOCAL_TOOL_NAMES = {
    "echo",
    "current_time",
    "fetch_url",
    "sleep",
    "calculator",
}
LOCAL_TOOL_NAMES = {
    *DEFAULT_LOCAL_TOOL_NAMES,
    "retrieve_knowledge",
    "peer_agent_task",
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
_FETCH_URL_DEFAULT_MAX_BYTES = 200_000
_FETCH_URL_MAX_BYTES_LIMIT = 1_000_000
_FETCH_URL_MAX_REDIRECTS = 5
_FETCH_URL_BLOCKED_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
}
_PEER_AGENT_TERMINAL_STATUSES = {"completed", "failed", "canceled"}
_PEER_AGENT_DEFAULT_TIMEOUT_SECONDS = 180.0
_PEER_AGENT_DEFAULT_POLL_INTERVAL_SECONDS = 1.0
_PEER_AGENT_MAX_OUTPUT_CHARS = 4000
_PEER_AGENT_PREVIEW_CHARS = 500


@dataclass(frozen=True)
class ToolExecutionContext:
    tenant_id: str | None = None
    thread_id: str | None = None
    private_value_resolver: Callable[[str], str] | None = None
    private_value_validator: Callable[[str], None] | None = None
    private_value_authorizer: (
        Callable[[str, str, tuple[PrivateValueDisclosure, ...]], None] | None
    ) = None


class ToolRegistry:
    def __init__(
        self,
        *,
        result_redaction_policy: ToolResultRedactionPolicy | None = None,
    ) -> None:
        self._result_redaction_policy = result_redaction_policy or ToolResultRedactionPolicy()
        self._tools: dict[
            str,
            tuple[
                ToolSpec,
                Callable[[dict[str, Any], ToolExecutionContext | None], Any],
                ToolResultRedactionPolicy | None,
            ],
        ] = {}
        self._private_value_policies: dict[str, MCPPrivateValuePolicy] = {}
        self._mcp_servers: list[dict[str, Any]] = []

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Callable[[dict[str, Any], ToolExecutionContext | None], Any],
        *,
        result_redaction_policy: ToolResultRedactionPolicy | None = None,
        private_value_policy: MCPPrivateValuePolicy | None = None,
    ) -> None:
        self._tools[name] = (
            ToolSpec(name=name, description=description, input_schema=input_schema),
            handler,
            result_redaction_policy,
        )
        self._private_value_policies[name] = private_value_policy or MCPPrivateValuePolicy()

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
            handler = tool[1]
            handler_arguments = _prepare_private_tool_arguments(
                arguments,
                policy=self._private_value_policies[name],
                resolver=context.private_value_resolver if context is not None else None,
                validator=context.private_value_validator if context is not None else None,
                authorizer=context.private_value_authorizer if context is not None else None,
                tool_name=name,
            )
            handler_context = (
                ToolExecutionContext(
                    tenant_id=context.tenant_id,
                    thread_id=context.thread_id,
                )
                if context is not None
                else None
            )
            if inspect.iscoroutinefunction(handler):
                result = handler(handler_arguments, handler_context)
            else:
                result = await asyncio.to_thread(handler, handler_arguments, handler_context)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning("tool.cancelled name=%s duration_ms=%s", name, duration_ms)
            raise
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
        return sanitize_tool_result(
            result,
            policy=tool[2] or self._result_redaction_policy,
            tool_name=name,
        )

    def set_mcp_servers(self, servers: list[dict[str, Any]]) -> None:
        self._mcp_servers = servers

    def mcp_servers(self) -> list[dict[str, Any]]:
        return list(self._mcp_servers)


def build_local_tool_registry(
    allowed_tools: list[str] | None = None,
    *,
    peer_agent_registry: PeerAgentRegistry | None = None,
    enable_peer_agent_tool: bool | None = None,
    result_redaction_policy: ToolResultRedactionPolicy | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(result_redaction_policy=result_redaction_policy)
    peer_agent_tool_enabled = _peer_agent_tool_enabled(enable_peer_agent_tool)
    allowed_tool_set = (
        set(allowed_tools) if allowed_tools is not None else set(DEFAULT_LOCAL_TOOL_NAMES)
    )
    if allowed_tools is None and peer_agent_tool_enabled:
        allowed_tool_set.add("peer_agent_task")

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
        if not url:
            raise HTTPException(status_code=400, detail="fetch_url requires a url")
        method = str(arguments.get("method", "GET")).strip().upper()
        if method not in {"GET", "HEAD"}:
            raise HTTPException(status_code=400, detail="fetch_url method must be GET or HEAD")
        timeout = _fetch_url_timeout(arguments.get("timeout_seconds", 10.0))
        max_bytes = _fetch_url_max_bytes(arguments.get("max_bytes", _FETCH_URL_DEFAULT_MAX_BYTES))
        follow_redirects = bool(arguments.get("follow_redirects", True))
        headers = _fetch_url_headers(arguments.get("headers", {}))
        _validate_fetch_url_target(url)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                current_url = url
                for redirect_count in range(_FETCH_URL_MAX_REDIRECTS + 1):
                    async with client.stream(method, current_url, headers=headers) as response:
                        if (
                            follow_redirects
                            and response.status_code in {301, 302, 303, 307, 308}
                            and response.headers.get("location")
                        ):
                            if redirect_count >= _FETCH_URL_MAX_REDIRECTS:
                                raise HTTPException(
                                    status_code=502,
                                    detail="fetch_url exceeded maximum redirects",
                                )
                            current_url = str(
                                httpx.URL(str(response.url)).join(response.headers["location"])
                            )
                            _validate_fetch_url_target(current_url)
                            if response.status_code == 303:
                                method = "GET"
                            continue
                        body = bytearray()
                        truncated = False
                        async for chunk in response.aiter_bytes():
                            remaining = max_bytes - len(body)
                            if remaining <= 0:
                                truncated = True
                                break
                            body.extend(chunk[:remaining])
                            if len(chunk) > remaining:
                                truncated = True
                                break
                        break
                else:  # pragma: no cover - defensive loop boundary
                    raise HTTPException(
                        status_code=502, detail="fetch_url exceeded maximum redirects"
                    )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"fetch_url request failed: {exc}") from exc
        content_type = response.headers.get("content-type", "")
        text = _decode_fetch_url_body(bytes(body), response)
        return {
            "url": str(response.url),
            "final_url": str(response.url),
            "status_code": response.status_code,
            "status": response.status_code,
            "content_type": content_type,
            "headers": dict(response.headers),
            "text": text,
            "body": text,
            "truncated": truncated,
        }

    register_local_tool(
        name="fetch_url",
        description="Fetch a URL and return its response text with basic metadata.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "HEAD"]},
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "timeout_seconds": {"type": "number", "minimum": 0.1},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _FETCH_URL_MAX_BYTES_LIMIT,
                },
                "follow_redirects": {"type": "boolean"},
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

    async def peer_agent_task_tool(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, Any]:
        _ = context
        registry = peer_agent_registry or build_peer_agent_registry_from_env()
        peer = _required_tool_string(arguments, "peer", "peer_agent_task")
        cwd = _required_tool_string(arguments, "cwd", "peer_agent_task")
        prompt = _required_tool_string(arguments, "prompt", "peer_agent_task")
        poll = bool(arguments.get("poll", True))
        timeout_seconds = _positive_float(
            arguments.get("timeout_seconds", _PEER_AGENT_DEFAULT_TIMEOUT_SECONDS),
            field_name="timeout_seconds",
            tool_name="peer_agent_task",
        )
        poll_interval_seconds = _positive_float(
            arguments.get("poll_interval_seconds", _PEER_AGENT_DEFAULT_POLL_INTERVAL_SECONDS),
            field_name="poll_interval_seconds",
            tool_name="peer_agent_task",
        )
        started_at = time.perf_counter()
        task_id = ""
        try:
            task = await registry.create_task(peer, {"cwd": cwd, "prompt": prompt})
            task_id = str(task.get("task_id", "")).strip()
            if not task_id:
                raise HTTPException(
                    status_code=502,
                    detail="peer_agent_task peer returned task response without task_id",
                )
            if poll:
                deadline = time.monotonic() + timeout_seconds
                while str(task.get("status", "")) not in _PEER_AGENT_TERMINAL_STATUSES:
                    if time.monotonic() >= deadline:
                        canceled_task, cancel_error = await _cancel_peer_agent_task(
                            registry, peer, task_id
                        )
                        return _peer_agent_tool_result(
                            canceled_task or task,
                            peer=peer,
                            timed_out=True,
                            duration_seconds=time.perf_counter() - started_at,
                            canceled_on_timeout=cancel_error is None,
                            cancel_error=cancel_error,
                        )
                    await asyncio.sleep(poll_interval_seconds)
                    task = await registry.task(peer, task_id)
            return _peer_agent_tool_result(
                task,
                peer=peer,
                timed_out=False,
                duration_seconds=time.perf_counter() - started_at,
            )
        except asyncio.CancelledError:
            if task_id:
                await _cancel_peer_agent_task(registry, peer, task_id)
            raise

    if peer_agent_tool_enabled:
        register_local_tool(
            name="peer_agent_task",
            description=_peer_agent_task_description(peer_agent_registry),
            input_schema=_peer_agent_task_input_schema(peer_agent_registry),
            handler=peer_agent_task_tool,
        )

    return registry


def build_tool_registry_from_env() -> ToolRegistry:
    return build_tool_registry(mcp_server_configs=load_mcp_server_configs_from_env())


def build_tool_registry(
    *,
    mcp_server_configs: list[MCPServerConfig] | None = None,
    mcp_snapshot: MCPRegistrySnapshot | None = None,
    allowed_local_tools: list[str] | None = None,
    peer_agent_registry: PeerAgentRegistry | None = None,
    enable_peer_agent_tool: bool | None = None,
    result_redaction_policy: ToolResultRedactionPolicy | None = None,
) -> ToolRegistry:
    registry = build_local_tool_registry(
        allowed_tools=allowed_local_tools,
        peer_agent_registry=peer_agent_registry,
        enable_peer_agent_tool=enable_peer_agent_tool,
        result_redaction_policy=result_redaction_policy,
    )

    mcp_servers: list[dict[str, Any]] = []
    if mcp_snapshot is not None:
        for state in mcp_snapshot.servers:
            if state.status == "connected" and state.client is not None:
                for spec in state.tools:
                    raw_tool_name = spec.name.split(".", 1)[1]
                    registry.register(
                        name=spec.name,
                        description=spec.description,
                        input_schema=spec.input_schema,
                        handler=lambda arguments, context=None, c=state.client, tool_name=raw_tool_name: (
                            c.call_tool(tool_name, arguments)
                        ),
                        result_redaction_policy=state.config.result_redaction_policy,
                        private_value_policy=state.config.private_value_tool_policies.get(
                            raw_tool_name,
                            state.config.private_value_policy,
                        ),
                    )
            mcp_servers.append(state.public_dict())
        registry.set_mcp_servers(mcp_servers)
        return registry

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
                    handler=lambda arguments, context=None, c=client, tool_name=raw_tool_name: (
                        c.call_tool(tool_name, arguments)
                    ),
                    result_redaction_policy=config.result_redaction_policy,
                    private_value_policy=config.private_value_tool_policies.get(
                        raw_tool_name,
                        config.private_value_policy,
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
                    "allowed_tools": config.allowed_tools,
                    "path_policy": {
                        "deny_globs": list(config.path_policy.deny_globs),
                        "allow_globs": list(config.path_policy.allow_globs),
                    },
                    "result_redaction": {
                        "enabled": config.result_redaction_policy.enabled,
                        "mode": config.result_redaction_policy.mode,
                        "sensitive_tools": sorted(config.result_redaction_policy.sensitive_tools),
                    },
                    "private_value_policy": {
                        "mode": config.private_value_policy.mode,
                        "argument_paths": list(config.private_value_policy.argument_paths),
                        "tool_overrides": {
                            tool_name: {
                                "mode": policy.mode,
                                "argument_paths": list(policy.argument_paths),
                            }
                            for tool_name, policy in config.private_value_tool_policies.items()
                        },
                    },
                    "status": "connected",
                    "last_error": None,
                    "last_checked_at": None,
                    "next_retry_at": None,
                }
            )
        except Exception as exc:
            logger.warning("MCP server '%s' unavailable during discovery: %s", config.name, exc)
            mcp_servers.append(
                {
                    "name": config.name,
                    "url": config.url,
                    "protocol_version": config.protocol_version,
                    "session": False,
                    "server_name": None,
                    "server_version": None,
                    "tool_count": 0,
                    "allowed_tools": config.allowed_tools,
                    "path_policy": {
                        "deny_globs": list(config.path_policy.deny_globs),
                        "allow_globs": list(config.path_policy.allow_globs),
                    },
                    "result_redaction": {
                        "enabled": config.result_redaction_policy.enabled,
                        "mode": config.result_redaction_policy.mode,
                        "sensitive_tools": sorted(config.result_redaction_policy.sensitive_tools),
                    },
                    "status": "unavailable",
                    "last_error": _tool_error_detail(exc),
                    "last_checked_at": None,
                    "next_retry_at": None,
                }
            )
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


def _peer_agent_tool_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return _env_flag(MINIGENT_ENABLE_PEER_AGENT_TOOL_ENV, default=False)


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _peer_agent_task_description(peer_agent_registry: PeerAgentRegistry | None) -> str:
    description = (
        "Submit a task to a configured peer agent and optionally poll until it finishes. "
        "Use only when explicit delegation to a peer agent is useful."
    )
    peers = _peer_agents_for_tool_metadata(peer_agent_registry)
    if not peers:
        return description
    peer_lines = []
    for peer in peers:
        peer_lines.append(_peer_agent_hint_line(peer))
    return f"{description} Available peers: " + " ".join(peer_lines)


def _peer_agent_task_input_schema(peer_agent_registry: PeerAgentRegistry | None) -> dict[str, Any]:
    peer_schema: dict[str, Any] = {
        "type": "string",
        "description": "Configured peer agent name.",
    }
    peer_names = [
        name
        for peer in _peer_agents_for_tool_metadata(peer_agent_registry)
        if (name := str(peer.get("name", "")).strip())
    ]
    if peer_names:
        peer_schema["enum"] = peer_names
    return {
        "type": "object",
        "properties": {
            "peer": peer_schema,
            "cwd": {
                "type": "string",
                "description": "Working directory to pass to the peer agent.",
            },
            "prompt": {
                "type": "string",
                "description": "Task prompt for the peer agent.",
            },
            "poll": {
                "type": "boolean",
                "description": "Whether to wait for a terminal peer task status before returning.",
            },
            "timeout_seconds": {
                "type": "number",
                "minimum": 0.1,
                "description": "Maximum time to poll before canceling the peer task.",
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 0.1,
                "description": "Delay between peer task status checks while polling.",
            },
        },
        "required": ["peer", "cwd", "prompt"],
    }


def _peer_agents_for_tool_metadata(
    peer_agent_registry: PeerAgentRegistry | None,
) -> list[dict[str, object]]:
    registry = peer_agent_registry
    if registry is None:
        try:
            registry = build_peer_agent_registry_from_env()
        except Exception:
            return []
    return registry.list_agents()


def _peer_agent_hint_line(peer: dict[str, object]) -> str:
    parts = [str(peer.get("name", "")).strip()]
    description = str(peer.get("description", "")).strip()
    if description:
        parts.append(f"description: {description}")
    capabilities = _string_list_for_tool_description(peer.get("capabilities"))
    if capabilities:
        parts.append(f"capabilities: {', '.join(capabilities)}")
    side_effects = _string_list_for_tool_description(peer.get("side_effects"))
    if side_effects:
        parts.append(f"side effects: {', '.join(side_effects)}")
    version = str(peer.get("version", "")).strip()
    if version:
        parts.append(f"version: {version}")
    return "- " + "; ".join(part for part in parts if part)


def _string_list_for_tool_description(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _required_tool_string(arguments: dict[str, Any], field_name: str, tool_name: str) -> str:
    value = str(arguments.get(field_name, "")).strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{tool_name} requires {field_name}")
    return value


def _positive_float(value: object, *, field_name: str, tool_name: str) -> float:
    if not isinstance(value, str | int | float):
        raise HTTPException(
            status_code=400,
            detail=f"{tool_name} requires {field_name} to be numeric",
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{tool_name} requires {field_name} to be numeric",
        ) from exc
    if parsed <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"{tool_name} requires {field_name} > 0",
        )
    return parsed


def _peer_agent_tool_result(
    task: dict[str, Any],
    *,
    peer: str,
    timed_out: bool,
    duration_seconds: float,
    canceled_on_timeout: bool = False,
    cancel_error: str | None = None,
) -> dict[str, Any]:
    final_output = str(task.get("final_output") or "").strip()
    stdout_tail = str(task.get("stdout_tail") or "").strip()
    stderr_tail = str(task.get("stderr_tail") or "").strip()
    result: dict[str, Any] = {
        "peer": peer,
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "exit_code": task.get("exit_code"),
        "timed_out": timed_out,
        "canceled_on_timeout": canceled_on_timeout,
        "duration_seconds": round(max(0.0, duration_seconds), 3),
        "events_count": _peer_agent_events_count(task),
    }
    if cancel_error:
        result["cancel_error"] = cancel_error
    if final_output:
        result["final_output"] = _truncate_peer_agent_output(final_output)
        result["final_output_preview"] = _truncate_peer_agent_preview(final_output)
    if stdout_tail:
        result["stdout_tail"] = _truncate_peer_agent_output(stdout_tail)
    if stderr_tail:
        result["stderr_tail"] = _truncate_peer_agent_output(stderr_tail)
        result["stderr_tail_preview"] = _truncate_peer_agent_preview(stderr_tail)
    logger.info(
        "peer_agent_task.result peer=%s task_id=%s status=%s exit_code=%s timed_out=%s "
        "duration_seconds=%s canceled_on_timeout=%s events_count=%s "
        "final_output_preview=%s stderr_tail_preview=%s",
        result["peer"],
        result["task_id"],
        result["status"],
        result["exit_code"],
        result["timed_out"],
        result["duration_seconds"],
        result["canceled_on_timeout"],
        result["events_count"],
        sanitize_value_for_logging("final_output_preview", result.get("final_output_preview", "")),
        sanitize_value_for_logging("stderr_tail_preview", result.get("stderr_tail_preview", "")),
    )
    return result


async def _cancel_peer_agent_task(
    registry: PeerAgentRegistry,
    peer: str,
    task_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        canceled_task = await registry.cancel_task(peer, task_id)
    except Exception as exc:
        detail = _tool_error_detail(exc)
        logger.warning(
            "peer_agent_task.cancel_failed peer=%s task_id=%s error_type=%s detail=%s",
            peer,
            task_id,
            type(exc).__name__,
            detail,
        )
        return None, detail
    logger.info("peer_agent_task.cancel peer=%s task_id=%s", peer, task_id)
    return canceled_task, None


def _truncate_peer_agent_output(text: str) -> str:
    if len(text) <= _PEER_AGENT_MAX_OUTPUT_CHARS:
        return text
    return text[:_PEER_AGENT_MAX_OUTPUT_CHARS] + "\n...[truncated]"


def _truncate_peer_agent_preview(text: str) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= _PEER_AGENT_PREVIEW_CHARS:
        return normalized
    return normalized[:_PEER_AGENT_PREVIEW_CHARS] + "...[truncated]"


def _peer_agent_events_count(task: dict[str, Any]) -> int:
    for key in ("events_tail", "events"):
        events = task.get(key)
        if isinstance(events, list):
            return len(events)
    return 0


def _fetch_url_timeout(raw_timeout: Any) -> float:
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="fetch_url timeout_seconds must be a number"
        ) from exc
    if timeout < 0.1 or timeout > 60:
        raise HTTPException(
            status_code=400,
            detail="fetch_url timeout_seconds must be between 0.1 and 60",
        )
    return timeout


def _fetch_url_max_bytes(raw_max_bytes: Any) -> int:
    if isinstance(raw_max_bytes, bool):
        raise HTTPException(status_code=400, detail="fetch_url max_bytes must be an integer")
    try:
        max_bytes = int(raw_max_bytes)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="fetch_url max_bytes must be an integer"
        ) from exc
    if max_bytes < 1 or max_bytes > _FETCH_URL_MAX_BYTES_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"fetch_url max_bytes must be between 1 and {_FETCH_URL_MAX_BYTES_LIMIT}",
        )
    return max_bytes


def _fetch_url_headers(raw_headers: Any) -> dict[str, str]:
    if raw_headers is None:
        return {}
    if not isinstance(raw_headers, dict):
        raise HTTPException(status_code=400, detail="fetch_url headers must be an object")
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        name = str(raw_name).strip()
        if not name:
            raise HTTPException(status_code=400, detail="fetch_url header names must be non-empty")
        if name.lower() in _FETCH_URL_BLOCKED_REQUEST_HEADERS:
            raise HTTPException(
                status_code=400,
                detail=f"fetch_url header '{name}' is not allowed",
            )
        if not isinstance(raw_value, str):
            raise HTTPException(
                status_code=400,
                detail=f"fetch_url header '{name}' value must be a string",
            )
        headers[name] = raw_value
    return headers


def _validate_fetch_url_target(raw_url: str) -> None:
    try:
        url = httpx.URL(raw_url)
    except httpx.InvalidURL as exc:
        raise HTTPException(status_code=400, detail="fetch_url url is invalid") from exc
    if url.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="fetch_url only supports http and https URLs")
    if not url.host:
        raise HTTPException(status_code=400, detail="fetch_url requires a URL host")
    host = url.host.strip("[]")
    if _is_blocked_fetch_host(host):
        raise HTTPException(status_code=400, detail="fetch_url cannot access private network hosts")


def _is_blocked_fetch_host(host: str) -> bool:
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise HTTPException(
                status_code=400, detail=f"fetch_url could not resolve host '{host}'"
            ) from exc
        addresses = []
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            try:
                addresses.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                continue
    return any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        for address in addresses
    )


def _decode_fetch_url_body(body: bytes, response: httpx.Response) -> str:
    if not body:
        return ""
    encoding = response.encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


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
                float(hybrid_lexical_weight_raw) if hybrid_lexical_weight_raw is not None else None
            ),
            hybrid_dense_weight=(
                float(hybrid_dense_weight_raw) if hybrid_dense_weight_raw is not None else None
            ),
        ),
    )
    return retrieve_knowledge(rag, query=query, tenant_id=context.tenant_id, top_k=top_k)


def _prepare_private_tool_arguments(
    arguments: dict[str, Any],
    *,
    policy: MCPPrivateValuePolicy,
    resolver: Callable[[str], str] | None,
    validator: Callable[[str], None] | None,
    authorizer: (Callable[[str, str, tuple[PrivateValueDisclosure, ...]], None] | None),
    tool_name: str,
) -> dict[str, Any]:
    if policy.mode == "pass_through":
        return arguments

    disclosures: list[PrivateValueDisclosure] = []

    def discover(value: Any, path: str) -> None:
        if isinstance(value, str):
            disclosures.extend(
                PrivateValueDisclosure(
                    path=path,
                    kind=match.group("kind"),
                    reference=match.group("reference"),
                )
                for match in PII_PLACEHOLDER_PATTERN.finditer(value)
            )
            return
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                if PII_PLACEHOLDER_PATTERN.search(key_text) is not None:
                    raise HTTPException(
                        status_code=403,
                        detail="Tool private-value disclosure is not allowed in argument keys",
                    )
                discover(item, f"{path}.{key_text}" if path else key_text)
            return
        if isinstance(value, list):
            for item in value:
                discover(item, f"{path}[*]")

    discover(arguments, "")
    if not disclosures:
        return arguments
    if policy.mode == "deny":
        raise HTTPException(
            status_code=403,
            detail="Tool private-value disclosure is denied by policy",
        )
    allowed_paths = set(policy.argument_paths)
    disallowed_path = next(
        (item.path for item in disclosures if item.path not in allowed_paths),
        None,
    )
    if disallowed_path is not None:
        raise HTTPException(
            status_code=403,
            detail=(
                "Tool private-value disclosure is not allowed for argument path "
                f"'{disallowed_path}'"
            ),
        )
    if resolver is None:
        raise HTTPException(
            status_code=500,
            detail="Private-value resolution is unavailable for this tool execution",
        )
    if authorizer is None:
        raise HTTPException(
            status_code=500,
            detail="Private-value consent is unavailable for this tool execution",
        )

    def preflight(value: Any) -> None:
        if isinstance(value, str):
            if PII_PLACEHOLDER_PATTERN.search(value) is not None:
                if validator is not None:
                    validator(value)
                else:
                    # Backward-compatible fallback for custom execution contexts. Runtime
                    # contexts provide a non-disclosing validator.
                    resolver(value)
            return
        if isinstance(value, dict):
            for item in value.values():
                preflight(item)
            return
        if isinstance(value, list):
            for item in value:
                preflight(item)

    preflight(arguments)
    argument_fingerprint = hashlib.sha256(
        json.dumps(
            arguments,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    authorizer(
        tool_name,
        argument_fingerprint,
        tuple(sorted(set(disclosures))),
    )

    def resolve(value: Any) -> Any:
        if isinstance(value, str):
            return resolver(value) if PII_PLACEHOLDER_PATTERN.search(value) is not None else value
        if isinstance(value, dict):
            return {str(key): resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    prepared = resolve(arguments)
    if not isinstance(prepared, dict):
        raise HTTPException(status_code=500, detail="Tool arguments must remain an object")
    return prepared


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
