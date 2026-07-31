from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import HTTPException

from app.mcp_identity import MCPIdentityTokenIssuer
from app.models import ToolSpec
from app.redaction import (
    ToolResultRedactionPolicy,
    parse_tool_result_redaction_policy,
    redact_url_secrets,
)

logger = logging.getLogger(__name__)

DEFAULT_MCP_PROTOCOL_VERSION = "2025-11-25"
DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 30.0
MCP_SERVERS_ENV = "MINIGENT_MCP_SERVERS"
PRIVATE_VALUES_META_KEY = "io.minigent/private-values"
LEGACY_PRIVATE_VALUES_META_KEY = "io.minigent/carddav-private-values"
PRIVATE_VALUES_META_KEYS = (PRIVATE_VALUES_META_KEY, LEGACY_PRIVATE_VALUES_META_KEY)
PRIVATE_VALUE_DISCLOSURE_MODES = frozenset({"deny", "pass_through", "resolve_selected"})
_PRIVATE_VALUE_ARGUMENT_PATH_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]*(?:(?:\.[A-Za-z_][A-Za-z0-9_-]*)|(?:\[\*\]))*$"
)


@dataclass(frozen=True)
class MCPPrivateToolResult:
    model_content: Any
    private_values: dict[str, str]


@dataclass(frozen=True)
class MCPSettings:
    servers: list[MCPServerConfig] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MCPSettings:
        lookup = os.environ if env is None else env
        return cls(servers=_parse_mcp_server_configs(lookup.get(MCP_SERVERS_ENV, "")))


