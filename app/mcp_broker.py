from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
import logging
from typing import Any

from fastapi import HTTPException, Request, Response

from app.models import Principal
from app.tools import ToolExecutionContext, ToolRegistry

MINIGENT_MCP_BROKER_BASE_URL_ENV = "MINIGENT_MCP_BROKER_BASE_URL"
MINIGENT_MCP_BROKER_URL_ENV = "MINIGENT_MCP_BROKER_URL"
MINIGENT_MCP_BROKER_TOKEN_ENV = "MINIGENT_MCP_BROKER_TOKEN"
MINIGENT_MCP_BROKER_SESSION_ENV = "MINIGENT_MCP_BROKER_SESSION"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPBrokerSession:
    session_id: str
    token: str
    tenant_id: str
    user_id: str
    thread_id: str
    tool_registry: ToolRegistry
    expires_at: float


class MCPBrokerSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, MCPBrokerSession] = {}

    def create_session(
        self,
        *,
        principal: Principal,
        thread_id: str,
        tool_registry: ToolRegistry,
        ttl_seconds: float,
    ) -> MCPBrokerSession:
        self._prune_expired()
        session_id = secrets.token_urlsafe(18)
        token = secrets.token_urlsafe(32)
        session = MCPBrokerSession(
            session_id=session_id,
            token=token,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            thread_id=thread_id,
            tool_registry=tool_registry,
            expires_at=time.time() + ttl_seconds,
        )
        self._sessions[session_id] = session
        logger.info(
            "mcp_broker.session_created session_id=%s tenant_id=%s thread_id=%s tools=%s",
            session_id,
            principal.tenant_id,
            thread_id,
            len(tool_registry.specs()),
        )
        return session

    def delete_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            logger.info(
                "mcp_broker.session_deleted session_id=%s tenant_id=%s thread_id=%s",
                session_id,
                session.tenant_id,
                session.thread_id,
            )

    def require_session(self, session_id: str, token: str | None) -> MCPBrokerSession:
        self._prune_expired()
        session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="MCP broker session not found")
        if not token or not secrets.compare_digest(token, session.token):
            raise HTTPException(status_code=401, detail="Invalid MCP broker token")
        return session

    def _prune_expired(self) -> None:
        now = time.time()
        expired = [session_id for session_id, session in self._sessions.items() if session.expires_at <= now]
        for session_id in expired:
            del self._sessions[session_id]


async def handle_mcp_broker_request(
    *,
    session_store: MCPBrokerSessionStore,
    session_id: str,
    request: Request,
) -> Response | dict[str, object]:
    token = _bearer_token(request) or request.headers.get("x-minigent-mcp-broker-token")
    session = session_store.require_session(session_id, token)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="MCP broker request must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="MCP broker request must be a JSON object")

    request_id = payload.get("id")
    method = str(payload.get("method", ""))
    params = payload.get("params") or {}
    if params is not None and not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32602, "Invalid params")

    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "minigent-mcp-broker", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        )
    if method == "tools/list":
        logger.info(
            "mcp_broker.tools_list session_id=%s tenant_id=%s thread_id=%s tools=%s",
            session.session_id,
            session.tenant_id,
            session.thread_id,
            len(session.tool_registry.specs()),
        )
        return _jsonrpc_result(
            request_id,
            {
                "tools": [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "inputSchema": spec.input_schema,
                    }
                    for spec in session.tool_registry.specs()
                ]
            },
        )
    if method == "tools/call":
        return await _call_tool(session, request_id, params)
    return _jsonrpc_error(request_id, -32601, f"Unsupported MCP method '{method}'")


async def _call_tool(
    session: MCPBrokerSession,
    request_id: object,
    params: dict[str, Any],
) -> dict[str, object]:
    name = str(params.get("name", "")).strip()
    arguments = params.get("arguments") or {}
    if not name or not isinstance(arguments, dict):
        return _jsonrpc_error(request_id, -32602, "tools/call requires name and object arguments")
    context = ToolExecutionContext(tenant_id=session.tenant_id, thread_id=session.thread_id)
    try:
        result = await session.tool_registry.execute(name, arguments, context=context)
    except HTTPException as exc:
        logger.warning(
            "mcp_broker.tool_call_error session_id=%s tenant_id=%s thread_id=%s tool=%s detail=%s",
            session.session_id,
            session.tenant_id,
            session.thread_id,
            name,
            exc.detail,
        )
        return _jsonrpc_error(request_id, -32000, str(exc.detail))
    logger.info(
        "mcp_broker.tool_call session_id=%s tenant_id=%s thread_id=%s tool=%s",
        session.session_id,
        session.tenant_id,
        session.thread_id,
        name,
    )
    return _jsonrpc_result(
        request_id,
        {
            "structuredContent": result,
            "content": [{"type": "text", "text": _tool_result_text(result)}],
        },
    )


def _tool_result_text(result: object) -> str:
    if isinstance(result, str):
        return result
    return str(result)


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _jsonrpc_result(request_id: object, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
