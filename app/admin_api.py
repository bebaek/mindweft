from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import require_admin_principal
from app.execution import (
    TenantExecutionConfig,
    TenantExecutionResolver,
    parse_tenant_execution_config,
    redact_tenant_execution_payload,
    validate_tenant_execution_config,
)
from app.models import (
    AuditRecord,
    Message,
    Principal,
    Tenant,
    TenantDomain,
    TenantEntitlements,
    TenantStatus,
    TenantUser,
    TenantUserRole,
    TenantUserStatus,
    Thread,
    ThreadContext,
    ThreadStatus,
)

ADMIN_DB_PATH_ENV = "MINIGENT_ADMIN_DB_PATH"
ADMIN_ENCRYPTION_KEY_ENV = "MINIGENT_ADMIN_ENCRYPTION_KEY"
TENANT_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class AdminTenantResponse(BaseModel):
    id: str
    slug: str
    name: str
    status: TenantStatus
    plan: str | None = None
    region: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime
    updated_at: datetime


class AdminTenantListResponse(BaseModel):
    tenants: list[AdminTenantResponse]
    limit: int
    offset: int
    total: int
    next_offset: int | None = None


class AdminTenantCreateRequest(BaseModel):
    id: str | None = None
    slug: str
    name: str
    status: TenantStatus = TenantStatus.PROVISIONING
    plan: str | None = None
    region: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AdminTenantPatchRequest(BaseModel):
    slug: str | None = None
    name: str | None = None
    plan: str | None = None
    region: str | None = None
    metadata: dict[str, Any] | None = None


class AdminTenantDeleteResponse(BaseModel):
    deleted: bool
    tenant_id: str
    status: TenantStatus


class AdminTenantDomainCreateRequest(BaseModel):
    domain: str


class AdminTenantUserCreateRequest(BaseModel):
    user_id: str
    email: str | None = None
    display_name: str | None = None
    role: TenantUserRole = TenantUserRole.MEMBER
    status: TenantUserStatus = TenantUserStatus.INVITED
    metadata: dict[str, Any] = Field(default_factory=dict)


class AdminTenantUserPatchRequest(BaseModel):
    email: str | None = None
    display_name: str | None = None
    role: TenantUserRole | None = None
    status: TenantUserStatus | None = None
    metadata: dict[str, Any] | None = None


class AdminTenantUserResponse(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    email: str | None = None
    display_name: str | None = None
    role: TenantUserRole
    status: TenantUserStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime
    updated_at: datetime


class AdminTenantUserListResponse(BaseModel):
    tenant_id: str
    users: list[AdminTenantUserResponse]
    limit: int
    offset: int
    total: int
    next_offset: int | None = None


class AdminTenantUserDeleteResponse(BaseModel):
    deleted: bool
    tenant_id: str
    id: str
    status: TenantUserStatus


class AdminTenantDomainResponse(BaseModel):
    id: str
    tenant_id: str
    domain: str
    verified: bool
    created_at: datetime


class AdminTenantDomainListResponse(BaseModel):
    tenant_id: str
    domains: list[AdminTenantDomainResponse]


class AdminTenantSeedRequest(BaseModel):
    source: str = "execution-configs"
    status: TenantStatus = TenantStatus.ACTIVE
    plan: str | None = None
    region: str | None = None
    dry_run: bool = False


class AdminTenantSeedItemResponse(BaseModel):
    id: str
    slug: str
    name: str
    status: TenantStatus
    action: str


class AdminTenantSeedResponse(BaseModel):
    source: str
    dry_run: bool
    discovered: int
    existing: int
    created: int
    conflicts: int
    tenants: list[AdminTenantSeedItemResponse]


class AdminTenantEntitlementsRequest(BaseModel):
    features: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, int | float | str | bool | None] = Field(default_factory=dict)


class AdminTenantEntitlementsResponse(BaseModel):
    tenant_id: str
    features: dict[str, bool]
    limits: dict[str, int | float | str | bool | None]
    version: int
    updated_at: datetime


class AdminTenantEntitlementsValidationResponse(BaseModel):
    valid: bool
    features: dict[str, Any]
    limits: dict[str, Any]


class AdminExecutionConfigTenantListResponse(BaseModel):
    tenants: list[str]


class AdminTenantExecutionConfigResponse(BaseModel):
    tenant_id: str
    config: dict[str, Any]


class AdminTenantExecutionConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class AdminThreadSummaryResponse(BaseModel):
    thread_id: str
    tenant_id: str
    status: ThreadStatus
    created_at: datetime
    updated_at: datetime
    skill_name: str | None = None
    skill_names: list[str] | None = None
    capability_profile: str | None = None
    message_count: int


class AdminThreadListResponse(BaseModel):
    tenant_id: str
    threads: list[AdminThreadSummaryResponse]
    limit: int
    offset: int
    total: int
    next_offset: int | None = None


