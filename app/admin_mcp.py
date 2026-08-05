"""Role-scoped Minigent administration tools and the external admin MCP surface."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from mcp.server import MCPServer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import require_principal
from app.execution import ADMIN_EXECUTION_CONFIG_KEY
from app.health import database_readiness_checks
from app.models import Principal
from app.tools import ToolExecutionContext, ToolRegistry

ADMIN_CHAT_SETUP_TOOL = "minigent_admin_get_setup_status"
ADMIN_CHAT_TENANT_TOOL = "minigent_admin_diagnose_tenant_setup"
ADMIN_CHAT_CATALOG_TOOL = "minigent_admin_list_mcp_server_catalog_access"


@dataclass(frozen=True, slots=True)
class AdminMCPRequestContext:
    principal: Principal
    app: Any


_request_context: ContextVar[AdminMCPRequestContext | None] = ContextVar(
    "minigent_admin_mcp_request_context",
    default=None,
)


def current_admin_mcp_request_context() -> AdminMCPRequestContext:
    context = _request_context.get()
    if context is None:
        raise RuntimeError("MCP tool called outside an authenticated admin request")
    return context


class AdminMCPAuthMiddleware:
    """Authenticate every MCP request using Minigent's normal principal mechanism."""

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
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        return principal

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
        token = _request_context.set(AdminMCPRequestContext(principal=principal, app=scope["app"]))
        try:
            await self._app(scope, receive, send)
        finally:
            _request_context.reset(token)


def _catalog_item_summary(item: Any) -> dict[str, object]:
    server = item.server if hasattr(item, "server") else {}
    allowed_tools = server.get("allowed_tools", server.get("allowedTools"))
    return {
        "id": item.id,
        "name": server.get("name"),
        "transport": "http" if isinstance(server.get("url"), str) else "unknown",
        "allowed_tools": allowed_tools if isinstance(allowed_tools, list) else None,
    }


async def get_setup_status(app: Any) -> dict[str, object]:
    """Return redacted deployment readiness and the next safe remediation steps."""
    checks = await database_readiness_checks()
    resolver_description = app.state.execution_resolver.describe(include_export=False)
    admin_store_configured = app.state.admin_store is not None
    admin_execution_configured = (
        app.state.admin_store.get_raw_config(ADMIN_EXECUTION_CONFIG_KEY) is not None
        if admin_store_configured
        else False
    )
    catalog = getattr(app.state.admin_store_settings, "mcp_server_catalog", ())
    findings: list[dict[str, str]] = []
    for name, healthy in checks.items():
        if not healthy:
            findings.append(
                {
                    "code": f"readiness.{name}",
                    "severity": "error",
                    "message": f"Readiness check '{name}' is failing.",
                    "remediation": "Inspect the deployment configuration and service logs.",
                }
            )
    if not admin_store_configured:
        findings.append(
            {
                "code": "admin_store.unconfigured",
                "severity": "warning",
                "message": "Durable admin configuration is not configured.",
                "remediation": "Configure the encrypted admin store before managing tenant setup.",
            }
        )
    elif not admin_execution_configured:
        findings.append(
            {
                "code": "admin_execution.unconfigured",
                "severity": "warning",
                "message": "Platform-admin chat is using deployment execution defaults.",
                "remediation": "Configure Platform admin execution in the administration screen.",
            }
        )
    return {
        "status": "ready" if all(checks.values()) else "not_ready",
        "readiness_checks": {
            name: "ok" if healthy else "failed" for name, healthy in checks.items()
        },
        "admin_store_configured": admin_store_configured,
        "admin_execution_configured": admin_execution_configured,
        "mcp_catalog_item_count": len(catalog),
        "execution_config_source": resolver_description.get("source"),
        "findings": findings,
    }


