from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request

from app.auth import require_principal
from app.models import (
    Principal,
    Tenant,
    TenantContext,
    TenantEntitlements,
    TenantStatus,
    TenantUser,
    TenantUserStatus,
)

TENANT_REGISTRY_REQUIRED_ENV = "MINIGENT_TENANT_REGISTRY_REQUIRED"
TENANT_USER_REGISTRY_REQUIRED_ENV = "MINIGENT_TENANT_USER_REGISTRY_REQUIRED"
_REQUIRE_PRINCIPAL = Depends(require_principal)


@dataclass(frozen=True)
class TenantRegistrySettings:
    tenant_registry_required: bool = False
    tenant_user_registry_required: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TenantRegistrySettings:
        lookup = os.environ if env is None else env
        return cls(
            tenant_registry_required=_parse_bool_env(lookup, TENANT_REGISTRY_REQUIRED_ENV),
            tenant_user_registry_required=_parse_bool_env(
                lookup, TENANT_USER_REGISTRY_REQUIRED_ENV
            ),
        )


def tenant_registry_settings_from_env() -> TenantRegistrySettings:
    return TenantRegistrySettings.from_env()


def tenant_registry_required_from_env() -> bool:
    return TenantRegistrySettings.from_env().tenant_registry_required


def tenant_user_registry_required_from_env() -> bool:
    return TenantRegistrySettings.from_env().tenant_user_registry_required


def _parse_bool_env(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name, "").strip().lower()
    if not value:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


async def require_tenant_context(
    request: Request,
    principal: Principal = _REQUIRE_PRINCIPAL,
) -> TenantContext:
    store = getattr(request.app.state, "admin_store", None)
    required = tenant_registry_required_from_env()
    user_required = tenant_user_registry_required_from_env()

    if store is None:
        if required:
            raise HTTPException(status_code=503, detail="Tenant registry is not enabled")
        if user_required:
            raise HTTPException(status_code=503, detail="Tenant user registry is not enabled")
        context = TenantContext(principal=principal, tenant_id=principal.tenant_id)
        request.state.tenant_context = context
        return context

    tenant = store.get_tenant(principal.tenant_id)
    if tenant is None:
        if required:
            raise HTTPException(status_code=403, detail="Tenant is not active")
        membership = _require_active_membership(store, principal) if user_required else None
        context = _apply_membership(
            TenantContext(principal=principal, tenant_id=principal.tenant_id),
            membership,
        )
        request.state.tenant_context = context
        return context

    if required and tenant.status != TenantStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Tenant is not active")

    entitlements = store.get_tenant_entitlements(principal.tenant_id)
    execution_config_version = store.get_config_version(principal.tenant_id)
    membership = _require_active_membership(store, principal) if user_required else None
    context = _tenant_context_from_records(
        principal,
        tenant,
        entitlements,
        execution_config_version,
        membership,
    )
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
    execution_config_version: int | None,
    membership: TenantUser | None = None,
) -> TenantContext:
    context = TenantContext(
        principal=principal,
        tenant_id=principal.tenant_id,
        slug=tenant.slug,
        status=tenant.status,
        plan=tenant.plan,
        region=tenant.region,
        features=dict(entitlements.features) if entitlements is not None else {},
        limits=dict(entitlements.limits) if entitlements is not None else {},
        execution_config_version=execution_config_version,
        entitlements_version=entitlements.version if entitlements is not None else None,
    )
    return _apply_membership(context, membership)


def _require_active_membership(store: Any, principal: Principal) -> TenantUser:
    membership = store.get_tenant_user_by_user_id(principal.tenant_id, principal.user_id)
    if not isinstance(membership, TenantUser) or membership.status != TenantUserStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Tenant user is not active")
    return membership


def _apply_membership(context: TenantContext, membership: TenantUser | None) -> TenantContext:
    if membership is None:
        return context
    return context.model_copy(
        update={
            "membership_id": membership.id,
            "membership_email": membership.email,
            "membership_display_name": membership.display_name,
            "user_role": membership.role,
            "user_status": membership.status,
            "membership_metadata": dict(membership.metadata),
        }
    )
