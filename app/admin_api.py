from __future__ import annotations

import os
from datetime import datetime
from typing import Any

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
from app.models import Message, Principal, Thread, ThreadContext, ThreadStatus

ADMIN_DB_PATH_ENV = "MINIGENT_ADMIN_DB_PATH"
ADMIN_ENCRYPTION_KEY_ENV = "MINIGENT_ADMIN_ENCRYPTION_KEY"


class AdminTenantListResponse(BaseModel):
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
    ) -> AdminTenantListResponse:
        _ = admin
        store = _require_admin_store(request)
        return AdminTenantListResponse(tenants=store.list_tenants())

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
