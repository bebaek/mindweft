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

from fastapi import HTTPException, Request, Response

from app.mcp import (
    LEGACY_MCP_PROTOCOL_VERSION,
    MODERN_MCP_PROTOCOL_VERSION,
    build_mcp_discover_result,
    mcp_request_protocol_version,
    stamp_modern_mcp_result,
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
    method = str(payload.get("method", ""))
    params = payload.get("params") or {}
    is_modern_request = mcp_request_protocol_version(payload) == MODERN_MCP_PROTOCOL_VERSION
    if params is not None and not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32602, "Invalid params")

    if method == "server/discover":
        return _jsonrpc_result(
            request_id,
            build_mcp_discover_result(
                server_name="minigent-mcp-broker",
                capabilities={"tools": {}},
            ),
        )
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": LEGACY_MCP_PROTOCOL_VERSION,
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
            len(session.allowed_tool_names),
        )
        result: dict[str, object] = {
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": spec.input_schema,
                }
                for spec in tool_registry.specs()
                if spec.name in session.allowed_tool_names
            ]
        }
        if is_modern_request:
            result.update({"ttlMs": 0, "cacheScope": "private"})
            result = stamp_modern_mcp_result(
                result,
                server_name="minigent-mcp-broker",
            )
        return _jsonrpc_result(request_id, result)
    if method == "tools/call":
        return await _call_tool(
            session,
            tool_registry,
            request_id,
            params,
            modern=is_modern_request,
        )
    return _jsonrpc_error(request_id, -32601, f"Unsupported MCP method '{method}'")


async def _call_tool(
    session: MCPBrokerSession,
    tool_registry: ToolRegistry,
    request_id: object,
    params: dict[str, Any],
    *,
    modern: bool = False,
) -> dict[str, object]:
    name = str(params.get("name", "")).strip()
    arguments = params.get("arguments") or {}
    if not name or not isinstance(arguments, dict):
        return _jsonrpc_error(request_id, -32602, "tools/call requires name and object arguments")
    if name not in session.allowed_tool_names:
        return _jsonrpc_error(request_id, -32602, f"Unknown tool '{name}'")
    context = ToolExecutionContext(
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        thread_id=session.thread_id,
    )
    try:
        result = await tool_registry.execute(name, arguments, context=context)
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
    response_result: dict[str, object] = {
        "structuredContent": result,
        "content": [{"type": "text", "text": _tool_result_text(result)}],
    }
    if modern:
        response_result = stamp_modern_mcp_result(
            response_result,
            server_name="minigent-mcp-broker",
        )
    return _jsonrpc_result(request_id, response_result)


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
