from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_admin_principal
from app.execution import (
    TenantExecutionConfig,
    TenantExecutionResolver,
    parse_tenant_execution_config,
    redact_tenant_execution_payload,
)
from app.models import Principal

ADMIN_DB_PATH_ENV = "MINIGENT_ADMIN_DB_PATH"
ADMIN_ENCRYPTION_KEY_ENV = "MINIGENT_ADMIN_ENCRYPTION_KEY"


class AdminTenantListResponse(BaseModel):
    tenants: list[str]


class AdminTenantExecutionConfigResponse(BaseModel):
    tenant_id: str
    config: dict[str, Any]


class AdminTenantExecutionConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


def build_admin_router() -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("/tenants", response_model=AdminTenantListResponse)
    async def list_tenants(
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantListResponse:
        _ = admin
        store = _require_admin_store(request)
        return AdminTenantListResponse(tenants=store.list_tenants())

    @router.get(
        "/tenants/{tenant_id}/execution-config",
        response_model=AdminTenantExecutionConfigResponse,
    )
    async def get_tenant_execution_config(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantExecutionConfigResponse:
        _ = admin
        store = _require_admin_store(request)
        payload = store.get_raw_config(tenant_id)
        if payload is None:
            raise HTTPException(
                status_code=404, detail=f"Tenant '{tenant_id}' has no execution configuration"
            )
        return AdminTenantExecutionConfigResponse(
            tenant_id=tenant_id,
            config=redact_tenant_execution_payload(payload),
        )

    @router.put(
        "/tenants/{tenant_id}/execution-config",
        response_model=AdminTenantExecutionConfigResponse,
    )
    async def put_tenant_execution_config(
        tenant_id: str,
        request: AdminTenantExecutionConfigRequest,
        app_request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantExecutionConfigResponse:
        _ = admin
        store = _require_admin_store(app_request)
        config = parse_tenant_execution_config(tenant_id, request.config)
        payload = _serialize_config_payload(config)
        store.upsert_raw_config(tenant_id, payload)
        _invalidate_resolver(app_request.app.state.execution_resolver, tenant_id)
        return AdminTenantExecutionConfigResponse(
            tenant_id=tenant_id,
            config=redact_tenant_execution_payload(payload),
        )

    @router.delete("/tenants/{tenant_id}/execution-config", status_code=204)
    async def delete_tenant_execution_config(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> None:
        _ = admin
        store = _require_admin_store(request)
        deleted = store.delete_config(tenant_id)
        _invalidate_resolver(request.app.state.execution_resolver, tenant_id)
        if not deleted:
            raise HTTPException(
                status_code=404, detail=f"Tenant '{tenant_id}' has no execution configuration"
            )

    return router


def admin_store_path_from_env() -> str | None:
    value = os.getenv(ADMIN_DB_PATH_ENV)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def admin_encryption_key_from_env() -> str | None:
    value = os.getenv(ADMIN_ENCRYPTION_KEY_ENV)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _require_admin_store(request: Request) -> Any:
    store = getattr(request.app.state, "admin_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Admin config store is not enabled")
    return store


def _invalidate_resolver(resolver: TenantExecutionResolver, tenant_id: str) -> None:
    invalidate = getattr(resolver, "invalidate", None)
    if callable(invalidate):
        invalidate(tenant_id)


def _serialize_config_payload(config: TenantExecutionConfig) -> dict[str, Any]:
    return {
        "llm": {
            "provider": config.llm.provider,
            "model": config.llm.model,
            "base_url": config.llm.base_url,
            "api_key": config.llm.api_key,
            "extra_headers": dict(config.llm.extra_headers),
            "timeout": config.llm.timeout,
        },
        "tools": {
            "allowed_local_tools": config.tools.allowed_local_tools,
            "mcp_servers": [
                {
                    "name": server.name,
                    "url": server.url,
                    "headers": dict(server.headers),
                    "protocolVersion": server.protocol_version,
                }
                for server in config.tools.mcp_servers
            ],
        },
    }
