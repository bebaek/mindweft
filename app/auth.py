from __future__ import annotations

import json
import os

from fastapi import Header, HTTPException

from app.models import Principal

USER_HEADER = "X-Minigent-User-Id"
TENANT_HEADER = "X-Minigent-Tenant-Id"
ADMIN_HEADER = "X-Minigent-Admin"
AUTHORIZATION_HEADER = "Authorization"
AUTH_TOKENS_ENV = "MINIGENT_AUTH_TOKENS"


async def require_principal(
    authorization: str | None = Header(default=None),
    x_minigent_user_id: str | None = Header(default=None),
    x_minigent_tenant_id: str | None = Header(default=None),
    x_minigent_admin: str | None = Header(default=None),
) -> Principal:
    configured_tokens = _load_token_principals()
    bearer_token = _extract_bearer_token(authorization)

    if bearer_token is not None:
        principal = configured_tokens.get(bearer_token)
        if principal is None:
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        return principal

    if configured_tokens:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing bearer token. Provide an Authorization header using the Bearer scheme."
            ),
        )

    if not x_minigent_user_id or not x_minigent_tenant_id:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing authenticated principal. Provide "
                f"{AUTHORIZATION_HEADER}: Bearer <token> or "
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


def _extract_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header. Expected 'Bearer <token>'.",
        )
    return token.strip()


def _load_token_principals() -> dict[str, Principal]:
    raw = os.getenv(AUTH_TOKENS_ENV, "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{AUTH_TOKENS_ENV} must be valid JSON") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"{AUTH_TOKENS_ENV} must be a JSON object")

    principals: dict[str, Principal] = {}
    for token, value in parsed.items():
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"{AUTH_TOKENS_ENV} keys must be non-empty bearer tokens")
        if not isinstance(value, dict):
            raise RuntimeError(
                f"{AUTH_TOKENS_ENV} values must be objects with user_id and tenant_id"
            )
        principals[token] = Principal.model_validate(value)
    return principals
