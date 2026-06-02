from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request

from app.auth import require_principal
from app.models import Principal, TenantStatus

TENANT_REGISTRY_REQUIRED_ENV = "MINIGENT_TENANT_REGISTRY_REQUIRED"
_REQUIRE_PRINCIPAL = Depends(require_principal)


def tenant_registry_required_from_env() -> bool:
    value = os.getenv(TENANT_REGISTRY_REQUIRED_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


async def require_active_tenant_principal(
    request: Request,
    principal: Principal = _REQUIRE_PRINCIPAL,
) -> Principal:
    if not tenant_registry_required_from_env():
        return principal

    store = getattr(request.app.state, "admin_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Tenant registry is not enabled")

    tenant = store.get_tenant(principal.tenant_id)
    if tenant is None or tenant.status != TenantStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Tenant is not active")
    return principal
