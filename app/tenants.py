from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request

from app.auth import require_principal
from app.models import Principal, Tenant, TenantContext, TenantEntitlements, TenantStatus

TENANT_REGISTRY_REQUIRED_ENV = "MINIGENT_TENANT_REGISTRY_REQUIRED"
_REQUIRE_PRINCIPAL = Depends(require_principal)


def tenant_registry_required_from_env() -> bool:
    value = os.getenv(TENANT_REGISTRY_REQUIRED_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


async def require_tenant_context(
    request: Request,
    principal: Principal = _REQUIRE_PRINCIPAL,
) -> TenantContext:
    store = getattr(request.app.state, "admin_store", None)
    required = tenant_registry_required_from_env()

    if store is None:
        if required:
            raise HTTPException(status_code=503, detail="Tenant registry is not enabled")
        context = TenantContext(principal=principal, tenant_id=principal.tenant_id)
        request.state.tenant_context = context
        return context

    tenant = store.get_tenant(principal.tenant_id)
    if tenant is None:
        if required:
            raise HTTPException(status_code=403, detail="Tenant is not active")
        context = TenantContext(principal=principal, tenant_id=principal.tenant_id)
        request.state.tenant_context = context
        return context

    if required and tenant.status != TenantStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Tenant is not active")

    entitlements = store.get_tenant_entitlements(principal.tenant_id)
    context = _tenant_context_from_records(principal, tenant, entitlements)
    request.state.tenant_context = context
    return context


async def require_active_tenant_principal(
    request: Request,
    principal: Principal = _REQUIRE_PRINCIPAL,
) -> Principal:
    context = await require_tenant_context(request, principal)
    return context.principal


def _tenant_context_from_records(
    principal: Principal,
    tenant: Tenant,
    entitlements: TenantEntitlements | None,
) -> TenantContext:
    return TenantContext(
        principal=principal,
        tenant_id=principal.tenant_id,
        slug=tenant.slug,
        status=tenant.status,
        plan=tenant.plan,
        region=tenant.region,
        features=dict(entitlements.features) if entitlements is not None else {},
        limits=dict(entitlements.limits) if entitlements is not None else {},
        entitlements_version=entitlements.version if entitlements is not None else None,
    )
