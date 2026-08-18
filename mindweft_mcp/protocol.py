from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.types import ErrorData, JSONRPCError, JSONRPCResponse

LEGACY_MCP_PROTOCOL_VERSION = "2025-11-25"
MODERN_MCP_PROTOCOL_VERSION = "2026-07-28"
DEFAULT_MCP_PROTOCOL_VERSION = MODERN_MCP_PROTOCOL_VERSION
SUPPORTED_MCP_PROTOCOL_VERSIONS = (
    LEGACY_MCP_PROTOCOL_VERSION,
    MODERN_MCP_PROTOCOL_VERSION,
)
MCP_PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"


def mcp_request_protocol_version(payload: Mapping[str, Any]) -> str | None:
    params = payload.get("params")
    if not isinstance(params, Mapping):
        return None
    metadata = params.get("_meta")
    if not isinstance(metadata, Mapping):
        return None
    version = metadata.get(MCP_PROTOCOL_VERSION_META_KEY)
    return version if isinstance(version, str) and version else None


def mcp_jsonrpc_error(request_id: object, code: int, message: str) -> dict[str, Any]:
    response = JSONRPCError(
        jsonrpc="2.0",
        id=_mcp_jsonrpc_id(request_id),
        error=ErrorData(code=code, message=message),
    )
    payload = response.model_dump(by_alias=True, mode="json", exclude_none=True)
    if response.id is None:
        payload["id"] = None
    return payload


def mcp_jsonrpc_result(request_id: object, result: Mapping[str, Any]) -> dict[str, Any]:
    normalized_id = _mcp_jsonrpc_id(request_id)
    if normalized_id is None:
        return mcp_jsonrpc_error(None, -32600, "Invalid Request")
    response = JSONRPCResponse(jsonrpc="2.0", id=normalized_id, result=dict(result))
    return response.model_dump(by_alias=True, mode="json", exclude_none=True)


def strip_modern_mcp_result_envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    result = normalized.get("result")
    if not isinstance(result, Mapping):
        return normalized
    legacy_result = dict(result)
    for key in ("resultType", "ttlMs", "cacheScope", "_meta"):
        legacy_result.pop(key, None)
    normalized["result"] = legacy_result
    return normalized


def _mcp_jsonrpc_id(value: object) -> int | str | None:
    if type(value) is int or isinstance(value, str):
        return value
    return None
