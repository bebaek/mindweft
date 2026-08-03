from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError

from app.admin_store import (
    SubjectMCPServerCatalogAssignment,
    TenantMCPServerCatalogPolicy,
)
from app.auth import require_admin_principal, require_principal
from app.execution import (
    TenantExecutionResolver,
    interpolate_tenant_execution_env_placeholders,
    parse_tenant_execution_config,
    redact_tenant_execution_payload,
    tenant_mcp_server_catalog_policy_errors,
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
from app.oauth import (
    OAuthCredentials,
    SQLiteEncryptedOAuthStore,
    build_oauth_credential_store_from_env,
    generic_oauth_config_from_env,
    tenant_oauth_credential_key,
)
from app.session_auth import validate_session_auth_settings

ADMIN_DB_PATH_ENV = "MINIGENT_ADMIN_DB_PATH"
ADMIN_ENCRYPTION_KEY_ENV = "MINIGENT_ADMIN_ENCRYPTION_KEY"
ADMIN_MCP_SERVER_CATALOG_ENV = "MINIGENT_ADMIN_MCP_SERVER_CATALOG"
ADMIN_MCP_SERVER_CATALOG_SECRET_ENV = "MINIGENT_ADMIN_MCP_SERVER_CATALOG_SECRET"
TENANT_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
LOGIN_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._@+-]{0,127}$")
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
NON_NEGATIVE_INTEGER_ENTITLEMENT_LIMITS = {
    "max_threads",
    "max_messages_per_thread",
    "max_messages",
    "max_thread_runs",
}


class AdminMCPServerCatalogItem(BaseModel):
    id: str
    title: str
    description: str
    detail: str | None = None
    server: dict[str, Any]


class AdminMCPServerCatalogResponse(BaseModel):
    items: list[AdminMCPServerCatalogItem]
    managed: bool = False
    allow_custom_mcp_servers: bool = True


class AdminMCPServerCatalogPolicyRequest(BaseModel):
    item_ids: list[str] = Field(default_factory=list)
    allow_custom_mcp_servers: bool = False


class AdminMCPServerCatalogPolicyResponse(BaseModel):
    tenant_id: str
    item_ids: list[str]
    allow_custom_mcp_servers: bool
    version: int
    updated_by: str | None = None
    updated_at: datetime


class AdminMCPServerCatalogAssignmentRequest(BaseModel):
    item_ids: list[str] = Field(default_factory=list)


class AdminMCPServerCatalogAssignmentResponse(BaseModel):
    tenant_id: str
    subject_type: str
    subject_id: str
    item_ids: list[str]
    version: int
    updated_by: str | None = None
    updated_at: datetime


class AdminMCPServerCatalogAssignmentListResponse(BaseModel):
    tenant_id: str
    assignments: list[AdminMCPServerCatalogAssignmentResponse]