@dataclass(frozen=True)
class MCPPathPolicy:
    deny_globs: list[str] = field(default_factory=list)
    allow_globs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MCPPrivateValuePolicy:
    mode: str = "deny"
    argument_paths: tuple[str, ...] = ()
    requires_approval: bool = False


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
    private_value_policy: MCPPrivateValuePolicy = field(default_factory=MCPPrivateValuePolicy)
    private_value_tool_policies: dict[str, MCPPrivateValuePolicy] = field(default_factory=dict)
    trusted_input_preprocessor_tools: frozenset[str] = frozenset()
    forward_identity: bool = False
    identity_audience: str = "private-dav"
    identity_scopes: tuple[str, ...] = ()
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
        identity_issuer: MCPIdentityTokenIssuer | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._timeout = timeout if timeout is not None else config.timeout_seconds
        self._identity_issuer = identity_issuer
        if config.forward_identity and self._identity_issuer is None:
            self._identity_issuer = MCPIdentityTokenIssuer.from_env(
                audience=config.identity_audience
            )
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

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: Any | None = None,
    ) -> Any:
        if self._config.forward_identity and (
            not getattr(context, "tenant_id", None) or not getattr(context, "user_id", None)
        ):
            raise HTTPException(
                status_code=500,
                detail=f"MCP tool '{self._config.name}.{tool_name}' requires user identity context",
            )
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
            identity_context=context,
        )
        if result.get("isError"):
            error_result = {key: value for key, value in result.items() if key != "_meta"}
            raise HTTPException(
                status_code=502,
                detail=(
                    f"MCP tool '{self._config.name}.{tool_name}' returned an error: "
                    f"{json.dumps(error_result, ensure_ascii=True)}"
                ),
            )
        return parse_mcp_tool_result(
            result,
            tool_name=tool_name,
            content_filter=self._filter_content,
        )

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
            try:
                response = await client.post(
                    self._config.url,
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                raise HTTPException(
                    status_code=504,
                    detail=f"MCP notification timed out for server '{self._config.name}' after {self._timeout:g}s",
                ) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"MCP notification failed for server '{self._config.name}': {exc}",
                ) from exc
        if response.status_code not in (200, 202):
            raise HTTPException(
                status_code=502,
                detail=f"MCP notification failed for server '{self._config.name}': {response.text}",
            )

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        identity_context: Any | None = None,
    ) -> dict[str, Any]:
        result, _ = await self._request_raw(
            method,
            params,
            use_protocol_header=True,
            identity_context=identity_context,
        )
        return result

    async def _request_raw(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        use_protocol_header: bool,
        retry_invalid_session: bool = True,
        identity_context: Any | None = None,
    ) -> tuple[dict[str, Any], httpx.Headers]:
        self._request_id += 1
        request_id = self._request_id
        headers = self._build_headers(
            include_protocol=use_protocol_header,
            identity_context=identity_context,
        )
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
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
                        identity_context=identity_context,
                    )
                raise HTTPException(
                    status_code=502,
                    detail=f"MCP server '{self._config.name}' HTTP error: {exc.response.text}",
                ) from exc
            except httpx.TimeoutException as exc:
                raise HTTPException(
                    status_code=504,
                    detail=f"MCP server '{self._config.name}' request timed out after {self._timeout:g}s",
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
                request_id=request_id,
                server_name=self._config.name,
            )
        else:
            raise HTTPException(
                status_code=502,
                detail=f"MCP server '{self._config.name}' returned unsupported content type '{content_type}'",
            )
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=502,
                detail=f"MCP server '{self._config.name}' returned non-object JSON-RPC response",
            )
        if body.get("id") != request_id:
            raise HTTPException(
                status_code=502,
                detail=f"MCP server '{self._config.name}' returned mismatched JSON-RPC response id",
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

    def _build_headers(
        self,
        *,
        include_protocol: bool,
        identity_context: Any | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self._config.headers,
        }
        if self._identity_issuer is not None:
            tenant_id = getattr(identity_context, "tenant_id", None) or "__mcp_discovery__"
            user_id = getattr(identity_context, "user_id", None) or "__mcp_discovery__"
            headers["Authorization"] = "Bearer " + self._identity_issuer.issue(
                tenant_id=tenant_id,
                user_id=user_id,
                scopes=self._config.identity_scopes,
            )
        if include_protocol:
            headers["MCP-Protocol-Version"] = self._negotiated_protocol_version
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        return headers


def load_mcp_server_configs_from_env(env: Mapping[str, str] | None = None) -> list[MCPServerConfig]:
    return MCPSettings.from_env(env).servers


def mcp_settings_from_env() -> MCPSettings:
    return MCPSettings.from_env()


def parse_mcp_tool_result(
    result: dict[str, Any],
    *,
    tool_name: str,
    content_filter: Callable[[str, Any], Any] | None = None,
) -> Any:
    if "structuredContent" in result:
        model_content: Any = result["structuredContent"]
    elif "content" in result:
        content = result["content"]
        if content_filter is not None:
            content = content_filter(tool_name, content)
        model_content = {"content": content}
    else:
        model_content = {
            key: value for key, value in result.items() if key not in {"_meta", "isError"}
        }

    metadata = result.get("_meta")
    if not isinstance(metadata, dict):
        return model_content
    raw_private_values = next(
        (metadata[key] for key in PRIVATE_VALUES_META_KEYS if key in metadata),
        None,
    )
    if raw_private_values is None:
        return model_content
    if not isinstance(raw_private_values, dict) or not all(
        isinstance(reference, str) and bool(reference) and isinstance(value, str)
        for reference, value in raw_private_values.items()
    ):
        raise HTTPException(
            status_code=502,
            detail=(
                f"MCP tool '{tool_name}' returned invalid private-value metadata; "
                "expected a string-to-string object"
            ),
        )
    return MCPPrivateToolResult(
        model_content=model_content,
        private_values=dict(raw_private_values),
    )


def _parse_mcp_server_configs(raw_value: str) -> list[MCPServerConfig]:
    raw = raw_value.strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{MCP_SERVERS_ENV} must be valid JSON") from exc
    if not isinstance(parsed, list):
        raise RuntimeError(f"{MCP_SERVERS_ENV} must be a JSON array")

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
        private_value_policy = parse_mcp_private_value_policy(
            entry.get("private_value_policy", entry.get("privateValuePolicy")),
            context=f"MCP server '{name}'",
        )
        private_value_tool_policies = parse_mcp_private_value_tool_policies(
            entry.get("private_value_tool_policies", entry.get("privateValueToolPolicies")),
            context=f"MCP server '{name}'",
        )
        trusted_input_preprocessor_tools = entry.get(
            "trusted_input_preprocessor_tools",
            entry.get("trustedInputPreprocessorTools", []),
        )
        forward_identity = entry.get("forward_identity", entry.get("forwardIdentity", False))
        identity_audience = entry.get(
            "identity_audience", entry.get("identityAudience", "private-dav")
        )
        identity_scopes = entry.get("identity_scopes", entry.get("identityScopes", []))
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
        if not isinstance(trusted_input_preprocessor_tools, list) or not all(
            isinstance(item, str) and item for item in trusted_input_preprocessor_tools
        ):
            raise RuntimeError(f"MCP server '{name}' has invalid trusted_input_preprocessor_tools")
        if len(set(trusted_input_preprocessor_tools)) != len(trusted_input_preprocessor_tools):
            raise RuntimeError(
                f"MCP server '{name}' has duplicate trusted_input_preprocessor_tools"
            )
        if not isinstance(forward_identity, bool):
            raise RuntimeError(f"MCP server '{name}' has invalid forward_identity")
        if not isinstance(identity_audience, str) or not identity_audience:
            raise RuntimeError(f"MCP server '{name}' has invalid identity_audience")
        if not isinstance(identity_scopes, list) or not all(
            isinstance(item, str) and item for item in identity_scopes
        ):
            raise RuntimeError(f"MCP server '{name}' has invalid identity_scopes")
        if len(set(identity_scopes)) != len(identity_scopes):
            raise RuntimeError(f"MCP server '{name}' has duplicate identity_scopes")
        if forward_identity and any(key.lower() == "authorization" for key in headers):
            raise RuntimeError(
                f"MCP server '{name}' cannot combine forward_identity with Authorization header"
            )
        if allowed_tools is not None and not set(trusted_input_preprocessor_tools) <= set(
            allowed_tools
        ):
            raise RuntimeError(
                f"MCP server '{name}' trusted_input_preprocessor_tools must be allowed"
            )
        configs.append(
            MCPServerConfig(
                name=name,
                url=url,
                headers=headers,
                protocol_version=str(protocol_version),
                allowed_tools=list(allowed_tools) if allowed_tools is not None else None,
                path_policy=path_policy,
                result_redaction_policy=result_redaction_policy,
                private_value_policy=private_value_policy,
                private_value_tool_policies=private_value_tool_policies,
                trusted_input_preprocessor_tools=frozenset(trusted_input_preprocessor_tools),
                forward_identity=forward_identity,
                identity_audience=identity_audience,
                identity_scopes=tuple(identity_scopes),
                timeout_seconds=timeout_seconds,
            )
        )
    return configs


def parse_mcp_private_value_policy(
    raw: object,
    *,
    context: str,
) -> MCPPrivateValuePolicy:
    if raw is None:
        return MCPPrivateValuePolicy()
    if isinstance(raw, str):
        mode = raw
        argument_paths: object = []
        requires_approval: object = False
    elif isinstance(raw, dict):
        mode = raw.get("mode", "deny")
        argument_paths = raw.get("argument_paths", raw.get("argumentPaths", []))
        requires_approval = raw.get("requires_approval", raw.get("requiresApproval", False))
    else:
        raise RuntimeError(f"{context} has invalid private_value_policy")
    if not isinstance(mode, str) or mode not in PRIVATE_VALUE_DISCLOSURE_MODES:
        choices = ", ".join(sorted(PRIVATE_VALUE_DISCLOSURE_MODES))
        raise RuntimeError(f"{context} private_value_policy mode must be one of: {choices}")
    if not isinstance(argument_paths, list) or not all(
        isinstance(path, str) and _PRIVATE_VALUE_ARGUMENT_PATH_PATTERN.fullmatch(path)
        for path in argument_paths
    ):
        raise RuntimeError(
            f"{context} private_value_policy argument_paths must be valid JSON paths"
        )
    if mode == "resolve_selected" and not argument_paths:
        raise RuntimeError(
            f"{context} resolve_selected private_value_policy requires argument_paths"
        )
    if mode != "resolve_selected" and argument_paths:
        raise RuntimeError(
            f"{context} private_value_policy argument_paths require resolve_selected mode"
        )
    if not isinstance(requires_approval, bool):
        raise RuntimeError(f"{context} private_value_policy requires_approval must be boolean")
    return MCPPrivateValuePolicy(
        mode=mode,
        argument_paths=tuple(argument_paths),
        requires_approval=requires_approval,
    )


def parse_mcp_private_value_tool_policies(
    raw: object,
    *,
    context: str,
) -> dict[str, MCPPrivateValuePolicy]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(tool_name, str) and tool_name for tool_name in raw
    ):
        raise RuntimeError(f"{context} has invalid private_value_tool_policies")
    return {
        tool_name: parse_mcp_private_value_policy(
            policy,
            context=f"{context} tool '{tool_name}'",
        )
        for tool_name, policy in raw.items()
    }


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
