from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

import anyio
from fastapi import HTTPException, Request, Response
from mcp import types as mcp_types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from app.mcp import (
    LEGACY_MCP_PROTOCOL_VERSION,
    MODERN_MCP_PROTOCOL_VERSION,
    mcp_jsonrpc_error,
    mcp_request_protocol_version,
    strip_modern_mcp_result_envelope,
)
from app.models import Principal
from app.tools import ToolExecutionContext, ToolRegistry

MINIGENT_MCP_BROKER_BASE_URL_ENV = "MINIGENT_MCP_BROKER_BASE_URL"
MINIGENT_MCP_BROKER_URL_ENV = "MINIGENT_MCP_BROKER_URL"
MINIGENT_MCP_BROKER_TOKEN_ENV = "MINIGENT_MCP_BROKER_TOKEN"
MINIGENT_MCP_BROKER_SESSION_ENV = "MINIGENT_MCP_BROKER_SESSION"
MINIGENT_MCP_BROKER_DB_PATH_ENV = "MINIGENT_MCP_BROKER_DB_PATH"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MCPBrokerSession:
    session_id: str
    token: str = field(repr=False)
    tenant_id: str
    user_id: str
    thread_id: str
    allowed_tool_names: tuple[str, ...]
    expires_at: float
    tool_registry: ToolRegistry | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MCPBrokerStoreSettings:
    db_path: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MCPBrokerStoreSettings:
        lookup = os.environ if env is None else env
        db_path = lookup.get(MINIGENT_MCP_BROKER_DB_PATH_ENV, "").strip()
        return cls(db_path=db_path or None)


ToolRegistryResolver = Callable[[MCPBrokerSession], ToolRegistry]


class MCPBrokerSessionStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else None
        self._sessions: dict[str, MCPBrokerSession] = {}
        self._registries: dict[str, ToolRegistry] = {}
        self._lock = Lock()
        if self._db_path is not None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

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
        allowed_tool_names = tuple(spec.name for spec in tool_registry.specs())
        session = MCPBrokerSession(
            session_id=session_id,
            token=token,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            thread_id=thread_id,
            allowed_tool_names=allowed_tool_names,
            tool_registry=tool_registry,
            expires_at=time.time() + ttl_seconds,
        )
        with self._lock:
            self._registries[session_id] = tool_registry
            if self._db_path is None:
                self._sessions[session_id] = session
            else:
                with self._connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO mcp_broker_sessions (
                          session_id, token_hash, tenant_id, user_id, thread_id,
                          allowed_tool_names_json, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            _token_hash(token),
                            principal.tenant_id,
                            principal.user_id,
                            thread_id,
                            json.dumps(allowed_tool_names),
                            session.expires_at,
                        ),
                    )
        logger.info(
            "mcp_broker.session_created session_id=%s tenant_id=%s thread_id=%s tools=%s",
            session_id,
            principal.tenant_id,
            thread_id,
            len(allowed_tool_names),
        )
        return session

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._registries.pop(session_id, None)
            if self._db_path is None:
                session = self._sessions.pop(session_id, None)
            else:
                with self._connection() as conn:
                    row = conn.execute(
                        "SELECT tenant_id, thread_id FROM mcp_broker_sessions WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    conn.execute(
                        "DELETE FROM mcp_broker_sessions WHERE session_id = ?", (session_id,)
                    )
                session = (
                    MCPBrokerSession(
                        session_id=session_id,
                        token="",
                        tenant_id=str(row[0]),
                        user_id="",
                        thread_id=str(row[1]),
                        allowed_tool_names=(),
                        expires_at=0,
                    )
                    if row is not None
                    else None
                )
        if session is not None:
            logger.info(
                "mcp_broker.session_deleted session_id=%s tenant_id=%s thread_id=%s",
                session_id,
                session.tenant_id,
                session.thread_id,
            )

    def require_session(self, session_id: str, token: str | None) -> MCPBrokerSession:
        self._prune_expired()
        with self._lock:
            if self._db_path is None:
                session = self._sessions.get(session_id)
                token_hash = _token_hash(session.token) if session is not None else ""
            else:
                with self._connection() as conn:
                    row = conn.execute(
                        """
                        SELECT token_hash, tenant_id, user_id, thread_id,
                               allowed_tool_names_json, expires_at
                        FROM mcp_broker_sessions WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                if row is None:
                    session = None
                    token_hash = ""
                else:
                    token_hash = str(row[0])
                    allowed = json.loads(row[4])
                    session = MCPBrokerSession(
                        session_id=session_id,
                        token="",
                        tenant_id=str(row[1]),
                        user_id=str(row[2]),
                        thread_id=str(row[3]),
                        allowed_tool_names=tuple(str(name) for name in allowed),
                        expires_at=float(row[5]),
                        tool_registry=self._registries.get(session_id),
                    )
        if session is None:
            raise HTTPException(status_code=404, detail="MCP broker session not found")
        supplied_hash = _token_hash(token) if token else ""
        if not token or not secrets.compare_digest(supplied_hash, token_hash):
            raise HTTPException(status_code=401, detail="Invalid MCP broker token")
        return session

    def _prune_expired(self) -> None:
        now = time.time()
        with self._lock:
            if self._db_path is None:
                expired = [
                    session_id
                    for session_id, session in self._sessions.items()
                    if session.expires_at <= now
                ]
                for session_id in expired:
                    self._sessions.pop(session_id, None)
                    self._registries.pop(session_id, None)
                return
            with self._connection() as conn:
                rows = conn.execute(
                    "SELECT session_id FROM mcp_broker_sessions WHERE expires_at <= ?", (now,)
                ).fetchall()
                conn.execute("DELETE FROM mcp_broker_sessions WHERE expires_at <= ?", (now,))
            for row in rows:
                self._registries.pop(str(row[0]), None)

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS mcp_broker_sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    allowed_tool_names_json TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mcp_broker_sessions_expires
                    ON mcp_broker_sessions (expires_at);
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._db_path is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("MCP broker SQLite path is not configured")
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def build_mcp_broker_session_store_from_env(
    env: Mapping[str, str] | None = None,
) -> MCPBrokerSessionStore:
    settings = MCPBrokerStoreSettings.from_env(env)
    return MCPBrokerSessionStore(settings.db_path)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def handle_mcp_broker_request(
    *,
    session_store: MCPBrokerSessionStore,
    session_id: str,
    request: Request,
    tool_registry_resolver: ToolRegistryResolver | None = None,
) -> Response | dict[str, object]:
    token = _bearer_token(request) or request.headers.get("x-minigent-mcp-broker-token")
    session = session_store.require_session(session_id, token)
    tool_registry = session.tool_registry
    if tool_registry is None and tool_registry_resolver is not None:
        tool_registry = tool_registry_resolver(session)
    if tool_registry is None:
        raise HTTPException(status_code=503, detail="MCP broker tool registry is unavailable")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="MCP broker request must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="MCP broker request must be a JSON object")

    request_id = payload.get("id")
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return mcp_jsonrpc_error(request_id, -32600, "Invalid Request")
    if "id" not in payload:
        return Response(status_code=202)
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return mcp_jsonrpc_error(request_id, -32602, "Invalid params")

    is_modern_request = (
        method == "server/discover"
        or mcp_request_protocol_version(payload) == MODERN_MCP_PROTOCOL_VERSION
    )
    sdk_payload = _broker_sdk_payload(payload, method=method, params=params)
    sdk_server = _build_broker_sdk_server(session, tool_registry)
    response_payload = await _run_broker_sdk_request(sdk_server, sdk_payload)
    if not is_modern_request and method in {"tools/list", "tools/call"}:
        response_payload = strip_modern_mcp_result_envelope(response_payload)
    return response_payload


def _broker_sdk_payload(
    payload: dict[str, Any],
    *,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    sdk_payload = dict(payload)
    sdk_params = dict(params)
    if method == "initialize":
        sdk_params.setdefault("protocolVersion", LEGACY_MCP_PROTOCOL_VERSION)
        sdk_params.setdefault("capabilities", {})
        sdk_params.setdefault(
            "clientInfo",
            {"name": "minigent-mcp-broker-http-client", "version": "0.1.0"},
        )
    else:
        raw_meta = sdk_params.get("_meta")
        metadata = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        metadata.setdefault("io.modelcontextprotocol/protocolVersion", MODERN_MCP_PROTOCOL_VERSION)
        metadata.setdefault(
            "io.modelcontextprotocol/clientInfo",
            {"name": "minigent-mcp-broker-http-client", "version": "0.1.0"},
        )
        metadata.setdefault("io.modelcontextprotocol/clientCapabilities", {})
        sdk_params["_meta"] = metadata
    sdk_payload["params"] = sdk_params
    return sdk_payload


def _build_broker_sdk_server(
    session: MCPBrokerSession,
    tool_registry: ToolRegistry,
) -> Server[Any]:
    sdk_server: Server[Any] = Server("minigent-mcp-broker", version="0.1.0")

    async def list_tools(
        _context: ServerRequestContext[Any, Any],
        _params: mcp_types.PaginatedRequestParams,
    ) -> mcp_types.ListToolsResult:
        tools = [
            mcp_types.Tool(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
            )
            for spec in tool_registry.specs()
            if spec.name in session.allowed_tool_names
        ]
        logger.info(
            "mcp_broker.tools_list session_id=%s tenant_id=%s thread_id=%s tools=%s",
            session.session_id,
            session.tenant_id,
            session.thread_id,
            len(tools),
        )
        return mcp_types.ListToolsResult(tools=tools)

    async def call_tool(
        _context: ServerRequestContext[Any, Any],
        params: mcp_types.CallToolRequestParams,
    ) -> mcp_types.CallToolResult:
        name = params.name.strip()
        arguments = params.arguments or {}
        if name not in session.allowed_tool_names:
            raise MCPError(code=-32602, message=f"Unknown tool '{name}'")
        context = ToolExecutionContext(
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            thread_id=session.thread_id,
        )
        try:
            result = await tool_registry.execute(name, arguments, context=context)
        except HTTPException as exc:
            logger.warning(
                "mcp_broker.tool_call_error session_id=%s tenant_id=%s thread_id=%s "
                "tool=%s detail=%s",
                session.session_id,
                session.tenant_id,
                session.thread_id,
                name,
                exc.detail,
            )
            raise MCPError(code=-32000, message=str(exc.detail)) from exc
        logger.info(
            "mcp_broker.tool_call session_id=%s tenant_id=%s thread_id=%s tool=%s",
            session.session_id,
            session.tenant_id,
            session.thread_id,
            name,
        )
        return mcp_types.CallToolResult(
            structured_content=result,
            content=[mcp_types.TextContent(text=_tool_result_text(result))],
        )

    sdk_server.add_request_handler(
        "tools/list",
        mcp_types.PaginatedRequestParams,
        list_tools,
    )
    sdk_server.add_request_handler(
        "tools/call",
        mcp_types.CallToolRequestParams,
        call_tool,
    )
    return sdk_server


async def _run_broker_sdk_request(
    sdk_server: Server[Any],
    payload: dict[str, Any],
) -> dict[str, object]:
    try:
        message = mcp_types.jsonrpc_message_adapter.validate_python(payload, by_name=False)
    except ValueError:
        return mcp_jsonrpc_error(payload.get("id"), -32600, "Invalid Request")
    if not isinstance(message, mcp_types.JSONRPCRequest):
        return mcp_jsonrpc_error(payload.get("id"), -32600, "Invalid Request")

    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)
    response: SessionMessage | None = None
    async with (
        read_writer,
        read_stream,
        write_stream,
        write_reader,
        anyio.create_task_group() as task_group,
    ):
        task_group.start_soon(
            sdk_server.run,
            read_stream,
            write_stream,
            sdk_server.create_initialization_options(),
        )
        await read_writer.send(SessionMessage(message))
        response = await write_reader.receive()
        await read_writer.aclose()
        task_group.cancel_scope.cancel()

    if response is None:  # pragma: no cover - receive() either returns or raises
        return mcp_jsonrpc_error(payload.get("id"), -32603, "MCP SDK returned no response")
    response_message = response.message
    if not isinstance(response_message, (mcp_types.JSONRPCResponse, mcp_types.JSONRPCError)):
        return mcp_jsonrpc_error(payload.get("id"), -32603, "Invalid MCP SDK response")
    return response_message.model_dump(by_alias=True, mode="json", exclude_none=True)


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