@dataclass(frozen=True)
class AdminStoreSettings:
    db_path: str | None = None
    encryption_key: str | None = None
    mcp_server_catalog: tuple[AdminMCPServerCatalogItem, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AdminStoreSettings:
        lookup = os.environ if env is None else env
        return cls(
            db_path=_optional_str_env(lookup, ADMIN_DB_PATH_ENV),
            encryption_key=_optional_str_env(lookup, ADMIN_ENCRYPTION_KEY_ENV),
            mcp_server_catalog=_parse_mcp_server_catalog(lookup),
        )


def admin_store_settings_from_env() -> AdminStoreSettings:
    return AdminStoreSettings.from_env()


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


class AdminTenantAttachmentStatisticsResponse(BaseModel):
    tenant_id: str
    total_count: int
    total_bytes: int
    pending_count: int
    pending_bytes: int
    referenced_count: int
    referenced_bytes: int
    exempt_count: int
    exempt_bytes: int
    oldest_pending_created_at: datetime | None = None
    oldest_pending_age_seconds: int | None = None
    max_count: int
    max_bytes: int


class AdminTenantRunConcurrencyResponse(BaseModel):
    tenant_id: str
    active_runs: int
    active_users: int
    next_expiration: datetime | None = None
    tenant_capacity: int
    user_capacity: int
    lease_seconds: int
    heartbeat_seconds: int


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


class AdminCredentialSetupRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    expires_in_seconds: int = Field(default=86_400, ge=300, le=604_800)


class AdminCredentialStatusResponse(BaseModel):
    configured: bool
    username: str | None = None
    disabled: bool = False
    managed_externally: bool = False
    updated_at: datetime | None = None


class AdminCredentialSetupResponse(BaseModel):
    username: str
    setup_token: str
    expires_at: datetime


class AdminPiOAuthImportRequest(BaseModel):
    credential: dict[str, Any]
    acknowledge_transfer: bool = False


class AdminTenantOAuthCredentialResponse(BaseModel):
    tenant_id: str
    provider_id: str
    source: str
    connected: bool
    account_id: str | None = None
    expires_at: datetime | None = None


class AdminCredentialDisableResponse(BaseModel):
    disabled: bool


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
    version: int
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


async def require_tenant_owner_principal(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> Principal:
    if principal.is_admin:
        return principal
    tenant_id = request.path_params.get("tenant_id")
    if not isinstance(tenant_id, str) or tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant owner access required")
    store = _require_admin_store(request)
    tenant = store.get_tenant(tenant_id)
    if tenant is None or tenant.status not in {TenantStatus.PROVISIONING, TenantStatus.ACTIVE}:
        raise HTTPException(status_code=403, detail="Tenant owner access required")
    membership = store.get_tenant_user_by_user_id(tenant_id, principal.user_id)
    if (
        membership is None
        or membership.status != TenantUserStatus.ACTIVE
        or membership.role != TenantUserRole.OWNER
    ):
        raise HTTPException(status_code=403, detail="Tenant owner access required")
    return principal


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
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantUserResponse:
        email = _normalize_email(body.email) if body.email is not None else None
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        old_user = store.get_tenant_user(tenant_id, user_record_id)
        if old_user is not None:
            next_role = body.role if body.role is not None else old_user.role
            next_status = body.status if body.status is not None else old_user.status
            _protect_last_active_owner(store, tenant_id, old_user, next_role, next_status)
        user_updates = body.model_dump(exclude_unset=True)
        for field in ("role", "status"):
            if user_updates.get(field) is None:
                user_updates.pop(field, None)
        if "email" in body.model_fields_set:
            user_updates["email"] = email
        user = store.update_tenant_user(
            tenant_id,
            user_record_id,
            **user_updates,
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
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantUserDeleteResponse:
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        old_user = store.get_tenant_user(tenant_id, user_record_id)
        if old_user is not None:
            _protect_last_active_owner(
                store,
                tenant_id,
                old_user,
                old_user.role,
                TenantUserStatus.DELETED,
            )
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
        "/tenants/{tenant_id}/users/{user_record_id}/credential",
        response_model=AdminCredentialStatusResponse,
    )
    async def get_tenant_user_credential(
        tenant_id: str,
        user_record_id: str,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminCredentialStatusResponse:
        _ = admin
        store = _require_admin_store(request)
        user = store.get_tenant_user(tenant_id, user_record_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"Tenant user '{user_record_id}' not found")
        identity = store.get_local_identity_for_user(tenant_id, user.user_id)
        environment_username = next(
            (
                username
                for username, credential in validate_session_auth_settings().credentials.items()
                if credential.principal.user_id == user.user_id
                and credential.principal.tenant_id == tenant_id
            ),
            None,
        )
        return AdminCredentialStatusResponse(
            configured=identity is not None or environment_username is not None,
            username=(identity.username if identity is not None else environment_username),
            disabled=identity.disabled if identity is not None else False,
            managed_externally=environment_username is not None and identity is None,
            updated_at=identity.updated_at if identity is not None else None,
        )

    @router.post(
        "/tenants/{tenant_id}/users/{user_record_id}/credential/setup",
        response_model=AdminCredentialSetupResponse,
    )
    async def create_tenant_user_credential_setup(
        tenant_id: str,
        user_record_id: str,
        body: AdminCredentialSetupRequest,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminCredentialSetupResponse:
        store = _require_admin_store(request)
        tenant = _require_tenant(request, tenant_id)
        if tenant.status not in {TenantStatus.PROVISIONING, TenantStatus.ACTIVE}:
            raise HTTPException(status_code=409, detail="Tenant is not available for user setup")
        user = store.get_tenant_user(tenant_id, user_record_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"Tenant user '{user_record_id}' not found")
        if user.status not in {TenantUserStatus.INVITED, TenantUserStatus.ACTIVE}:
            raise HTTPException(status_code=409, detail="Tenant user is not available for sign-in")
        username = body.username.strip().lower()
        if username in validate_session_auth_settings().credentials:
            raise HTTPException(
                status_code=409, detail="Username is managed by deployment configuration"
            )
        if not LOGIN_USERNAME_PATTERN.fullmatch(username):
            raise HTTPException(
                status_code=400,
                detail="Username must start with a letter or digit and use only letters, digits, '.', '_', '@', '+', or '-'",
            )
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in_seconds)
        try:
            store.create_password_setup(
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                username=username,
                tenant_id=tenant_id,
                user_id=user.user_id,
                expires_at=expires_at,
                created_by=admin.user_id,
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409, detail="Username is unavailable or cannot be changed"
            ) from exc
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_user_credentials.setup_created",
            new_values={"username": username, "expires_at": expires_at.isoformat()},
            resource_type="tenant_user",
            resource_id=user_record_id,
        )
        return AdminCredentialSetupResponse(
            username=username,
            setup_token=token,
            expires_at=expires_at,
        )

    @router.delete(
        "/tenants/{tenant_id}/users/{user_record_id}/credential",
        response_model=AdminCredentialDisableResponse,
    )
    async def disable_tenant_user_credential(
        tenant_id: str,
        user_record_id: str,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminCredentialDisableResponse:
        store = _require_admin_store(request)
        user = store.get_tenant_user(tenant_id, user_record_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"Tenant user '{user_record_id}' not found")
        if not admin.is_admin and user.user_id == admin.user_id:
            raise HTTPException(
                status_code=409, detail="Owners cannot disable their own credential"
            )
        disabled = store.disable_local_identity(tenant_id, user.user_id)
        if not disabled:
            raise HTTPException(status_code=404, detail="Tenant user has no local credential")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_user_credentials.disabled",
            resource_type="tenant_user",
            resource_id=user_record_id,
        )
        return AdminCredentialDisableResponse(disabled=True)

    @router.get(
        "/tenants/{tenant_id}/oauth/openai-codex",
        response_model=AdminTenantOAuthCredentialResponse,
    )
    async def get_tenant_openai_oauth_credential(
        tenant_id: str,
        request: Request,
        owner: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantOAuthCredentialResponse:
        _ = owner
        store, provider_id, credential_key = _tenant_oauth_store(tenant_id)
        credentials = store.get(credential_key)
        return _tenant_oauth_credential_response(tenant_id, provider_id, credentials)

    @router.post(
        "/tenants/{tenant_id}/oauth/openai-codex/import/pi",
        response_model=AdminTenantOAuthCredentialResponse,
    )
    async def import_tenant_openai_oauth_from_pi(
        tenant_id: str,
        body: AdminPiOAuthImportRequest,
        request: Request,
        owner: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantOAuthCredentialResponse:
        if not body.acknowledge_transfer:
            raise HTTPException(
                status_code=400,
                detail="Credential transfer acknowledgement is required",
            )
        credentials = _parse_pi_openai_oauth_credential(body.credential)
        store, provider_id, credential_key = _tenant_oauth_store(tenant_id)
        store.set(credential_key, credentials)
        _invalidate_resolver(request.app.state.execution_resolver, tenant_id)
        _append_tenant_audit(
            request,
            tenant_id,
            owner,
            "tenant_oauth_credentials.import",
            new_values={
                "provider_id": provider_id,
                "source": "pi",
                "account_id": credentials.account_id,
                "expires_at": datetime.fromtimestamp(
                    credentials.expires_at, timezone.utc
                ).isoformat(),
            },
            resource_type="oauth_credential",
            resource_id=provider_id,
        )
        return _tenant_oauth_credential_response(tenant_id, provider_id, credentials)

    @router.delete("/tenants/{tenant_id}/oauth/openai-codex", status_code=204)
    async def delete_tenant_openai_oauth_credential(
        tenant_id: str,
        request: Request,
        owner: Principal = Depends(require_tenant_owner_principal),
    ) -> None:
        store, provider_id, credential_key = _tenant_oauth_store(tenant_id)
        credentials = store.get(credential_key)
        if credentials is None:
            raise HTTPException(status_code=404, detail="Tenant OAuth credential not found")
        store.delete(credential_key)
        _invalidate_resolver(request.app.state.execution_resolver, tenant_id)
        _append_tenant_audit(
            request,
            tenant_id,
            owner,
            "tenant_oauth_credentials.delete",
            old_values={
                "provider_id": provider_id,
                "source": "pi",
                "account_id": credentials.account_id,
            },
            resource_type="oauth_credential",
            resource_id=provider_id,
        )

    @router.get(
        "/tenants/{tenant_id}/domains",
        response_model=AdminTenantDomainListResponse,
    )
    async def list_tenant_domains(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
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
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantResponse:
        _ = admin
        tenant = _require_tenant(request, tenant_id)
        return _tenant_response(tenant)

    @router.patch("/tenants/{tenant_id}", response_model=AdminTenantResponse)
    async def patch_tenant(
        tenant_id: str,
        body: AdminTenantPatchRequest,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantResponse:
        if not admin.is_admin:
            disallowed = body.model_fields_set - {"slug", "name"}
            if disallowed:
                raise HTTPException(
                    status_code=403,
                    detail="Tenant owners can only update the tenant name and slug",
                )
        if body.slug is not None:
            _validate_slug(body.slug)
        if body.name is not None:
            _validate_name(body.name)
        store = _require_admin_store(request)
        old_tenant = store.get_tenant(tenant_id)
        try:
            tenant_updates = body.model_dump(exclude_unset=True)
            tenant = store.update_tenant(
                tenant_id,
                **tenant_updates,
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
        return _update_tenant_status(
            request, tenant_id, admin, TenantStatus.ACTIVE, "tenants.activate"
        )

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
        return _update_tenant_status(
            request, tenant_id, admin, TenantStatus.ARCHIVED, "tenants.archive"
        )

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

    @router.get(
        "/tenants/{tenant_id}/run-concurrency",
        response_model=AdminTenantRunConcurrencyResponse,
    )
    async def tenant_run_concurrency(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantRunConcurrencyResponse:
        _ = admin
        statistics = request.app.state.rate_limiter.run_concurrency_statistics(tenant_id)
        policy = request.app.state.rate_limit_settings.concurrent_runs
        return AdminTenantRunConcurrencyResponse(
            tenant_id=tenant_id,
            active_runs=statistics.active_runs,
            active_users=statistics.active_users,
            next_expiration=(
                datetime.fromtimestamp(statistics.next_expiration, tz=timezone.utc)
                if statistics.next_expiration is not None
                else None
            ),
            tenant_capacity=policy.tenant_capacity,
            user_capacity=policy.user_capacity,
            lease_seconds=policy.lease_seconds,
            heartbeat_seconds=policy.heartbeat_seconds,
        )

    @router.get(
        "/tenants/{tenant_id}/attachments/statistics",
        response_model=AdminTenantAttachmentStatisticsResponse,
    )
    async def tenant_attachment_statistics(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminTenantAttachmentStatisticsResponse:
        _ = admin
        now = datetime.now(timezone.utc)
        statistics = request.app.state.attachment_store.statistics(tenant_id, now=now)
        settings = request.app.state.attachment_store_settings
        oldest_age = (
            max(0, int((now - statistics.oldest_pending_created_at).total_seconds()))
            if statistics.oldest_pending_created_at is not None
            else None
        )
        return AdminTenantAttachmentStatisticsResponse(
            tenant_id=tenant_id,
            total_count=statistics.total_count,
            total_bytes=statistics.total_bytes,
            pending_count=statistics.pending_count,
            pending_bytes=statistics.pending_bytes,
            referenced_count=statistics.referenced_count,
            referenced_bytes=statistics.referenced_bytes,
            exempt_count=statistics.exempt_count,
            exempt_bytes=statistics.exempt_bytes,
            oldest_pending_created_at=statistics.oldest_pending_created_at,
            oldest_pending_age_seconds=oldest_age,
            max_count=settings.max_per_tenant,
            max_bytes=settings.max_bytes_per_tenant,
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
        "/mcp-server-catalog",
        response_model=AdminMCPServerCatalogResponse,
    )
    async def get_deployment_mcp_server_catalog(
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminMCPServerCatalogResponse:
        _ = admin
        items = [
            item.model_copy(update={"server": redact_tenant_execution_payload(item.server)})
            for item in _configured_mcp_server_catalog(request)
        ]
        return AdminMCPServerCatalogResponse(items=items)

    @router.get(
        "/tenants/{tenant_id}/mcp-server-catalog-policy",
        response_model=AdminMCPServerCatalogPolicyResponse,
    )
    async def get_mcp_server_catalog_policy(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminMCPServerCatalogPolicyResponse:
        _ = admin
        store = _require_admin_store(request)
        policy = store.get_tenant_mcp_server_catalog_policy(tenant_id)
        if policy is None:
            raise HTTPException(
                status_code=404,
                detail=f"Tenant '{tenant_id}' has no managed MCP server catalog policy",
            )
        return AdminMCPServerCatalogPolicyResponse(
            tenant_id=policy.tenant_id,
            item_ids=list(policy.item_ids),
            allow_custom_mcp_servers=policy.allow_custom_mcp_servers,
            version=policy.version,
            updated_by=policy.updated_by,
            updated_at=policy.updated_at,
        )

    @router.put(
        "/tenants/{tenant_id}/mcp-server-catalog-policy",
        response_model=AdminMCPServerCatalogPolicyResponse,
    )
    async def put_mcp_server_catalog_policy(
        tenant_id: str,
        body: AdminMCPServerCatalogPolicyRequest,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminMCPServerCatalogPolicyResponse:
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        catalog = _configured_mcp_server_catalog(request)
        catalog_ids = {item.id for item in catalog}
        duplicate_ids = sorted(
            {item_id for item_id in body.item_ids if body.item_ids.count(item_id) > 1}
        )
        unknown_ids = sorted(set(body.item_ids) - catalog_ids)
        if duplicate_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Catalog policy contains duplicate item ids: {', '.join(duplicate_ids)}",
            )
        if unknown_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Catalog policy references unknown item ids: {', '.join(unknown_ids)}",
            )
        old_policy = store.get_tenant_mcp_server_catalog_policy(tenant_id)
        policy = store.upsert_tenant_mcp_server_catalog_policy(
            tenant_id,
            item_ids=body.item_ids,
            allow_custom_mcp_servers=body.allow_custom_mcp_servers,
            updated_by=admin.user_id,
        )
        _invalidate_resolver(request.app.state.execution_resolver, tenant_id)
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_mcp_server_catalog_policy.put",
            old_values=_mcp_server_catalog_policy_audit_values(old_policy),
            new_values=_mcp_server_catalog_policy_audit_values(policy),
        )
        return AdminMCPServerCatalogPolicyResponse(
            tenant_id=policy.tenant_id,
            item_ids=list(policy.item_ids),
            allow_custom_mcp_servers=policy.allow_custom_mcp_servers,
            version=policy.version,
            updated_by=policy.updated_by,
            updated_at=policy.updated_at,
        )

    @router.delete("/tenants/{tenant_id}/mcp-server-catalog-policy", status_code=204)
    async def delete_mcp_server_catalog_policy(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> None:
        store = _require_admin_store(request)
        old_policy = store.get_tenant_mcp_server_catalog_policy(tenant_id)
        if old_policy is None or not store.delete_tenant_mcp_server_catalog_policy(tenant_id):
            raise HTTPException(
                status_code=404,
                detail=f"Tenant '{tenant_id}' has no managed MCP server catalog policy",
            )
        _invalidate_resolver(request.app.state.execution_resolver, tenant_id)
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_mcp_server_catalog_policy.delete",
            old_values=_mcp_server_catalog_policy_audit_values(old_policy),
            new_values=None,
        )

    @router.get(
        "/tenants/{tenant_id}/mcp-server-catalog-assignments",
        response_model=AdminMCPServerCatalogAssignmentListResponse,
    )
    async def list_mcp_server_catalog_assignments(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminMCPServerCatalogAssignmentListResponse:
        _ = admin
        _require_tenant(request, tenant_id)
        assignments = _require_admin_store(request).list_subject_mcp_server_catalog_assignments(
            tenant_id
        )
        return AdminMCPServerCatalogAssignmentListResponse(
            tenant_id=tenant_id,
            assignments=[_mcp_server_catalog_assignment_response(item) for item in assignments],
        )

    @router.put(
        "/tenants/{tenant_id}/mcp-server-catalog-assignments/{subject_type}/{subject_id}",
        response_model=AdminMCPServerCatalogAssignmentResponse,
    )
    async def put_mcp_server_catalog_assignment(
        tenant_id: str,
        subject_type: str,
        subject_id: str,
        body: AdminMCPServerCatalogAssignmentRequest,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> AdminMCPServerCatalogAssignmentResponse:
        store = _require_admin_store(request)
        _require_tenant(request, tenant_id)
        _validate_catalog_assignment_subject(store, tenant_id, subject_type, subject_id)
        tenant_policy = store.get_tenant_mcp_server_catalog_policy(tenant_id)
        if tenant_policy is None:
            raise HTTPException(
                status_code=409,
                detail="Create a managed tenant catalog policy before assigning subjects",
            )
        catalog_ids = {item.id for item in _configured_mcp_server_catalog(request)}
        duplicate_ids = sorted(
            {item_id for item_id in body.item_ids if body.item_ids.count(item_id) > 1}
        )
        unknown_ids = sorted(set(body.item_ids) - catalog_ids)
        if duplicate_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Catalog assignment contains duplicate item ids: {', '.join(duplicate_ids)}",
            )
        if unknown_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Catalog assignment references unknown item ids: {', '.join(unknown_ids)}",
            )
        disallowed_ids = sorted(set(body.item_ids) - set(tenant_policy.item_ids))
        if disallowed_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Catalog assignment exceeds the tenant policy: " + ", ".join(disallowed_ids)
                ),
            )
        old_assignment = store.get_subject_mcp_server_catalog_assignment(
            tenant_id, subject_type, subject_id
        )
        assignment = store.upsert_subject_mcp_server_catalog_assignment(
            tenant_id,
            subject_type,
            subject_id,
            item_ids=body.item_ids,
            updated_by=admin.user_id,
        )
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_mcp_server_catalog_assignment.upsert",
            old_values=_mcp_server_catalog_assignment_audit_values(old_assignment),
            new_values=_mcp_server_catalog_assignment_audit_values(assignment),
        )
        return _mcp_server_catalog_assignment_response(assignment)

    @router.delete(
        "/tenants/{tenant_id}/mcp-server-catalog-assignments/{subject_type}/{subject_id}",
        status_code=204,
    )
    async def delete_mcp_server_catalog_assignment(
        tenant_id: str,
        subject_type: str,
        subject_id: str,
        request: Request,
        admin: Principal = Depends(require_admin_principal),
    ) -> None:
        store = _require_admin_store(request)
        old_assignment = store.get_subject_mcp_server_catalog_assignment(
            tenant_id, subject_type, subject_id
        )
        if old_assignment is None or not store.delete_subject_mcp_server_catalog_assignment(
            tenant_id, subject_type, subject_id
        ):
            raise HTTPException(status_code=404, detail="Catalog assignment not found")
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_mcp_server_catalog_assignment.delete",
            old_values=_mcp_server_catalog_assignment_audit_values(old_assignment),
            new_values=None,
        )

    @router.get(
        "/tenants/{tenant_id}/mcp-server-catalog",
        response_model=AdminMCPServerCatalogResponse,
    )
    async def get_mcp_server_catalog(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminMCPServerCatalogResponse:
        _ = admin
        store = getattr(request.app.state, "admin_store", None)
        configured_items = _configured_mcp_server_catalog(request)
        policy = (
            store.get_tenant_mcp_server_catalog_policy(tenant_id) if store is not None else None
        )
        if policy is not None:
            granted_ids = set(policy.item_ids)
            configured_items = tuple(item for item in configured_items if item.id in granted_ids)
        items = [
            item.model_copy(update={"server": redact_tenant_execution_payload(item.server)})
            for item in configured_items
        ]
        return AdminMCPServerCatalogResponse(
            items=items,
            managed=policy is not None,
            allow_custom_mcp_servers=(
                policy.allow_custom_mcp_servers if policy is not None else True
            ),
        )

    @router.get(
        "/tenants/{tenant_id}/execution-config",
        response_model=AdminTenantExecutionConfigResponse,
    )
    async def get_tenant_execution_config(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantExecutionConfigResponse:
        _ = admin
        store = _require_admin_store(request)
        payload = store.get_raw_config(tenant_id)
        if payload is None:
            raise HTTPException(
                status_code=404, detail=f"Tenant '{tenant_id}' has no execution configuration"
            )
        version = store.get_config_version(tenant_id)
        if version is None:  # pragma: no cover - defensive
            raise HTTPException(
                status_code=404, detail=f"Tenant '{tenant_id}' has no execution configuration"
            )
        return AdminTenantExecutionConfigResponse(
            tenant_id=tenant_id,
            version=version,
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
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantExecutionConfigResponse:
        store = _require_admin_store(app_request)
        old_payload = store.get_raw_config(tenant_id)
        secret_source = _execution_config_secret_source(app_request, old_payload)
        payload = _restore_redacted_payload(request.config, secret_source)
        policy_errors = _tenant_catalog_policy_errors(app_request, tenant_id, payload)
        if policy_errors:
            raise HTTPException(status_code=400, detail="; ".join(policy_errors))
        try:
            parse_tenant_execution_config(tenant_id, payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.upsert_raw_config(tenant_id, payload)
        version = store.get_config_version(tenant_id)
        if version is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Execution config for tenant '{tenant_id}' was not saved")
        _invalidate_resolver(app_request.app.state.execution_resolver, tenant_id)
        _append_tenant_audit(
            app_request,
            tenant_id,
            admin,
            "tenant_execution_config.put",
            old_values=(
                redact_tenant_execution_payload(old_payload) if old_payload is not None else None
            ),
            new_values=redact_tenant_execution_payload(payload),
        )
        return AdminTenantExecutionConfigResponse(
            tenant_id=tenant_id,
            version=version,
            config=redact_tenant_execution_payload(payload),
        )

    @router.post(
        "/tenants/{tenant_id}/execution-config/validate",
        response_model=AdminTenantExecutionConfigValidationResponse,
    )
    async def validate_tenant_execution_config_route(
        tenant_id: str,
        body: AdminTenantExecutionConfigRequest,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> AdminTenantExecutionConfigValidationResponse:
        _ = admin
        store = _require_admin_store(request)
        existing = store.get_raw_config(tenant_id)
        secret_source = _execution_config_secret_source(request, existing)
        payload = _restore_redacted_payload(body.config, secret_source)
        policy_errors = _tenant_catalog_policy_errors(request, tenant_id, payload)
        if policy_errors:
            raise HTTPException(status_code=400, detail="; ".join(policy_errors))
        report = await validate_tenant_execution_config(tenant_id, payload)
        return AdminTenantExecutionConfigValidationResponse.model_validate(report.to_dict())

    @router.delete("/tenants/{tenant_id}/execution-config", status_code=204)
    async def delete_tenant_execution_config(
        tenant_id: str,
        request: Request,
        admin: Principal = Depends(require_tenant_owner_principal),
    ) -> None:
        store = _require_admin_store(request)
        old_payload = store.get_raw_config(tenant_id)
        deleted = store.delete_config(tenant_id)
        if not deleted:
            raise HTTPException(
                status_code=404, detail=f"Tenant '{tenant_id}' has no execution configuration"
            )
        _invalidate_resolver(request.app.state.execution_resolver, tenant_id)
        _append_tenant_audit(
            request,
            tenant_id,
            admin,
            "tenant_execution_config.delete",
            old_values=(
                redact_tenant_execution_payload(old_payload) if old_payload is not None else None
            ),
            new_values=None,
        )

    return router


def admin_store_path_from_env() -> str | None:
    return AdminStoreSettings.from_env().db_path


def admin_encryption_key_from_env() -> str | None:
    return AdminStoreSettings.from_env().encryption_key


def _parse_mcp_server_catalog(
    env: Mapping[str, str],
) -> tuple[AdminMCPServerCatalogItem, ...]:
    env_name = ADMIN_MCP_SERVER_CATALOG_ENV
    raw = _optional_str_env(env, ADMIN_MCP_SERVER_CATALOG_SECRET_ENV)
    if raw is not None:
        env_name = ADMIN_MCP_SERVER_CATALOG_SECRET_ENV
    else:
        raw = _optional_str_env(env, ADMIN_MCP_SERVER_CATALOG_ENV)
    if raw is None:
        return ()
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{env_name} must be valid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"{env_name} must be a JSON array")
    interpolated_payload = interpolate_tenant_execution_env_placeholders(payload, env)
    if not isinstance(interpolated_payload, list):  # pragma: no cover - shape is preserved
        raise RuntimeError(f"{env_name} interpolation changed the catalog shape")

    items: list[AdminMCPServerCatalogItem] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, value in enumerate(interpolated_payload):
        try:
            item = AdminMCPServerCatalogItem.model_validate(value)
        except ValidationError as exc:
            raise RuntimeError(f"{env_name}[{index}] is invalid") from exc
        server_name = item.server.get("name")
        server_url = item.server.get("url")
        if not item.id.strip() or not item.title.strip() or not item.description.strip():
            raise RuntimeError(f"{env_name}[{index}] requires id, title, and description")
        if not isinstance(server_name, str) or not server_name.strip():
            raise RuntimeError(f"{env_name}[{index}].server requires name")
        if not isinstance(server_url, str) or not server_url.strip():
            raise RuntimeError(f"{env_name}[{index}].server requires url")
        headers = item.server.get("headers", {})
        if not isinstance(headers, dict) or not all(
            isinstance(name, str) and isinstance(header_value, str)
            for name, header_value in headers.items()
        ):
            raise RuntimeError(f"{env_name}[{index}].server.headers must be a string map")
        allowed_tools = item.server.get("allowed_tools", item.server.get("allowedTools"))
        if allowed_tools is not None and (
            not isinstance(allowed_tools, list)
            or not all(isinstance(tool, str) and tool for tool in allowed_tools)
        ):
            raise RuntimeError(f"{env_name}[{index}].server.allowed_tools must be a string array")
        if item.id in seen_ids or server_name in seen_names:
            raise RuntimeError(f"{env_name} contains duplicate ids or server names")
        seen_ids.add(item.id)
        seen_names.add(server_name)
        items.append(item)
    return tuple(items)


def _optional_str_env(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
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
        if key in NON_NEGATIVE_INTEGER_ENTITLEMENT_LIMITS and value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                limit_errors.append(f"Limit '{key}' must be a non-negative integer or null")
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
    if old_user is not None:
        _protect_last_active_owner(store, tenant_id, old_user, old_user.role, status)
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


def _tenant_oauth_store(
    tenant_id: str,
) -> tuple[SQLiteEncryptedOAuthStore, str, str]:
    try:
        store = build_oauth_credential_store_from_env()
        provider_id = generic_oauth_config_from_env().provider_id
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Encrypted OAuth credential storage is not configured",
        ) from exc
    if not isinstance(store, SQLiteEncryptedOAuthStore):
        raise HTTPException(
            status_code=503,
            detail="Encrypted OAuth credential storage is required for tenant imports",
        )
    return store, provider_id, tenant_oauth_credential_key(provider_id, tenant_id)


def _parse_pi_openai_oauth_credential(payload: dict[str, Any]) -> OAuthCredentials:
    if payload.get("type") != "oauth":
        raise HTTPException(status_code=400, detail="Pi openai-codex credential must use OAuth")
    access_token = _required_pi_credential_string(payload, "access", max_length=131_072)
    refresh_token = _required_pi_credential_string(payload, "refresh", max_length=131_072)
    account_id = _required_pi_credential_string(payload, "accountId", max_length=512)
    expires = payload.get("expires")
    if isinstance(expires, bool) or not isinstance(expires, int | float):
        raise HTTPException(status_code=400, detail="Pi OAuth credential has an invalid expiry")
    expires_at = float(expires)
    if not math.isfinite(expires_at) or expires_at <= 0:
        raise HTTPException(status_code=400, detail="Pi OAuth credential has an invalid expiry")
    if expires_at >= 100_000_000_000:
        expires_at /= 1000
    if expires_at > 253_402_300_799:
        raise HTTPException(status_code=400, detail="Pi OAuth credential has an invalid expiry")
    return OAuthCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        account_id=account_id,
    )


def _required_pi_credential_string(
    payload: dict[str, Any],
    field: str,
    *,
    max_length: int,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"Pi OAuth credential has an invalid '{field}' field",
        )
    return value


def _tenant_oauth_credential_response(
    tenant_id: str,
    provider_id: str,
    credentials: OAuthCredentials | None,
) -> AdminTenantOAuthCredentialResponse:
    return AdminTenantOAuthCredentialResponse(
        tenant_id=tenant_id,
        provider_id=provider_id,
        source="pi",
        connected=credentials is not None,
        account_id=credentials.account_id if credentials is not None else None,
        expires_at=(
            datetime.fromtimestamp(credentials.expires_at, timezone.utc)
            if credentials is not None
            else None
        ),
    )


def _protect_last_active_owner(
    store: Any,
    tenant_id: str,
    user: TenantUser,
    next_role: TenantUserRole,
    next_status: TenantUserStatus,
) -> None:
    if user.role != TenantUserRole.OWNER or user.status != TenantUserStatus.ACTIVE:
        return
    if next_role == TenantUserRole.OWNER and next_status == TenantUserStatus.ACTIVE:
        return
    active_owners, total = store.list_tenant_users(
        tenant_id,
        role=TenantUserRole.OWNER,
        status=TenantUserStatus.ACTIVE,
        limit=2,
        offset=0,
    )
    _ = active_owners
    if total <= 1:
        raise HTTPException(status_code=409, detail="A tenant must retain an active owner")


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


def _configured_mcp_server_catalog(
    request: Request,
) -> tuple[AdminMCPServerCatalogItem, ...]:
    settings = getattr(request.app.state, "admin_store_settings", None)
    return settings.mcp_server_catalog if isinstance(settings, AdminStoreSettings) else ()


def _tenant_catalog_policy_errors(
    request: Request,
    tenant_id: str,
    payload: dict[str, Any],
) -> list[str]:
    store = _require_admin_store(request)
    policy = store.get_tenant_mcp_server_catalog_policy(tenant_id)
    catalog = {item.id: item.server for item in _configured_mcp_server_catalog(request)}
    return tenant_mcp_server_catalog_policy_errors(
        tenant_id,
        payload,
        policy,
        catalog,
    )


def _validate_catalog_assignment_subject(
    store: Any, tenant_id: str, subject_type: str, subject_id: str
) -> None:
    if subject_type == "user":
        if store.get_tenant_user_by_user_id(tenant_id, subject_id) is None:
            raise HTTPException(status_code=404, detail=f"Tenant user '{subject_id}' not found")
        return
    if subject_type == "role":
        if subject_id not in {role.value for role in TenantUserRole}:
            raise HTTPException(status_code=400, detail=f"Unknown tenant role '{subject_id}'")
        return
    raise HTTPException(status_code=400, detail="Subject type must be 'user' or 'role'")


def _mcp_server_catalog_assignment_response(
    assignment: SubjectMCPServerCatalogAssignment,
) -> AdminMCPServerCatalogAssignmentResponse:
    return AdminMCPServerCatalogAssignmentResponse(
        tenant_id=assignment.tenant_id,
        subject_type=assignment.subject_type,
        subject_id=assignment.subject_id,
        item_ids=list(assignment.item_ids),
        version=assignment.version,
        updated_by=assignment.updated_by,
        updated_at=assignment.updated_at,
    )


def _mcp_server_catalog_assignment_audit_values(
    assignment: SubjectMCPServerCatalogAssignment | None,
) -> dict[str, Any] | None:
    if assignment is None:
        return None
    return _mcp_server_catalog_assignment_response(assignment).model_dump(mode="json")


def _mcp_server_catalog_policy_audit_values(
    policy: TenantMCPServerCatalogPolicy | None,
) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "tenant_id": policy.tenant_id,
        "item_ids": list(policy.item_ids),
        "allow_custom_mcp_servers": policy.allow_custom_mcp_servers,
        "version": policy.version,
        "updated_by": policy.updated_by,
        "updated_at": policy.updated_at.isoformat(),
    }


def _execution_config_secret_source(
    request: Request,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    catalog = _configured_mcp_server_catalog(request)
    store = _require_admin_store(request)
    policy = store.get_tenant_mcp_server_catalog_policy(
        str(request.path_params.get("tenant_id", ""))
    )
    granted_ids = set(policy.item_ids) if policy is not None else None
    catalog_servers = {
        str(item.server["name"]): item.server
        for item in catalog
        if isinstance(item.server.get("name"), str)
        and (granted_ids is None or item.id in granted_ids)
    }

    source = json.loads(json.dumps(existing or {}))
    source_tools = source.get("tools")
    if not isinstance(source_tools, dict):
        source_tools = {}
        source["tools"] = source_tools
    existing_servers = source_tools.get("mcp_servers", source_tools.get("mcpServers", []))
    if isinstance(existing_servers, list):
        for server in existing_servers:
            if isinstance(server, dict) and isinstance(server.get("name"), str):
                catalog_servers[str(server["name"])] = server
    merged_servers = list(catalog_servers.values())
    source_tools["mcp_servers"] = merged_servers
    source_tools["mcpServers"] = merged_servers
    return source


def _restore_redacted_payload(
    payload: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    restored = _restore_redacted_value(payload, existing or {})
    if not isinstance(restored, dict):  # pragma: no cover - payload is typed as an object
        raise RuntimeError("Execution config must be an object")
    return restored


def _restore_redacted_value(value: Any, existing: Any) -> Any:
    if value == "<redacted>":
        return existing if not isinstance(existing, (dict, list)) else None
    if isinstance(value, dict):
        existing_dict = existing if isinstance(existing, dict) else {}
        return {
            key: _restore_redacted_value(item, existing_dict.get(key))
            for key, item in value.items()
            if key not in {"has_api_key", "has_headers", "has_extra_headers"}
        }
    if isinstance(value, list):
        existing_items = existing if isinstance(existing, list) else []
        existing_by_name = {
            item.get("name"): item
            for item in existing_items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        restored_items: list[Any] = []
        for index, item in enumerate(value):
            previous = existing_items[index] if index < len(existing_items) else None
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                previous = existing_by_name.get(item["name"], previous)
            restored_items.append(_restore_redacted_value(item, previous))
        return restored_items
    return value
