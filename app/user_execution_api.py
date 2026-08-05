from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from app.admin_store import (
    SQLiteTenantConfigStore,
    UserExecutionConfigConflictError,
    UserExecutionConfigRecord,
    UserExecutionCredentialConflictError,
    UserExecutionCredentialMetadata,
    UserExecutionCredentialRecord,
)
from app.models import Principal
from app.tenants import require_active_tenant_principal
from app.user_execution import validate_user_execution_config
from app.user_mcp_access import get_user_execution_status, list_user_mcp_access


class UserExecutionConfigPutRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    expected_version: int | None = Field(default=None, ge=0)


class UserExecutionConfigValidateRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class UserExecutionConfigResponse(BaseModel):
    tenant_id: str
    user_id: str
    config: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class UserExecutionCredentialPutRequest(BaseModel):
    header_name: str = Field(min_length=1, max_length=128)
    header_value: str = Field(min_length=1, max_length=16_384)
    expected_version: int | None = Field(default=None, ge=0)

    @field_validator("header_name")
    @classmethod
    def validate_header_name(cls, value: str) -> str:
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value):
            raise ValueError("header_name must be a valid HTTP field name")
        if value.lower() in {
            "host",
            "connection",
            "content-length",
            "transfer-encoding",
            "forwarded",
            "x-forwarded-for",
            "x-forwarded-host",
            "proxy-authorization",
        }:
            raise ValueError("header_name is not allowed for personal MCP credentials")
        return value

    @field_validator("header_value")
    @classmethod
    def validate_header_value(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("header_value must not contain newlines")
        return value


class UserExecutionCredentialResponse(BaseModel):
    tenant_id: str
    user_id: str
    credential_ref: str
    header_name: str
    version: int
    created_at: datetime
    updated_at: datetime


class UserExecutionCredentialListResponse(BaseModel):
    items: list[UserExecutionCredentialResponse] = Field(default_factory=list)


class UserMCPServerAccessResponse(BaseModel):
    id: str
    name: str
    source: str
    allowed_tools: list[str] | None = None
    credential_configured: bool = False


class UserMCPAccessResponse(BaseModel):
    tenant_id: str
    user_id: str
    endpoint_path: str
    personal_mcp_servers_allowed: bool
    personal_servers: list[UserMCPServerAccessResponse] = Field(default_factory=list)
    shared_servers: list[UserMCPServerAccessResponse] = Field(default_factory=list)


class UserMCPStatusFinding(BaseModel):
    code: str
    severity: str
    message: str
    remediation: str


class UserMCPStatusResponse(BaseModel):
    tenant_id: str
    user_id: str
    endpoint_path: str
    execution_configured: bool
    execution_config_version: int | None = None
    encrypted_credentials_available: bool
    personal_mcp_servers_allowed: bool
    skills: int
    mcp_servers: int
    capability_profiles: int
    agents: int
    findings: list[UserMCPStatusFinding] = Field(default_factory=list)


class UserExecutionConfigValidationResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    normalized_config: dict[str, Any] | None = None


def build_user_execution_router() -> APIRouter:
    router = APIRouter(prefix="/me", tags=["user-execution"])

    @router.get("/execution-config", response_model=UserExecutionConfigResponse)
    async def get_user_execution_config(
        request: Request,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
    ) -> UserExecutionConfigResponse:
        store = _require_user_execution_store(request)
        record = store.get_user_execution_config(principal.tenant_id, principal.user_id)
        if record is None:
            raise HTTPException(status_code=404, detail="User execution config not found")
        return _record_response(record)

    @router.put("/execution-config", response_model=UserExecutionConfigResponse)
    async def put_user_execution_config(
        body: UserExecutionConfigPutRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
    ) -> UserExecutionConfigResponse:
        report = validate_user_execution_config(body.config)
        if not report.valid or report.config is None:
            raise HTTPException(
                status_code=422,
                detail={"message": "Invalid user execution config", "errors": report.errors},
            )
        store = _require_user_execution_store(request)
        try:
            record = store.upsert_user_execution_config(
                principal.tenant_id,
                principal.user_id,
                report.config.model_dump(mode="json", exclude_none=True),
                expected_version=body.expected_version,
            )
        except UserExecutionConfigConflictError as exc:
            raise _version_conflict(exc) from exc
        return _record_response(record)

    @router.post(
        "/execution-config/validate",
        response_model=UserExecutionConfigValidationResponse,
    )
    async def validate_execution_config(
        body: UserExecutionConfigValidateRequest,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
    ) -> UserExecutionConfigValidationResponse:
        _ = principal
        report = validate_user_execution_config(body.config)
        return UserExecutionConfigValidationResponse(
            valid=report.valid,
            errors=report.errors,
            normalized_config=(
                report.config.model_dump(mode="json", exclude_none=True)
                if report.config is not None
                else None
            ),
        )

    @router.get("/mcp-status", response_model=UserMCPStatusResponse)
    async def get_user_mcp_status(
        request: Request,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
    ) -> UserMCPStatusResponse:
        return UserMCPStatusResponse.model_validate(
            get_user_execution_status(request.app, principal)
        )

    @router.get("/mcp-access", response_model=UserMCPAccessResponse)
    async def get_user_mcp_access(
        request: Request,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
    ) -> UserMCPAccessResponse:
        return UserMCPAccessResponse.model_validate(list_user_mcp_access(request.app, principal))

    @router.delete("/execution-config", status_code=204)
    async def delete_user_execution_config(
        request: Request,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
        expected_version: Annotated[int | None, Query(ge=0)] = None,
    ) -> Response:
        store = _require_user_execution_store(request)
        try:
            deleted = store.delete_user_execution_config(
                principal.tenant_id,
                principal.user_id,
                expected_version=expected_version,
            )
        except UserExecutionConfigConflictError as exc:
            raise _version_conflict(exc) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="User execution config not found")
        return Response(status_code=204)

    @router.get(
        "/execution-credentials",
        response_model=UserExecutionCredentialListResponse,
    )
    async def list_user_execution_credentials(
        request: Request,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
    ) -> UserExecutionCredentialListResponse:
        store = _require_encrypted_user_execution_credential_store(request)
        records = store.list_user_execution_credentials(principal.tenant_id, principal.user_id)
        return UserExecutionCredentialListResponse(
            items=[_credential_response(record) for record in records]
        )

    @router.put(
        "/execution-credentials/{credential_ref}",
        response_model=UserExecutionCredentialResponse,
    )
    async def put_user_execution_credential(
        credential_ref: str,
        body: UserExecutionCredentialPutRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
    ) -> UserExecutionCredentialResponse:
        _validate_credential_ref(credential_ref)
        store = _require_encrypted_user_execution_credential_store(request)
        try:
            record = store.upsert_user_execution_credential(
                principal.tenant_id,
                principal.user_id,
                credential_ref,
                header_name=body.header_name,
                header_value=body.header_value,
                expected_version=body.expected_version,
            )
        except UserExecutionCredentialConflictError as exc:
            raise _credential_version_conflict(exc) from exc
        return _credential_response(record)

    @router.delete("/execution-credentials/{credential_ref}", status_code=204)
    async def delete_user_execution_credential(
        credential_ref: str,
        request: Request,
        principal: Annotated[Principal, Depends(require_active_tenant_principal)],
        expected_version: Annotated[int | None, Query(ge=0)] = None,
    ) -> Response:
        _validate_credential_ref(credential_ref)
        store = _require_encrypted_user_execution_credential_store(request)
        try:
            deleted = store.delete_user_execution_credential(
                principal.tenant_id,
                principal.user_id,
                credential_ref,
                expected_version=expected_version,
            )
        except UserExecutionCredentialConflictError as exc:
            raise _credential_version_conflict(exc) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="User execution credential not found")
        return Response(status_code=204)

    return router


