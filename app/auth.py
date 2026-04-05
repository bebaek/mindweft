from __future__ import annotations

from fastapi import Header, HTTPException

from app.models import Principal

USER_HEADER = "X-Minigent-User-Id"
TENANT_HEADER = "X-Minigent-Tenant-Id"
ADMIN_HEADER = "X-Minigent-Admin"


async def require_principal(
    x_minigent_user_id: str | None = Header(default=None),
    x_minigent_tenant_id: str | None = Header(default=None),
    x_minigent_admin: str | None = Header(default=None),
) -> Principal:
    if not x_minigent_user_id or not x_minigent_tenant_id:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing authenticated principal. Provide "
                f"{USER_HEADER} and {TENANT_HEADER} headers."
            ),
        )

    return Principal(
        user_id=x_minigent_user_id,
        tenant_id=x_minigent_tenant_id,
        is_admin=_parse_bool_header(x_minigent_admin),
    )


def _parse_bool_header(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}