def diagnose_tenant_setup(app: Any, tenant_id: str) -> dict[str, object]:
    """Report redacted effective execution readiness for one tenant selected by an admin."""
    store = app.state.admin_store
    execution = app.state.execution_resolver.resolve(tenant_id)
    policy = store.get_tenant_mcp_server_catalog_policy(tenant_id) if store is not None else None
    assignment_count = (
        len(store.list_subject_mcp_server_catalog_assignments(tenant_id))
        if store is not None
        else 0
    )
    catalog = getattr(app.state.admin_store_settings, "mcp_server_catalog", ())
    selected_ids = set(policy.item_ids) if policy is not None else set()
    findings: list[dict[str, str]] = []
    if store is None:
        findings.append(
            {
                "code": "admin_store.unconfigured",
                "severity": "warning",
                "message": "Tenant-specific durable configuration is unavailable.",
                "remediation": "Configure the encrypted admin store.",
            }
        )
    elif policy is None:
        findings.append(
            {
                "code": "mcp_catalog.no_tenant_policy",
                "severity": "warning",
                "message": "No tenant MCP catalog policy is configured.",
                "remediation": "Set a tenant catalog policy before assigning catalog access.",
            }
        )
    return {
        "tenant_id": tenant_id,
        "execution_resolved": execution is not None,
        "execution_resolver": type(app.state.execution_resolver).__name__,
        "mcp_catalog": {
            "tenant_policy_configured": policy is not None,
            "tenant_item_ids": sorted(selected_ids),
            "require_subject_assignment": (
                policy.require_subject_assignment if policy is not None else None
            ),
            "assignment_count": assignment_count,
            "available_catalog_items": [
                _catalog_item_summary(item) for item in catalog if item.id in selected_ids
            ],
        },
        "findings": findings,
    }


def list_mcp_server_catalog_access(app: Any, tenant_id: str, user_id: str) -> dict[str, object]:
    """List catalog servers available to a tenant user without returning credentials."""
    store = app.state.admin_store
    if store is None:
        raise HTTPException(status_code=409, detail="Admin store is not configured")
    item_ids = store.effective_subject_mcp_server_catalog_item_ids(tenant_id, user_id)
    catalog = getattr(app.state.admin_store_settings, "mcp_server_catalog", ())
    allowed_ids = set(item_ids or ())
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "access_configured": item_ids is not None,
        "catalog_items": [
            _catalog_item_summary(item) for item in catalog if item.id in allowed_ids
        ],
    }


def _required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"{name} must be a non-empty string")
    return value.strip()


def build_admin_chat_tool_registry(app: Any, principal: Principal) -> ToolRegistry:
    """Build in-process self-service tools for an authenticated platform admin chat."""
    if not principal.is_admin:
        raise ValueError("Admin chat tools require an admin principal")
    registry = ToolRegistry()

    async def setup_handler(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, object]:
        _ = arguments, context
        return await get_setup_status(app)

    def tenant_handler(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, object]:
        _ = context
        return diagnose_tenant_setup(app, _required_string(arguments, "tenant_id"))

    def catalog_handler(
        arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> dict[str, object]:
        _ = context
        return list_mcp_server_catalog_access(
            app,
            _required_string(arguments, "tenant_id"),
            _required_string(arguments, "user_id"),
        )

    registry.register(
        ADMIN_CHAT_SETUP_TOOL,
        "Inspect this Minigent deployment's redacted readiness and safe setup findings.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        setup_handler,
    )
    registry.register(
        ADMIN_CHAT_TENANT_TOOL,
        "Diagnose redacted execution and MCP catalog setup for a Minigent tenant.",
        {
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
            "additionalProperties": False,
        },
        tenant_handler,
    )
    registry.register(
        ADMIN_CHAT_CATALOG_TOOL,
        "Inspect redacted effective Minigent MCP catalog access for one tenant user.",
        {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["tenant_id", "user_id"],
            "additionalProperties": False,
        },
        catalog_handler,
    )
    return registry


def build_admin_mcp_server() -> MCPServer[Any]:
    server = MCPServer(
        "Minigent Admin Operations",
        instructions=(
            "This server is read-only. It reports redacted Minigent deployment and tenant "
            "configuration diagnostics for authenticated platform administrators. Never infer "
            "or request secrets, credential values, or authorization headers."
        ),
    )

    @server.tool(name="get_setup_status")
    async def mcp_get_setup_status() -> dict[str, object]:
        """Return redacted deployment readiness and the next safe remediation steps."""
        return await get_setup_status(current_admin_mcp_request_context().app)

    @server.tool(name="diagnose_tenant_setup")
    def mcp_diagnose_tenant_setup(tenant_id: str) -> dict[str, object]:
        """Report redacted effective execution readiness for one tenant selected by an admin."""
        return diagnose_tenant_setup(current_admin_mcp_request_context().app, tenant_id)

    @server.tool(name="list_mcp_server_catalog_access")
    def mcp_list_mcp_server_catalog_access(tenant_id: str, user_id: str) -> dict[str, object]:
        """List the catalog servers effectively available to one tenant user without credentials."""
        return list_mcp_server_catalog_access(
            current_admin_mcp_request_context().app, tenant_id, user_id
        )

    return server
