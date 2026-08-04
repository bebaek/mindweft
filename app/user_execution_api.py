from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.admin_store import (
    SQLiteTenantConfigStore,
    UserExecutionConfigConflictError,
    UserExecutionConfigRecord,
)
from app.models import Principal
from app.tenants import require_active_tenant_principal
from app.user_execution import validate_user_execution_config


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

    return router


def _require_user_execution_store(request: Request) -> SQLiteTenantConfigStore:
    store = getattr(request.app.state, "admin_store", None)
    if not isinstance(store, SQLiteTenantConfigStore):
        raise HTTPException(
            status_code=503,
            detail="User execution config storage is not configured",
        )
    return store


def _record_response(record: UserExecutionConfigRecord) -> UserExecutionConfigResponse:
    return UserExecutionConfigResponse(
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        config=record.config,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
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