def _require_user_execution_store(request: Request) -> SQLiteTenantConfigStore:
    store = getattr(request.app.state, "admin_store", None)
    if not isinstance(store, SQLiteTenantConfigStore):
        raise HTTPException(
            status_code=503,
            detail="User execution config storage is not configured",
        )
    return store


def _require_encrypted_user_execution_credential_store(
    request: Request,
) -> SQLiteTenantConfigStore:
    store = _require_user_execution_store(request)
    if not store.user_execution_credentials_encrypted:
        raise HTTPException(
            status_code=503,
            detail="Encrypted user execution credential storage is not configured",
        )
    return store


def _validate_credential_ref(credential_ref: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,255}", credential_ref):
        raise HTTPException(status_code=400, detail="Invalid user execution credential reference")


def _credential_response(
    record: UserExecutionCredentialRecord | UserExecutionCredentialMetadata,
) -> UserExecutionCredentialResponse:
    return UserExecutionCredentialResponse(
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        credential_ref=record.credential_ref,
        header_name=record.header_name,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _record_response(record: UserExecutionConfigRecord) -> UserExecutionConfigResponse:
    return UserExecutionConfigResponse(
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        config=record.config,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _credential_version_conflict(
    exc: UserExecutionCredentialConflictError,
) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "message": "User execution credential version conflict",
            "expected_version": exc.expected_version,
            "actual_version": exc.actual_version,
        },
    )


def _version_conflict(exc: UserExecutionConfigConflictError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "message": "User execution config version conflict",
            "expected_version": exc.expected_version,
            "actual_version": exc.actual_version,
        },
    )
