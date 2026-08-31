"""Authenticated, principal-scoped MCP tools for the active tenant user."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from fastapi import HTTPException, Request
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.admin_store import (
    UserExecutionConfigConflictError,
    UserExecutionCredentialConflictError,
)
from app.auth import (
    ADMIN_HEADER,
    LEGACY_ADMIN_HEADER,
    LEGACY_TENANT_HEADER,
    LEGACY_USER_HEADER,
    TENANT_HEADER,
    USER_HEADER,
    require_principal,
)
from app.execution import redact_tenant_execution_payload
from app.models import AuditRecord, Principal
from app.tenants import require_active_tenant_principal
from app.tools import ToolRegistry
from app.user_execution import ensure_default_personal_agent, validate_user_execution_config
from app.user_execution_api import (
    UserExecutionConfigPutRequest,
    UserExecutionCredentialPutRequest,
    _validate_credential_ref,
)
from app.user_mcp_access import (
    get_user_execution_status,
    list_user_mcp_access,
)


@dataclass(frozen=True)
class UserMCPRequestContext:
    principal: Principal
    app: Any


_request_context: ContextVar[UserMCPRequestContext | None] = ContextVar(
    "mindweft_user_mcp_request_context",
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
            x_mindweft_user_id=request.headers.get(USER_HEADER),
            x_mindweft_tenant_id=request.headers.get(TENANT_HEADER),
            x_mindweft_admin=request.headers.get(ADMIN_HEADER),
            x_minigent_user_id=request.headers.get(LEGACY_USER_HEADER),
            x_minigent_tenant_id=request.headers.get(LEGACY_TENANT_HEADER),
            x_minigent_admin=request.headers.get(LEGACY_ADMIN_HEADER),
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
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _call_mcp_tool(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    """Expose expected HTTP failures to MCP clients without leaking unexpected exceptions."""
    try:
        return operation()
    except HTTPException as exc:
        message = json.dumps(
            {"status_code": exc.status_code, "detail": _safe_payload(exc.detail)},
            sort_keys=True,
        )
        raise ToolError(message) from exc


def get_user_execution_config(app: Any, principal: Principal) -> dict[str, object]:
    store = _store_or_raise(app)
    record = store.get_user_execution_config(principal.tenant_id, principal.user_id)
    if record is None:
        raise HTTPException(status_code=404, detail="User execution config not found")
    return _safe_payload(
        {
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "config": redact_tenant_execution_payload(_safe_payload(record.config)),
            "version": record.version,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
    )


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


def _audit_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(
                part in key_text.casefold()
                for part in ("token", "secret", "key", "authorization", "password", "value")
            ):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = _audit_payload(item)
        return result
    if isinstance(value, list):
        return [_audit_payload(item) for item in value]
    return value


def _append_user_audit(
    app: Any,
    principal: Principal,
    action: str,
    *,
    resource_type: str,
    resource_id: str,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
) -> None:
    app.state.store.append_audit_record(
        AuditRecord(
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            action=action,
            affected_count=1,
            resource_type=resource_type,
            resource_id=resource_id,
            old_values=_audit_payload(old_values),
            new_values=_audit_payload(new_values),
        )
    )


def _require_confirmation(arguments: dict[str, Any]) -> None:
    if arguments.get("confirm") is not True:
        raise HTTPException(
            status_code=400,
            detail="This destructive operation requires confirm=true",
        )


def _config_record_response(record: Any) -> dict[str, object]:
    return _safe_payload(
        {
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "config": redact_tenant_execution_payload(_safe_payload(record.config)),
            "version": record.version,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
    )


def put_user_execution_config(
    app: Any,
    principal: Principal,
    arguments: dict[str, Any],
) -> dict[str, object]:
    request = UserExecutionConfigPutRequest.model_validate(arguments)
    report = validate_user_execution_config(request.config)
    if not report.valid or report.config is None:
        raise HTTPException(
            status_code=422,
            detail={"message": "Invalid user execution config", "errors": report.errors},
        )
    store = _store_or_raise(app)
    old_record = store.get_user_execution_config(principal.tenant_id, principal.user_id)
    try:
        record = store.upsert_user_execution_config(
            principal.tenant_id,
            principal.user_id,
            ensure_default_personal_agent(report.config).model_dump(mode="json", exclude_none=True),
            expected_version=request.expected_version,
        )
    except UserExecutionConfigConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "User execution config version conflict",
                "expected_version": exc.expected_version,
                "actual_version": exc.actual_version,
            },
        ) from exc
    _append_user_audit(
        app,
        principal,
        "user_execution_config.put",
        resource_type="user_execution_config",
        resource_id=f"{principal.tenant_id}/{principal.user_id}",
        old_values=(old_record.config if old_record is not None else None),
        new_values=record.config,
    )
    return _config_record_response(record)


def delete_user_execution_config(
    app: Any,
    principal: Principal,
    arguments: dict[str, Any],
) -> dict[str, object]:
    _require_confirmation(arguments)
    expected_version = arguments.get("expected_version")
    if expected_version is not None and (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 0
    ):
        raise HTTPException(
            status_code=400, detail="expected_version must be a non-negative integer"
        )
    store = _store_or_raise(app)
    old_record = store.get_user_execution_config(principal.tenant_id, principal.user_id)
    try:
        deleted = store.delete_user_execution_config(
            principal.tenant_id,
            principal.user_id,
            expected_version=expected_version,
        )
    except UserExecutionConfigConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "User execution config version conflict",
                "expected_version": exc.expected_version,
                "actual_version": exc.actual_version,
            },
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="User execution config not found")
    _append_user_audit(
        app,
        principal,
        "user_execution_config.delete",
        resource_type="user_execution_config",
        resource_id=f"{principal.tenant_id}/{principal.user_id}",
        old_values=(old_record.config if old_record is not None else None),
    )
    return {"deleted": True, "tenant_id": principal.tenant_id, "user_id": principal.user_id}


def put_user_execution_credential(
    app: Any,
    principal: Principal,
    credential_ref: str,
    arguments: dict[str, Any],
) -> dict[str, object]:
    _validate_credential_ref(credential_ref)
    request = UserExecutionCredentialPutRequest.model_validate(arguments)
    store = _store_or_raise(app)
    if not store.user_execution_credentials_encrypted:
        raise HTTPException(
            status_code=503,
            detail="Encrypted user execution credential storage is not configured",
        )
    old_record = store.get_user_execution_credential(
        principal.tenant_id, principal.user_id, credential_ref
    )
    try:
        record = store.upsert_user_execution_credential(
            principal.tenant_id,
            principal.user_id,
            credential_ref,
            header_name=request.header_name,
            header_value=request.header_value,
            expected_version=request.expected_version,
        )
    except UserExecutionCredentialConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "User execution credential version conflict",
                "expected_version": exc.expected_version,
                "actual_version": exc.actual_version,
            },
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _append_user_audit(
        app,
        principal,
        "user_execution_credential.put",
        resource_type="user_execution_credential",
        resource_id=f"{principal.tenant_id}/{principal.user_id}/{credential_ref}",
        old_values=(
            {"credential_ref": old_record.credential_ref, "header_name": old_record.header_name}
            if old_record is not None
            else None
        ),
        new_values={"credential_ref": record.credential_ref, "header_name": record.header_name},
    )
    return _safe_payload(
        {
            "tenant_id": record.tenant_id,
            "user_id": record.user_id,
            "credential_ref": record.credential_ref,
            "header_name": record.header_name,
            "version": record.version,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
    )


def delete_user_execution_credential(
    app: Any,
    principal: Principal,
    credential_ref: str,
    arguments: dict[str, Any],
) -> dict[str, object]:
    _require_confirmation(arguments)
    _validate_credential_ref(credential_ref)
    expected_version = arguments.get("expected_version")
    if expected_version is not None and (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 0
    ):
        raise HTTPException(
            status_code=400, detail="expected_version must be a non-negative integer"
        )
    store = _store_or_raise(app)
    if not store.user_execution_credentials_encrypted:
        raise HTTPException(
            status_code=503,
            detail="Encrypted user execution credential storage is not configured",
        )
    old_record = store.get_user_execution_credential(
        principal.tenant_id, principal.user_id, credential_ref
    )
    try:
        deleted = store.delete_user_execution_credential(
            principal.tenant_id,
            principal.user_id,
            credential_ref,
            expected_version=expected_version,
        )
    except UserExecutionCredentialConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "User execution credential version conflict",
                "expected_version": exc.expected_version,
                "actual_version": exc.actual_version,
            },
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="User execution credential not found")
    _append_user_audit(
        app,
        principal,
        "user_execution_credential.delete",
        resource_type="user_execution_credential",
        resource_id=f"{principal.tenant_id}/{principal.user_id}/{credential_ref}",
        old_values=(
            {"credential_ref": old_record.credential_ref, "header_name": old_record.header_name}
            if old_record is not None
            else None
        ),
    )
    return {
        "deleted": True,
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "credential_ref": credential_ref,
    }


def build_user_mcp_tool_registry(app: Any, principal: Principal) -> ToolRegistry:
    """Build principal-scoped user-MCP tools for in-process agent execution."""
    registry = ToolRegistry()
    prefix = "mindweft_user_mcp"

    def register(name: str, description: str, schema: dict[str, Any], handler: Any) -> None:
        registry.register(
            name=f"{prefix}.{name}",
            description=description,
            input_schema=schema,
            handler=lambda arguments, context=None: handler(arguments),
        )

    register(
        "get_user_execution_status",
        "Get the authenticated user's execution and personal MCP status.",
        {"type": "object", "properties": {}},
        lambda _: get_user_execution_status(app, principal),
    )
    register(
        "get_user_execution_config",
        "Get the authenticated user's redacted execution configuration.",
        {"type": "object", "properties": {}},
        lambda _: get_user_execution_config(app, principal),
    )
    register(
        "validate_user_execution_config",
        "Validate a proposed personal execution configuration without saving it.",
        {"type": "object", "properties": {"config": {"type": "object"}}, "required": ["config"]},
        lambda arguments: validate_user_execution_config_for_mcp(arguments["config"]),
    )
    register(
        "list_user_mcp_access",
        "List the authenticated user's effective personal and shared MCP access.",
        {"type": "object", "properties": {}},
        lambda _: list_user_mcp_access(app, principal),
    )
    register(
        "put_user_execution_config",
        "Save the authenticated user's personal execution configuration.",
        {
            "type": "object",
            "properties": {
                "config": {"type": "object"},
                "expected_version": {"type": ["integer", "null"]},
            },
            "required": ["config"],
        },
        lambda arguments: put_user_execution_config(app, principal, arguments),
    )
    register(
        "delete_user_execution_config",
        "Delete the authenticated user's personal execution configuration.",
        {
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean"},
                "expected_version": {"type": ["integer", "null"]},
            },
            "required": ["confirm"],
        },
        lambda arguments: delete_user_execution_config(app, principal, arguments),
    )
    register(
        "put_user_execution_credential",
        "Store or rotate a write-only personal MCP credential header.",
        {
            "type": "object",
            "properties": {
                "credential_ref": {"type": "string"},
                "header_name": {"type": "string"},
                "header_value": {"type": "string"},
                "expected_version": {"type": ["integer", "null"]},
            },
            "required": ["credential_ref", "header_name", "header_value"],
        },
        lambda arguments: put_user_execution_credential(
            app, principal, arguments["credential_ref"], arguments
        ),
    )
    register(
        "delete_user_execution_credential",
        "Delete a write-only personal MCP credential after confirmation.",
        {
            "type": "object",
            "properties": {
                "credential_ref": {"type": "string"},
                "confirm": {"type": "boolean"},
                "expected_version": {"type": ["integer", "null"]},
            },
            "required": ["credential_ref", "confirm"],
        },
        lambda arguments: delete_user_execution_credential(
            app, principal, arguments["credential_ref"], arguments
        ),
    )
    registry.set_mcp_servers(
        [
            {
                "name": prefix,
                "status": "connected",
                "tool_count": len(registry.specs()),
                "builtin": True,
            }
        ]
    )
    return registry


def build_user_mcp_server() -> MCPServer[Any]:
    server = MCPServer(
        "Mindweft User Operations",
        instructions=(
            "This server is principal-scoped. It reports the authenticated user's redacted execution "
            "configuration and effective MCP access, and may update the user's own configuration. "
            "Never request or return secrets, credential values, or authorization headers."
        ),
    )

    @server.tool(name="get_user_execution_status")
    def mcp_get_user_execution_status() -> dict[str, object]:
        context = _context()
        return _call_mcp_tool(lambda: get_user_execution_status(context.app, context.principal))

    @server.tool(name="get_user_execution_config")
    def mcp_get_user_execution_config() -> dict[str, object]:
        context = _context()
        return _call_mcp_tool(lambda: get_user_execution_config(context.app, context.principal))

    @server.tool(name="validate_user_execution_config")
    def mcp_validate_user_execution_config(config: dict[str, Any]) -> dict[str, object]:
        return _call_mcp_tool(lambda: validate_user_execution_config_for_mcp(config))

    @server.tool(name="list_user_mcp_access")
    def mcp_list_user_mcp_access() -> dict[str, object]:
        context = _context()
        return _call_mcp_tool(lambda: list_user_mcp_access(context.app, context.principal))

    @server.tool(name="put_user_execution_config")
    def mcp_put_user_execution_config(
        config: dict[str, Any], expected_version: int | None = None
    ) -> dict[str, object]:
        context = _context()
        return _call_mcp_tool(
            lambda: put_user_execution_config(
                context.app,
                context.principal,
                {"config": config, "expected_version": expected_version},
            )
        )

    @server.tool(name="delete_user_execution_config")
    def mcp_delete_user_execution_config(
        confirm: bool, expected_version: int | None = None
    ) -> dict[str, object]:
        context = _context()
        return _call_mcp_tool(
            lambda: delete_user_execution_config(
                context.app,
                context.principal,
                {"confirm": confirm, "expected_version": expected_version},
            )
        )

    @server.tool(name="put_user_execution_credential")
    def mcp_put_user_execution_credential(
        credential_ref: str,
        header_name: str,
        header_value: str,
        expected_version: int | None = None,
    ) -> dict[str, object]:
        context = _context()
        return _call_mcp_tool(
            lambda: put_user_execution_credential(
                context.app,
                context.principal,
                credential_ref,
                {
                    "header_name": header_name,
                    "header_value": header_value,
                    "expected_version": expected_version,
                },
            )
        )

    @server.tool(name="delete_user_execution_credential")
    def mcp_delete_user_execution_credential(
        credential_ref: str,
        confirm: bool,
        expected_version: int | None = None,
    ) -> dict[str, object]:
        context = _context()
        return _call_mcp_tool(
            lambda: delete_user_execution_credential(
                context.app,
                context.principal,
                credential_ref,
                {"confirm": confirm, "expected_version": expected_version},
            )
        )

    return server