class AdminThreadContextResponse(BaseModel):
    summary: str
    summarized_message_count: int
    updated_at: datetime


class AdminThreadDetailResponse(AdminThreadSummaryResponse):
    context: AdminThreadContextResponse
    messages: list[Message]


class AdminThreadDeleteResponse(BaseModel):
    deleted: bool
    tenant_id: str
    thread_id: str


class AdminThreadPruneResponse(BaseModel):
    tenant_id: str
    deleted_count: int
    updated_before: datetime
    dry_run: bool = False
    candidate_thread_ids: list[str] = Field(default_factory=list)


class AdminAuditRecordResponse(BaseModel):
    audit_id: str
    tenant_id: str
    actor_user_id: str
    action: str
    affected_count: int
    thread_ids: list[str]
    resource_type: str | None = None
    resource_id: str | None = None
    old_values: dict[str, Any] | None = None
    new_values: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime


class AdminAuditRecordListResponse(BaseModel):
    tenant_id: str
    audit_records: list[AdminAuditRecordResponse]
    limit: int
    offset: int
    total: int
    next_offset: int | None = None


class AdminMCPServerValidationResponse(BaseModel):
    name: str
    url: str
    ok: bool
    error: str | None = None
    tool_count: int = 0
    protocol_version: str | None = None
    session: bool = False
    server_name: str | None = None
    server_version: str | None = None


class AdminValidationSectionResponse(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)


class AdminLLMValidationResponse(AdminValidationSectionResponse):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None


class AdminToolsValidationResponse(AdminValidationSectionResponse):
    local_tools: list[str] = Field(default_factory=list)
    unknown_local_tools: list[str] = Field(default_factory=list)
    mcp_servers: list[AdminMCPServerValidationResponse] = Field(default_factory=list)


class AdminTenantExecutionConfigValidationResponse(BaseModel):
    valid: bool
    config_shape: AdminValidationSectionResponse
    llm: AdminLLMValidationResponse
    tools: AdminToolsValidationResponse


