"""Authenticated, read-only MCP tools for the active tenant user."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from mcp.server import MCPServer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import require_principal
from app.execution import redact_tenant_execution_payload
from app.models import Principal
from app.tenants import require_active_tenant_principal
from app.user_execution import effective_execution_catalog, validate_user_execution_config


@dataclass(frozen=True)
class UserMCPRequestContext:
    principal: Principal
    app: Any


_request_context: ContextVar[UserMCPRequestContext | None] = ContextVar(
    "minigent_user_mcp_request_context",
    default=None,
)


def current_user_mcp_request_context() -> UserMCPRequestContext:
    context = _request_context.get()
    if context is None:
        raise RuntimeError("MCP tool called outside an authenticated user request")
    return context


class UserMCPAuthMiddleware:
    """Authenticate a user MCP request and require active tenant membership."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    @staticmethod
    async def _authenticate(request: Request) -> Principal:
        principal = await require_principal(
            request,
            authorization=request.headers.get("Authorization"),
            x_minigent_user_id=request.headers.get("X-Minigent-User-Id"),
            x_minigent_tenant_id=request.headers.get("X-Minigent-Tenant-Id"),
            x_minigent_admin=request.headers.get("X-Minigent-Admin"),
        )
        if principal.is_admin:
            raise HTTPException(
                status_code=403, detail="User MCP access is not available to admins"
            )
        return await require_active_tenant_principal(request, principal)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        try:
            principal = await self._authenticate(request)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                scope, receive, send
            )
            return
        token = _request_context.set(UserMCPRequestContext(principal=principal, app=scope["app"]))
        try:
            await self._app(scope, receive, send)
        finally:
            _request_context.reset(token)


def _context() -> UserMCPRequestContext:
    return current_user_mcp_request_context()


def _store_or_raise(app: Any) -> Any:
    store = getattr(app.state, "admin_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="User execution config storage is not configured"
        )
    return store


def _safe_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    return value


def get_user_execution_status(app: Any, principal: Principal) -> dict[str, object]:
    store = _store_or_raise(app)
    record = store.get_user_execution_config(principal.tenant_id, principal.user_id)
    credentials_encrypted = bool(getattr(store, "user_execution_credentials_encrypted", False))
    findings: list[dict[str, str]] = []
    if record is None:
        findings.append(
            {
                "code": "user_execution.unconfigured",
                "severity": "info",
                "message": "No personal execution configuration is stored.",
                "remediation": "Create a personal execution configuration if needed.",
            }
        )
    if not credentials_encrypted:
        findings.append(
            {
                "code": "user_execution.credentials_unavailable",
                "severity": "warning",
                "message": "Encrypted personal MCP credential storage is unavailable.",
                "remediation": "Configure encrypted admin storage before using credential references.",
            }
        )
    counts = {"skills": 0, "mcp_servers": 0, "capability_profiles": 0, "agents": 0}
    if record is not None:
        config = record.config
        for key in counts:
            collection = config.get(key, {})
            if isinstance(collection, dict) and isinstance(collection.get("items"), list):
                counts[key] = len(collection["items"])
    return {
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "execution_configured": record is not None,
        "execution_config_version": record.version if record is not None else None,
        "encrypted_credentials_available": credentials_encrypted,
        **counts,
        "findings": findings,
    }


def get_user_execution_config(app: Any, principal: Principal) -> dict[str, object]:
    store = _store_or_raise(app)
    record = store.get_user_execution_config(principal.tenant_id, principal.user_id)
    if record is None:
        raise HTTPException(status_code=404, detail="User execution config not found")
    return {
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
        "config": redact_tenant_execution_payload(_safe_payload(record.config)),
        "version": record.version,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def validate_user_execution_config_for_mcp(
    config: dict[str, Any],
) -> dict[str, object]:
    report = validate_user_execution_config(config)
    return {
        "valid": report.valid,
        "errors": report.errors,
        "normalized_config": (
            report.config.model_dump(mode="json", exclude_none=True)
            if report.config is not None
            else None
        ),
    }


def _server_summary(server: Any, *, source: str) -> dict[str, object]:
    return {
        "id": server.id if source == "user" else server.name,
        "name": server.name,
        "source": source,
        "allowed_tools": list(server.allowed_tools) if server.allowed_tools is not None else None,
        "credential_configured": bool(source == "user" and getattr(server, "credential_ref", None)),
    }


def list_user_mcp_access(app: Any, principal: Principal) -> dict[str, object]:
    store = _store_or_raise(app)
    execution = app.state.execution_resolver.resolve(principal.tenant_id)
    catalog = effective_execution_catalog(
        execution.config,
        store,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
    )
    personal = [
        _server_summary(server, source="user")
        for server in (catalog.user_config.mcp_servers.items if catalog.user_config else [])
    ]
    shared_servers = list(catalog.tenant_config.tools.mcp_servers)
    item_ids = store.effective_subject_mcp_server_catalog_item_ids(
        principal.tenant_id, principal.user_id
    )
    catalog_items = getattr(app.state.admin_store_settings, "mcp_server_catalog", ())
    if item_ids is not None:
        allowed_names = {
            item.server.get("name")
            for item in catalog_items
            if item.id in set(item_ids) and isinstance(item.server.get("name"), str)
        }
        shared_servers = [server for server in shared_servers if server.name in allowed_names]
    shared = [_server_summary(server, source="shared") for server in shared_servers]
    return {
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "personal_mcp_servers_allowed": catalog.personal_mcp_servers_allowed,
        "personal_servers": personal,
        "shared_servers": shared,
    }


def build_user_mcp_server() -> MCPServer[Any]:
    server = MCPServer(
        "Minigent User Operations",
        instructions=(
            "This server is read-only. It reports the authenticated user's redacted execution "
            "configuration and effective MCP access. Never request or return secrets, credential "
            "values, or authorization headers."
        ),
    )

    @server.tool(name="get_user_execution_status")
    def mcp_get_user_execution_status() -> dict[str, object]:
        context = _context()
        return get_user_execution_status(context.app, context.principal)

    @server.tool(name="get_user_execution_config")
    def mcp_get_user_execution_config() -> dict[str, object]:
        context = _context()
        return get_user_execution_config(context.app, context.principal)

    @server.tool(name="validate_user_execution_config")
    def mcp_validate_user_execution_config(config: dict[str, Any]) -> dict[str, object]:
        return validate_user_execution_config_for_mcp(config)

    @server.tool(name="list_user_mcp_access")
    def mcp_list_user_mcp_access() -> dict[str, object]:
        context = _context()
        return list_user_mcp_access(context.app, context.principal)

    return server
