from __future__ import annotations

import fnmatch
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import HTTPException

from app.models import ToolSpec
from app.redaction import (
    ToolResultRedactionPolicy,
    parse_tool_result_redaction_policy,
    redact_url_secrets,
)

logger = logging.getLogger(__name__)

DEFAULT_MCP_PROTOCOL_VERSION = "2025-11-25"
DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class MCPPathPolicy:
    deny_globs: list[str] = field(default_factory=list)
    allow_globs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    url: str
    headers: dict[str, str]
    protocol_version: str = DEFAULT_MCP_PROTOCOL_VERSION
    allowed_tools: list[str] | None = None
    path_policy: MCPPathPolicy = field(default_factory=MCPPathPolicy)
    result_redaction_policy: ToolResultRedactionPolicy = field(
        default_factory=ToolResultRedactionPolicy
    )
    timeout_seconds: float = DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS


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
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._timeout = timeout if timeout is not None else config.timeout_seconds
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

    async def list_tools(self) -> list[ToolSpec]:
        await self._ensure_initialized()
        tools: list[ToolSpec] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params or None)
            for tool in result.get("tools", []):
                tool_name = tool["name"]
                if not self._is_tool_allowed(tool_name):
                    continue
                namespaced_name = f"{self._config.name}.{tool_name}"
                description = (
                    tool.get("description") or f"MCP tool {tool_name} from {self._config.name}"
                )
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

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not self._is_tool_allowed(tool_name):
            raise HTTPException(
                status_code=403,
                detail=f"MCP tool '{self._config.name}.{tool_name}' is not allowed",
            )
        self._validate_path_policy(tool_name, arguments)
        await self._ensure_initialized()
        result = await self._request(
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
            return {"content": self._filter_content(tool_name, result["content"])}
        return result

    def _is_tool_allowed(self, tool_name: str) -> bool:
        return self._config.allowed_tools is None or tool_name in self._config.allowed_tools

    def _validate_path_policy(self, tool_name: str, arguments: dict[str, Any]) -> None:
        _ = tool_name
        for path in _iter_path_arguments(arguments):
            if _path_denied(path, self._config.path_policy):
                raise HTTPException(
                    status_code=403,
                    detail=f"MCP path '{path}' is denied by server '{self._config.name}' policy",
                )

    def _filter_content(self, tool_name: str, content: Any) -> Any:
        if tool_name != "list_directory" or not self._config.path_policy.deny_globs:
            return content
        if not isinstance(content, list):
            return content
        filtered: list[Any] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                filtered.append(item)
                continue
            text = item.get("text")
            if not isinstance(text, str):
                filtered.append(item)
                continue
            filtered.append(
                {**item, "text": _filter_directory_listing_text(text, self._config.path_policy)}
            )
        return filtered

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        result, headers = await self._request_raw(
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
        self._negotiated_protocol_version = result.get(
            "protocolVersion", self._config.protocol_version
        )
        server_info = result.get("serverInfo") or {}
        self._server_name = server_info.get("name")
        self._server_version = server_info.get("version")
        await self._notify("notifications/initialized")
        self._initialized = True
        logger.info(
            "MCP initialized: server=%s url=%s protocol=%s session=%s remote=%s@%s",
            self._config.name,
            redact_url_secrets(self._config.url),
            self._negotiated_protocol_version,
            bool(self._session_id),
            self._server_name,
            self._server_version,
        )

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        headers = self._build_headers(include_protocol=True)
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.post(
                self._config.url,
                json=payload,
                headers=headers,
            )
        if response.status_code not in (200, 202):
            raise HTTPException(
                status_code=502,
                detail=f"MCP notification failed for server '{self._config.name}': {response.text}",
            )

    async def _request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        result, _ = await self._request_raw(method, params, use_protocol_header=True)
        return result

    async def _request_raw(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        use_protocol_header: bool,
        retry_invalid_session: bool = True,
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

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(
                    self._config.url,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if self._should_retry_invalid_session(
                    exc.response,
                    use_protocol_header=use_protocol_header,
                    retry_invalid_session=retry_invalid_session,
                ):
                    logger.warning(
                        "MCP session rejected; reinitializing and retrying once: server=%s url=%s status=%s",
                        self._config.name,
                        redact_url_secrets(self._config.url),
                        exc.response.status_code,
                    )
                    self._reset_session_state()
                    await self._ensure_initialized()
                    return await self._request_raw(
                        method,
                        params,
                        use_protocol_header=use_protocol_header,
                        retry_invalid_session=False,
                    )
                raise HTTPException(
                    status_code=502,
                    detail=f"MCP server '{self._config.name}' HTTP error: {exc.response.text}",
                ) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"MCP server '{self._config.name}' request failed: {exc}",
                ) from exc

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            body = response.json()
        elif "text/event-stream" in content_type:
            body = _parse_sse_jsonrpc_response(
                response.text,
                request_id=self._request_id,
                server_name=self._config.name,
            )
        else:
            raise HTTPException(
                status_code=502,
                detail=f"MCP server '{self._config.name}' returned unsupported content type '{content_type}'",
            )
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

    def _reset_session_state(self) -> None:
        self._session_id = None
        self._negotiated_protocol_version = self._config.protocol_version
        self._initialized = False
        self._server_name = None
        self._server_version = None

    def _should_retry_invalid_session(
        self,
        response: httpx.Response,
        *,
        use_protocol_header: bool,
        retry_invalid_session: bool,
    ) -> bool:
        if not retry_invalid_session or not use_protocol_header or not self._session_id:
            return False
        if response.status_code == 404:
            return True
        if response.status_code != 400:
            return False

        detail = response.text.lower()
        return "session" in detail and (
            "invalid" in detail or "no valid" in detail or "missing" in detail
        )

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
        allowed_tools = entry.get("allowed_tools", entry.get("allowedTools"))
        path_policy = _parse_path_policy(name, entry.get("path_policy", entry.get("pathPolicy")))
        result_redaction_policy = parse_tool_result_redaction_policy(
            entry.get("result_redaction", entry.get("resultRedaction")),
            context=f"MCP server '{name}'",
        )
        timeout_seconds = _parse_positive_float_config(
            entry.get(
                "timeout_seconds",
                entry.get("timeoutSeconds", DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS),
            ),
            f"MCP server '{name}' timeout_seconds",
        )
        if not isinstance(name, str) or not name:
            raise RuntimeError("Each MINIGENT_MCP_SERVERS entry must include a non-empty 'name'")
        if not isinstance(url, str) or not url:
            raise RuntimeError("Each MINIGENT_MCP_SERVERS entry must include a non-empty 'url'")
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            raise RuntimeError(f"MCP server '{name}' has invalid headers")
        if allowed_tools is not None and (
            not isinstance(allowed_tools, list)
            or not all(isinstance(item, str) and item for item in allowed_tools)
        ):
            raise RuntimeError(f"MCP server '{name}' has invalid allowed_tools")
        configs.append(
            MCPServerConfig(
                name=name,
                url=url,
                headers=headers,
                protocol_version=str(protocol_version),
                allowed_tools=list(allowed_tools) if allowed_tools is not None else None,
                path_policy=path_policy,
                result_redaction_policy=result_redaction_policy,
                timeout_seconds=timeout_seconds,
            )
        )
    return configs


def _parse_positive_float_config(value: object, label: str) -> float:
    if not isinstance(value, str | int | float):
        raise RuntimeError(f"{label} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be numeric") from exc
    if parsed <= 0:
        raise RuntimeError(f"{label} must be positive")
    return parsed


def _parse_path_policy(server_name: object, raw: object) -> MCPPathPolicy:
    if raw is None:
        return MCPPathPolicy()
    if not isinstance(raw, dict):
        raise RuntimeError(f"MCP server '{server_name}' has invalid path_policy")
    deny_globs = raw.get("deny_globs", raw.get("denyGlobs")) or []
    allow_globs = raw.get("allow_globs", raw.get("allowGlobs")) or []
    if not isinstance(deny_globs, list) or not all(
        isinstance(item, str) and item for item in deny_globs
    ):
        raise RuntimeError(f"MCP server '{server_name}' has invalid path_policy.deny_globs")
    if not isinstance(allow_globs, list) or not all(
        isinstance(item, str) and item for item in allow_globs
    ):
        raise RuntimeError(f"MCP server '{server_name}' has invalid path_policy.allow_globs")
    return MCPPathPolicy(deny_globs=list(deny_globs), allow_globs=list(allow_globs))


def _iter_path_arguments(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"path", "paths", "source", "destination", "target"}:
                paths.extend(_coerce_paths(nested))
            elif isinstance(nested, dict | list):
                paths.extend(_iter_path_arguments(nested))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_iter_path_arguments(item))
    return paths


def _coerce_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _path_denied(path: str, policy: MCPPathPolicy) -> bool:
    normalized = path.replace("\\", "/").rstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if _matches_path_globs(normalized, parts, policy.allow_globs):
        return False
    return _matches_path_globs(normalized, parts, policy.deny_globs)


def _matches_path_globs(normalized: str, parts: list[str], patterns: list[str]) -> bool:
    candidates = {normalized, normalized.lstrip("/")}
    candidates.update(parts)
    candidates.update("/".join(parts[index:]) for index in range(len(parts)))
    expanded_patterns = set(patterns)
    expanded_patterns.update(
        pattern.removesuffix("/**") for pattern in patterns if pattern.endswith("/**")
    )
    return any(
        fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(f"/{candidate}", pattern)
        for pattern in expanded_patterns
        for candidate in candidates
    )


def _filter_directory_listing_text(text: str, policy: MCPPathPolicy) -> str:
    kept_lines: list[str] = []
    hidden_count = 0
    for line in text.splitlines():
        name = line.rsplit(" ", 1)[-1].strip()
        if name and _path_denied(name, policy):
            hidden_count += 1
            continue
        kept_lines.append(line)
    if hidden_count:
        kept_lines.append(
            f"[hidden {hidden_count} entr{'y' if hidden_count == 1 else 'ies'} by path policy]"
        )
    return "\n".join(kept_lines)


def _parse_sse_jsonrpc_response(
    stream_text: str, *, request_id: int, server_name: str
) -> dict[str, Any]:
    for event_data in _iter_sse_data_messages(stream_text):
        try:
            payload = json.loads(event_data)
        except json.JSONDecodeError:
            logger.debug(
                "Ignoring non-JSON SSE event from MCP server '%s': %s", server_name, event_data
            )
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("id") == request_id:
            return payload
    raise HTTPException(
        status_code=502,
        detail=f"MCP server '{server_name}' returned no matching JSON-RPC response in event stream",
    )


def _iter_sse_data_messages(stream_text: str) -> list[str]:
    messages: list[str] = []
    data_lines: list[str] = []
    for raw_line in stream_text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                messages.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        messages.append("\n".join(data_lines))
    return messages