def build_admin_router() -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.get("/tenants", response_model=AdminTenantListResponse)
    async def list_tenants(
        request: Request,
        admin: Principal = Depends(require_admin_principal),
        status: TenantStatus | None = Query(default=None),
        plan: str | None = Query(default=None),
        slug: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> AdminTenantListResponse:
        _ = admin
        store = _require_admin_store(request)
        tenants, total = store.list_registry_tenants(
            status=status,
            plan=plan,
            slug=slug,
            limit=limit,
            offset=offset,
        )
        next_offset = offset + len(tenants) if offset + len(tenants) < total else None
        return AdminTenantListResponse(
            tenants=[_tenant_response(tenant) for tenant in tenants],
            limit=limit,
            offset=offset,
            total=total,
            next_offset=next_offset,
        )

    @router.get(
        "/execution-config-tenants",
        response_model=AdminExecutionConfigTenantListResponse,
    )
    async def list_execution_config_tenants(
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminExecutionConfigTenantListResponse:
        _ = admin
        store = _require_admin_store(request)
        return AdminExecutionConfigTenantListResponse(tenants=store.list_tenants())

    @router.get(
        "/tenant-domains/lookup",
        response_model=AdminTenantDomainResponse,
    )
    async def lookup_tenant_domain(
        request: Request,
        admin: Principal = Depends(require_admin_principal),
        domain: str = Query(...),
        verified_only: bool = Query(default=False),
    ) -> AdminTenantDomainResponse:
        _ = admin
        store = _require_admin_store(request)
        domain_name = _normalize_domain(domain)
        tenant_domain = store.get_tenant_domain_by_domain(
            domain_name,
            verified_only=verified_only,
        )
        if tenant_domain is None:
            raise HTTPException(status_code=404, detail=f"Tenant domain '{domain_name}' not found")
        return _domain_response(tenant_domain)

    @router.post("/tenants", response_model=AdminTenantResponse, status_code=201)
    async def create_tenant(
        request: AdminTenantCreateRequest,
        app_request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantResponse:
        _validate_tenant_id(request.id) if request.id is not None else None
        _validate_slug(request.slug)
        _validate_name(request.name)
        store = _require_admin_store(app_request)
        tenant = Tenant(
            id=request.id or str(uuid4()),
            slug=request.slug,
            name=request.name,
            status=request.status,
            plan=request.plan,
            region=request.region,
            metadata=request.metadata,
            created_by=admin.user_id,
            updated_by=admin.user_id,
        )
        try:
            created = store.create_tenant(tenant)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Tenant id or slug already exists") from exc
        _append_tenant_audit(
            app_request,
            created.id,
            admin,
            "tenants.create",
            new_values=_tenant_audit_values(created),
        )
        return _tenant_response(created)

    @router.post("/tenants/seed", response_model=AdminTenantSeedResponse)
    async def seed_tenants(
        body: AdminTenantSeedRequest,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantSeedResponse:
        if body.source != "execution-configs":
            raise HTTPException(
                status_code=400,
                detail="Unsupported tenant seed source. Expected 'execution-configs'.",
            )
        store = _require_admin_store(request)
        tenant_ids = store.list_tenants()
        used_slugs = _used_tenant_slugs(store)
        items: list[AdminTenantSeedItemResponse] = []
        existing = 0
        created = 0
        conflicts = 0
        for tenant_id in tenant_ids:
            current = store.get_tenant(tenant_id)
            if current is not None:
                existing += 1
                used_slugs.add(current.slug)
                items.append(
                    AdminTenantSeedItemResponse(
                        id=current.id,
                        slug=current.slug,
                        name=current.name,
                        status=current.status,
                        action="exists",
                    )
                )
                continue
            slug = _unique_seed_slug(tenant_id, used_slugs)
            used_slugs.add(slug)
            item = AdminTenantSeedItemResponse(
                id=tenant_id,
                slug=slug,
                name=tenant_id,
                status=body.status,
                action="would_create" if body.dry_run else "created",
            )
            items.append(item)
            if body.dry_run:
                continue
            try:
                store.create_tenant(
                    Tenant(
                        id=tenant_id,
                        slug=slug,
                        name=tenant_id,
                        status=body.status,
                        plan=body.plan,
                        region=body.region,
                        created_by=admin.user_id,
                        updated_by=admin.user_id,
                    )
                )
            except sqlite3.IntegrityError:
                conflicts += 1
                items[-1] = item.model_copy(update={"action": "conflict"})
                continue
            created += 1
            _append_tenant_audit(
                request,
                tenant_id,
                admin,
                "tenants.seed",
                new_values={
                    "id": tenant_id,
                    "slug": slug,
                    "name": tenant_id,
                    "status": body.status.value,
                    "plan": body.plan,
                    "region": body.region,
                },
                metadata={"source": body.source, "slug": slug},
            )
        return AdminTenantSeedResponse(
            source=body.source,
            dry_run=body.dry_run,
            discovered=len(tenant_ids),
            existing=existing,
            created=created,
            conflicts=conflicts,
            tenants=items,
        )

    @router.get(
        "/tenants/{tenant_id}/users",
        response_model=AdminTenantUserListResponse,
    )
    async def list_tenant_users(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
        status: TenantUserStatus | None = Query(default=None),
        role: TenantUserRole | None = Query(default=None),
        email: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> AdminTenantUserListResponse:
        _ = admin
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        normalized_email = _normalize_email(email) if email is not None else None
        users, total = store.list_tenant_users(
            tenant_id,
            status=status,
            role=role,
            email=normalized_email,
            limit=limit,
            offset=offset,
        )
        next_offset = offset + len(users) if offset + len(users) < total else None
        return AdminTenantUserListResponse(
            tenant_id=tenant_id,
            users=[_tenant_user_response(user) for user in users],
            limit=limit,
            offset=offset,
            total=total,
            next_offset=next_offset,
        )

    @router.post(
        "/tenants/{tenant_id}/users",
        response_model=AdminTenantUserResponse,
        status_code=201,
    )
    async def create_tenant_user(
        tenant_id: str,
        body: AdminTenantUserCreateRequest,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantUserResponse:
        _validate_user_id(body.user_id)
        email = _normalize_email(body.email) if body.email is not None else None
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        user = TenantUser(
            id=str(uuid4()),
            tenant_id=tenant_id,
            user_id=body.user_id,
            email=email,
            display_name=body.display_name,
            role=body.role,
            status=body.status,
            metadata=body.metadata,
            created_by=admin.user_id,
            updated_by=admin.user_id,
        )
        try:
            created = store.create_tenant_user(user)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Tenant user already exists") from exc
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_users.create",
            new_values=_tenant_user_audit_values(created),
            resource_type="tenant_user",
            resource_id=created.id,
        )
        return _tenant_user_response(created)

    @router.get(
        "/tenants/{tenant_id}/users/{user_record_id}",
        response_model=AdminTenantUserResponse,
    )
    async def get_tenant_user(
        tenant_id: str,
        user_record_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantUserResponse:
        _ = admin
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        user = store.get_tenant_user(tenant_id, user_record_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"Tenant user '{user_record_id}' not found")
        return _tenant_user_response(user)

    @router.patch(
        "/tenants/{tenant_id}/users/{user_record_id}",
        response_model=AdminTenantUserResponse,
    )
    async def patch_tenant_user(
        tenant_id: str,
        user_record_id: str,
        body: AdminTenantUserPatchRequest,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantUserResponse:
        email = _normalize_email(body.email) if body.email is not None else None
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        old_user = store.get_tenant_user(tenant_id, user_record_id)
        user = store.update_tenant_user(
            tenant_id,
            user_record_id,
            email=email,
            display_name=body.display_name,
            role=body.role,
            status=body.status,
            metadata=body.metadata,
            updated_by=admin.user_id,
        )
        if user is None:
            raise HTTPException(status_code=404, detail=f"Tenant user '{user_record_id}' not found")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_users.update",
            old_values=_tenant_user_audit_values(old_user) if old_user is not None else None,
            new_values=_tenant_user_audit_values(user),
            resource_type="tenant_user",
            resource_id=user.id,
        )
        return _tenant_user_response(user)

    @router.post(
        "/tenants/{tenant_id}/users/{user_record_id}/activate",
        response_model=AdminTenantUserResponse,
    )
    async def activate_tenant_user(
        tenant_id: str,
        user_record_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantUserResponse:
        return _update_tenant_user_status(
            request,
            tenant_id,
            user_record_id,
            admin,
            TenantUserStatus.ACTIVE,
            "tenant_users.activate",
        )

    @router.post(
        "/tenants/{tenant_id}/users/{user_record_id}/suspend",
        response_model=AdminTenantUserResponse,
    )
    async def suspend_tenant_user(
        tenant_id: str,
        user_record_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantUserResponse:
        return _update_tenant_user_status(
            request,
            tenant_id,
            user_record_id,
            admin,
            TenantUserStatus.SUSPENDED,
            "tenant_users.suspend",
        )

    @router.delete(
        "/tenants/{tenant_id}/users/{user_record_id}",
        response_model=AdminTenantUserDeleteResponse,
    )
    async def delete_tenant_user(
        tenant_id: str,
        user_record_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantUserDeleteResponse:
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        old_user = store.get_tenant_user(tenant_id, user_record_id)
        deleted = store.delete_tenant_user(tenant_id, user_record_id, updated_by=admin.user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Tenant user '{user_record_id}' not found")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_users.delete",
            old_values=_tenant_user_audit_values(old_user) if old_user is not None else None,
            new_values={"status": TenantUserStatus.DELETED.value},
            resource_type="tenant_user",
            resource_id=user_record_id,
        )
        return AdminTenantUserDeleteResponse(
            deleted=True,
            tenant_id=tenant_id,
            id=user_record_id,
            status=TenantUserStatus.DELETED,
        )

    @router.get(
        "/tenants/{tenant_id}/domains",
        response_model=AdminTenantDomainListResponse,
    )
    async def list_tenant_domains(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantDomainListResponse:
        _ = admin
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        return AdminTenantDomainListResponse(
            tenant_id=tenant_id,
            domains=[_domain_response(domain) for domain in store.list_tenant_domains(tenant_id)],
        )

    @router.post(
        "/tenants/{tenant_id}/domains",
        response_model=AdminTenantDomainResponse,
        status_code=201,
    )
    async def add_tenant_domain(
        tenant_id: str,
        body: AdminTenantDomainCreateRequest,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantDomainResponse:
        domain_name = _normalize_domain(body.domain)
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        domain = TenantDomain(id=str(uuid4()), tenant_id=tenant_id, domain=domain_name)
        try:
            created = store.add_tenant_domain(domain)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Tenant domain already exists") from exc
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_domains.create",
            new_values=_domain_audit_values(created),
        )
        return _domain_response(created)

    @router.post(
        "/tenants/{tenant_id}/domains/{domain_id}/verify",
        response_model=AdminTenantDomainResponse,
    )
    async def verify_tenant_domain(
        tenant_id: str,
        domain_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantDomainResponse:
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        old_domain = store.get_tenant_domain(tenant_id, domain_id)
        domain = store.verify_tenant_domain(tenant_id, domain_id)
        if domain is None:
            raise HTTPException(status_code=404, detail=f"Tenant domain '{domain_id}' not found")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_domains.verify",
            old_values=_domain_audit_values(old_domain) if old_domain is not None else None,
            new_values=_domain_audit_values(domain),
        )
        return _domain_response(domain)

    @router.delete("/tenants/{tenant_id}/domains/{domain_id}", status_code=204)
    async def delete_tenant_domain(
        tenant_id: str,
        domain_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> None:
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        deleted = store.delete_tenant_domain(tenant_id, domain_id)
        if deleted is None:
            raise HTTPException(status_code=404, detail=f"Tenant domain '{domain_id}' not found")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_domains.delete",
            old_values=_domain_audit_values(deleted),
            new_values=None,
        )

    @router.get(
        "/tenants/{tenant_id}/entitlements",
        response_model=AdminTenantEntitlementsResponse,
    )
    async def get_tenant_entitlements(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantEntitlementsResponse:
        _ = admin
        store = _require_admin_store(request)
        entitlements = store.get_tenant_entitlements(tenant_id)
        if entitlements is None:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' has no entitlements")
        return _entitlements_response(entitlements)

    @router.put(
        "/tenants/{tenant_id}/entitlements",
        response_model=AdminTenantEntitlementsResponse,
    )
    async def put_tenant_entitlements(
        tenant_id: str,
        body: AdminTenantEntitlementsRequest,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantEntitlementsResponse:
        _validate_entitlements(body.features, body.limits)
        store = _require_admin_store(request)
        old_entitlements = store.get_tenant_entitlements(tenant_id)
        entitlements = store.upsert_tenant_entitlements(
            tenant_id,
            features=body.features,
            limits=body.limits,
        )
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_entitlements.put",
            old_values=(
                _entitlements_audit_values(old_entitlements)
                if old_entitlements is not None
                else None
            ),
            new_values=_entitlements_audit_values(entitlements),
        )
        return _entitlements_response(entitlements)

    @router.post(
        "/tenants/{tenant_id}/entitlements/validate",
        response_model=AdminTenantEntitlementsValidationResponse,
    )
    async def validate_tenant_entitlements(
        tenant_id: str,
        body: AdminTenantEntitlementsRequest,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantEntitlementsValidationResponse:
        _ = tenant_id
        _ = admin
        feature_errors, limit_errors = _entitlement_validation_errors(body.features, body.limits)
        return AdminTenantEntitlementsValidationResponse(
            valid=not feature_errors and not limit_errors,
            features={"ok": not feature_errors, "errors": feature_errors},
            limits={"ok": not limit_errors, "errors": limit_errors},
        )

    @router.delete("/tenants/{tenant_id}/entitlements", status_code=204)
    async def delete_tenant_entitlements(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> None:
        store = _require_admin_store(request)
        old_entitlements = store.get_tenant_entitlements(tenant_id)
        deleted = store.delete_tenant_entitlements(tenant_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' has no entitlements")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_entitlements.delete",
            old_values=(
                _entitlements_audit_values(old_entitlements)
                if old_entitlements is not None
                else None
            ),
            new_values=None,
        )

    @router.get("/tenants/{tenant_id}", response_model=AdminTenantResponse)
    async def get_tenant(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantResponse:
        _ = admin
        tenant = _require_tenant(request, tenant_id)
        return _tenant_response(tenant)

    @router.patch("/tenants/{tenant_id}", response_model=AdminTenantResponse)
    async def patch_tenant(
        tenant_id: str,
        body: AdminTenantPatchRequest,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantResponse:
        if body.slug is not None:
            _validate_slug(body.slug)
        if body.name is not None:
            _validate_name(body.name)
        store = _require_admin_store(request)
        old_tenant = store.get_tenant(tenant_id)
        try:
            tenant = store.update_tenant(
                tenant_id,
                slug=body.slug,
                name=body.name,
                plan=body.plan,
                region=body.region,
                metadata=body.metadata,
                updated_by=admin.user_id,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Tenant slug already exists") from exc
        if tenant is None:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenants.update",
            old_values=_tenant_audit_values(old_tenant) if old_tenant is not None else None,
            new_values=_tenant_audit_values(tenant),
        )
        return _tenant_response(tenant)

    @router.post("/tenants/{tenant_id}/activate", response_model=AdminTenantResponse)
    async def activate_tenant(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantResponse:
        return _update_tenant_status(request, tenant_id, admin, TenantStatus.ACTIVE, "tenants.activate")

    @router.post("/tenants/{tenant_id}/suspend", response_model=AdminTenantResponse)
    async def suspend_tenant(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantResponse:
        return _update_tenant_status(
            request, tenant_id, admin, TenantStatus.SUSPENDED, "tenants.suspend"
        )

    @router.post("/tenants/{tenant_id}/archive", response_model=AdminTenantResponse)
    async def archive_tenant(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantResponse:
        return _update_tenant_status(request, tenant_id, admin, TenantStatus.ARCHIVED, "tenants.archive")

    @router.delete("/tenants/{tenant_id}", response_model=AdminTenantDeleteResponse)
    async def delete_tenant(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantDeleteResponse:
        store = _require_admin_store(request)
        old_tenant = store.get_tenant(tenant_id)
        deleted = store.delete_tenant(tenant_id, updated_by=admin.user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenants.delete",
            old_values=_tenant_audit_values(old_tenant) if old_tenant is not None else None,
            new_values={"status": TenantStatus.DELETED.value},
        )
        return AdminTenantDeleteResponse(
            deleted=True,
            tenant_id=tenant_id,
            status=TenantStatus.DELETED,
        )

    @router.get("/tenants/{tenant_id}/threads", response_model=AdminThreadListResponse)
    async def list_tenant_threads(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        status: ThreadStatus | None = Query(default=None),
        profile: str | None = Query(default=None),
        skill: str | None = Query(default=None),
        created_after: datetime | None = Query(default=None),
        updated_after: datetime | None = Query(default=None),
    ) -> AdminThreadListResponse:
        _ = admin
        store = _require_thread_store(request)
        total = store.count_threads(
            tenant_id,
            status=status,
            capability_profile=profile,
            skill=skill,
            created_after=created_after,
            updated_after=updated_after,
        )
        threads = store.list_threads(
            tenant_id,
            status=status,
            capability_profile=profile,
            skill=skill,
            created_after=created_after,
            updated_after=updated_after,
            limit=limit,
            offset=offset,
        )
        next_offset = offset + len(threads) if offset + len(threads) < total else None
        return AdminThreadListResponse(
            tenant_id=tenant_id,
            limit=limit,
            offset=offset,
            total=total,
            next_offset=next_offset,
            threads=[
                _thread_summary(thread, store.count_messages(tenant_id, thread.thread_id))
                for thread in threads
            ],
        )

    @router.post(
        "/tenants/{tenant_id}/threads/prune",
        response_model=AdminThreadPruneResponse,
    )
    async def prune_tenant_threads(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
        updated_before: datetime = Query(...),
        status: ThreadStatus | None = Query(default=None),
        profile: str | None = Query(default=None),
        skill: str | None = Query(default=None),
        dry_run: bool = Query(default=False),
    ) -> AdminThreadPruneResponse:
        store = _require_thread_store(request)
        candidates = store.list_prunable_threads(
            tenant_id,
            updated_before=updated_before,
            status=status,
            capability_profile=profile,
            skill=skill,
        )
        candidate_thread_ids = [thread.thread_id for thread in candidates]
        deleted_count = 0
        if not dry_run:
            deleted_count = store.prune_threads(
                tenant_id,
                updated_before=updated_before,
                status=status,
                capability_profile=profile,
                skill=skill,
            )
            store.append_audit_record(
                AuditRecord(
                    tenant_id=tenant_id,
                    actor_user_id=admin.user_id,
                    action="threads.prune",
                    affected_count=deleted_count,
                    thread_ids=candidate_thread_ids,
                )
            )
        return AdminThreadPruneResponse(
            tenant_id=tenant_id,
            deleted_count=deleted_count,
            updated_before=updated_before,
            dry_run=dry_run,
            candidate_thread_ids=candidate_thread_ids,
        )

    @router.get(
        "/tenants/{tenant_id}/audit-records",
        response_model=AdminAuditRecordListResponse,
    )
    async def list_tenant_audit_records(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        action: str | None = Query(default=None),
        actor: str | None = Query(default=None),
        created_after: datetime | None = Query(default=None),
        created_before: datetime | None = Query(default=None),
    ) -> AdminAuditRecordListResponse:
        _ = admin
        store = _require_thread_store(request)
        total = store.count_audit_records(
            tenant_id,
            action=action,
            actor_user_id=actor,
            created_after=created_after,
            created_before=created_before,
        )
        records = store.list_audit_records(
            tenant_id,
            action=action,
            actor_user_id=actor,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
        )
        next_offset = offset + len(records) if offset + len(records) < total else None
        return AdminAuditRecordListResponse(
            tenant_id=tenant_id,
            limit=limit,
            offset=offset,
            total=total,
            next_offset=next_offset,
            audit_records=[_audit_record_response(record) for record in records],
        )

    @router.get(
        "/tenants/{tenant_id}/threads/{thread_id}",
        response_model=AdminThreadDetailResponse,
    )
    async def get_tenant_thread(
        tenant_id: str,
        thread_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminThreadDetailResponse:
        _ = admin
        store = _require_thread_store(request)
        thread = store.get_thread(tenant_id, thread_id)
        context = store.get_thread_context(tenant_id, thread_id)
        messages = store.list_messages(tenant_id, thread_id)
        return AdminThreadDetailResponse(
            **_thread_summary(thread, len(messages)).model_dump(),
            context=_thread_context_response(context),
            messages=messages,
        )

    @router.delete(
        "/tenants/{tenant_id}/threads/{thread_id}",
        response_model=AdminThreadDeleteResponse,
    )
    async def delete_tenant_thread(
        tenant_id: str,
        thread_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminThreadDeleteResponse:
        store = _require_thread_store(request)
        store.delete_thread(tenant_id, thread_id)
        store.append_audit_record(
            AuditRecord(
                tenant_id=tenant_id,
                actor_user_id=admin.user_id,
                action="threads.delete",
                affected_count=1,
                thread_ids=[thread_id],
            )
        )
        return AdminThreadDeleteResponse(
            deleted=True,
            tenant_id=tenant_id,
            thread_id=thread_id,
        )

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

    @router.post(
        "/tenants/{tenant_id}/execution-config/validate",
        response_model=AdminTenantExecutionConfigValidationResponse,
    )
    async def validate_tenant_execution_config_route(
        tenant_id: str,
        request: AdminTenantExecutionConfigRequest,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantExecutionConfigValidationResponse:
        _ = admin
        report = await validate_tenant_execution_config(tenant_id, request.config)
        return AdminTenantExecutionConfigValidationResponse.model_validate(report.to_dict())

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


def _require_thread_store(request: Request) -> Any:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Thread store is not enabled")
    return store


def _tenant_response(tenant: Tenant) -> AdminTenantResponse:
    return AdminTenantResponse(**tenant.model_dump())


def _tenant_user_response(user: TenantUser) -> AdminTenantUserResponse:
    return AdminTenantUserResponse(**user.model_dump())


def _tenant_user_audit_values(user: TenantUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "tenant_id": user.tenant_id,
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.value,
        "status": user.status.value,
        "metadata": user.metadata,
    }


def _domain_response(domain: TenantDomain) -> AdminTenantDomainResponse:
    return AdminTenantDomainResponse(**domain.model_dump())


def _domain_audit_values(domain: TenantDomain) -> dict[str, Any]:
    return {
        "id": domain.id,
        "tenant_id": domain.tenant_id,
        "domain": domain.domain,
        "verified": domain.verified,
        "created_at": domain.created_at.isoformat(),
    }


def _entitlements_response(entitlements: TenantEntitlements) -> AdminTenantEntitlementsResponse:
    return AdminTenantEntitlementsResponse(**entitlements.model_dump())


def _entitlements_audit_values(entitlements: TenantEntitlements) -> dict[str, Any]:
    return {
        "tenant_id": entitlements.tenant_id,
        "features": entitlements.features,
        "limits": entitlements.limits,
        "version": entitlements.version,
        "updated_at": entitlements.updated_at.isoformat(),
    }


def _entitlement_validation_errors(
    features: dict[str, bool],
    limits: dict[str, int | float | str | bool | None],
) -> tuple[list[str], list[str]]:
    feature_errors: list[str] = []
    limit_errors: list[str] = []
    for key, value in features.items():
        if not key.strip():
            feature_errors.append("Feature names must be non-empty")
        if not isinstance(value, bool):
            feature_errors.append(f"Feature '{key}' must be a boolean")
    for key, value in limits.items():
        if not key.strip():
            limit_errors.append("Limit names must be non-empty")
        if value is not None and not isinstance(value, (int, float, str, bool)):
            limit_errors.append(f"Limit '{key}' must be a scalar or null")
    return feature_errors, limit_errors


def _validate_entitlements(
    features: dict[str, bool],
    limits: dict[str, int | float | str | bool | None],
) -> None:
    feature_errors, limit_errors = _entitlement_validation_errors(features, limits)
    errors = [*feature_errors, *limit_errors]
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})


def _require_tenant(request: Request, tenant_id: str) -> Tenant:
    store = _require_admin_store(request)
    tenant = store.get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    return tenant


def _update_tenant_status(
    request: Request,
    tenant_id: str,
    admin: Principal,
    status: TenantStatus,
    action: str,
) -> AdminTenantResponse:
    store = _require_admin_store(request)
    old_tenant = store.get_tenant(tenant_id)
    tenant = store.update_tenant(tenant_id, status=status, updated_by=admin.user_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    _append_tenant_audit(
        request,
        tenant_id,
        admin,
        action,
        old_values={"status": old_tenant.status.value} if old_tenant is not None else None,
        new_values={"status": status.value},
    )
    return _tenant_response(tenant)


def _update_tenant_user_status(
    request: Request,
    tenant_id: str,
    user_record_id: str,
    admin: Principal,
    status: TenantUserStatus,
    action: str,
) -> AdminTenantUserResponse:
    store = _require_admin_store(request)
    _require_tenant(request, tenant_id)
    old_user = store.get_tenant_user(tenant_id, user_record_id)
    user = store.update_tenant_user(
        tenant_id,
        user_record_id,
        status=status,
        updated_by=admin.user_id,
    )
    if user is None:
        raise HTTPException(status_code=404, detail=f"Tenant user '{user_record_id}' not found")
    _append_tenant_audit(
        request,
        tenant_id,
        admin,
        action,
        old_values={"status": old_user.status.value} if old_user is not None else None,
        new_values={"status": status.value},
        resource_type="tenant_user",
        resource_id=user.id,
    )
    return _tenant_user_response(user)


def _append_tenant_audit(
    request: Request,
    tenant_id: str,
    admin: Principal,
    action: str,
    *,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    resource_type: str = "tenant",
    resource_id: str | None = None,
) -> None:
    store = _require_thread_store(request)
    store.append_audit_record(
        AuditRecord(
            tenant_id=tenant_id,
            actor_user_id=admin.user_id,
            action=action,
            affected_count=1,
            thread_ids=[],
            resource_type=resource_type,
            resource_id=resource_id or tenant_id,
            old_values=_redact_audit_payload(old_values),
            new_values=_redact_audit_payload(new_values),
            metadata=_redact_audit_payload(metadata),
        )
    )


_AUDIT_SECRET_KEY_PARTS = ("token", "secret", "key", "authorization", "password")


def _tenant_audit_values(tenant: Tenant) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "name": tenant.name,
        "status": tenant.status.value,
        "plan": tenant.plan,
        "region": tenant.region,
        "metadata": tenant.metadata,
    }


def _redact_audit_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    redacted = _redact_audit_value(value)
    return redacted if isinstance(redacted, dict) else None


def _redact_audit_value(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.casefold() for part in _AUDIT_SECRET_KEY_PARTS):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = _redact_audit_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_audit_value(item) for item in value]
    return value


def _validate_tenant_id(tenant_id: str) -> None:
    if not tenant_id.strip():
        raise HTTPException(status_code=400, detail="Tenant id must be non-empty")


def _validate_user_id(user_id: str) -> None:
    if not user_id.strip():
        raise HTTPException(status_code=400, detail="User id must be non-empty")


def _normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if not normalized or "@" not in normalized:
        raise HTTPException(status_code=400, detail="Tenant user email is invalid")
    return normalized


def _validate_slug(slug: str) -> None:
    if not TENANT_SLUG_PATTERN.fullmatch(slug):
        raise HTTPException(
            status_code=400,
            detail="Tenant slug must contain only lowercase letters, digits, and hyphens",
        )


def _validate_name(name: str) -> None:
    if not name.strip():
        raise HTTPException(status_code=400, detail="Tenant name must be non-empty")


def _normalize_domain(domain: str) -> str:
    normalized = domain.strip().lower().rstrip(".")
    if "://" in normalized or "/" in normalized or ":" in normalized:
        raise HTTPException(status_code=400, detail="Tenant domain must be a hostname")
    if not DOMAIN_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="Tenant domain is invalid")
    return normalized


def _used_tenant_slugs(store: Any) -> set[str]:
    tenants, _total = store.list_registry_tenants(limit=500, offset=0)
    return {tenant.slug for tenant in tenants}


def _seed_slug_from_tenant_id(tenant_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", tenant_id.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = slug[:63].strip("-")
    if not slug:
        slug = "tenant"
    return slug


def _unique_seed_slug(tenant_id: str, used_slugs: set[str]) -> str:
    base = _seed_slug_from_tenant_id(tenant_id)
    if base not in used_slugs:
        return base
    for suffix in range(2, 10_000):
        suffix_text = f"-{suffix}"
        candidate = base[: 63 - len(suffix_text)].rstrip("-") + suffix_text
        if candidate not in used_slugs:
            return candidate
    raise HTTPException(status_code=409, detail=f"Unable to derive unique slug for '{tenant_id}'")


def _thread_summary(thread: Thread, message_count: int) -> AdminThreadSummaryResponse:
    return AdminThreadSummaryResponse(
        thread_id=thread.thread_id,
        tenant_id=thread.tenant_id,
        status=thread.status,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        skill_name=thread.skill_name,
        skill_names=thread.skill_names,
        capability_profile=thread.capability_profile,
        message_count=message_count,
    )


def _thread_context_response(context: ThreadContext) -> AdminThreadContextResponse:
    return AdminThreadContextResponse(
        summary=context.summary,
        summarized_message_count=context.summarized_message_count,
        updated_at=context.updated_at,
    )


def _audit_record_response(record: AuditRecord) -> AdminAuditRecordResponse:
    return AdminAuditRecordResponse(**record.model_dump())


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
                    "allowed_tools": server.allowed_tools,
                    "path_policy": {
                        "deny_globs": list(server.path_policy.deny_globs),
                        "allow_globs": list(server.path_policy.allow_globs),
                    },
                }
                for server in config.tools.mcp_servers
            ],
        },
        "quality": {
            "enabled": config.quality.enabled,
            "mode": config.quality.mode,
            "provider": config.quality.provider,
            "model": config.quality.model,
            "base_url": config.quality.base_url,
            "api_key": config.quality.api_key,
            "extra_headers": dict(config.quality.extra_headers),
            "timeout": config.quality.timeout,
            "max_payload_chars": config.quality.max_payload_chars,
        },
        "agent_backend": {
            "type": config.agent_backend.type,
            "peer": config.agent_backend.peer,
            "cwd": config.agent_backend.cwd,
            "timeout_seconds": config.agent_backend.timeout_seconds,
            "poll_interval_seconds": config.agent_backend.poll_interval_seconds,
            "mcp_broker_enabled": config.agent_backend.mcp_broker_enabled,
        },
        "skills": {
            "default_skill": config.skills.default_skill,
            "items": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "system_prompt": skill.system_prompt,
                    "allowed_local_tools": skill.allowed_local_tools,
                    "mcp_server_names": skill.mcp_server_names,
                }
                for skill in config.skills.items
            ],
        },
        "capability_profiles": {
            "default_profile": config.capability_profiles.default_profile,
            "items": [
                {
                    "name": profile.name,
                    "description": profile.description,
                    "allowed_local_tools": profile.allowed_local_tools,
                    "mcp_server_names": profile.mcp_server_names,
                }
                for profile in config.capability_profiles.items
            ],
        },
    }
