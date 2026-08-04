from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, Request
from jwt import InvalidTokenError, PyJWK

from app.models import Principal
from app.session_auth import SESSION_COOKIE_NAME, principal_from_session_request

USER_HEADER = "X-Minigent-User-Id"
TENANT_HEADER = "X-Minigent-Tenant-Id"
ADMIN_HEADER = "X-Minigent-Admin"
AUTHORIZATION_HEADER = "Authorization"
AUTH_MODE_ENV = "MINIGENT_AUTH_MODE"
AUTH_TOKENS_ENV = "MINIGENT_AUTH_TOKENS"
JWT_ISSUER_ENV = "MINIGENT_JWT_ISSUER"
JWT_AUDIENCE_ENV = "MINIGENT_JWT_AUDIENCE"
JWT_ALGORITHMS_ENV = "MINIGENT_JWT_ALGORITHMS"
JWT_SHARED_SECRET_ENV = "MINIGENT_JWT_SHARED_SECRET"
JWT_JWKS_URL_ENV = "MINIGENT_JWT_JWKS_URL"
JWT_TENANT_CLAIM_ENV = "MINIGENT_JWT_TENANT_CLAIM"
JWT_USER_CLAIM_ENV = "MINIGENT_JWT_USER_CLAIM"
JWT_ADMIN_CLAIM_ENV = "MINIGENT_JWT_ADMIN_CLAIM"
JWT_JWKS_CACHE_SECONDS_ENV = "MINIGENT_JWT_JWKS_CACHE_SECONDS"

AUTH_MODE_DEV_HEADERS = "dev-headers"
AUTH_MODE_STATIC_TOKENS = "static-tokens"
AUTH_MODE_JWT = "jwt"

_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class AuthSettings:
    mode: str
    static_tokens: dict[str, Principal]
    jwt_issuer: str | None
    jwt_audience: str | None
    jwt_algorithms: list[str]
    jwt_shared_secret: str | None
    jwt_jwks_url: str | None
    jwt_tenant_claim: str
    jwt_user_claim: str
    jwt_admin_claim: str
    jwt_jwks_cache_seconds: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuthSettings:
        lookup = os.environ if env is None else env
        mode = (
            lookup.get(AUTH_MODE_ENV, AUTH_MODE_DEV_HEADERS).strip().lower()
            or AUTH_MODE_DEV_HEADERS
        )
        static_tokens = _load_token_principals(lookup) if mode == AUTH_MODE_STATIC_TOKENS else {}
        jwt_algorithms = _load_json_or_csv_list_env(JWT_ALGORITHMS_ENV, lookup)
        return cls(
            mode=mode,
            static_tokens=static_tokens,
            jwt_issuer=_optional_env(JWT_ISSUER_ENV, lookup),
            jwt_audience=_optional_env(JWT_AUDIENCE_ENV, lookup),
            jwt_algorithms=jwt_algorithms or ["RS256"],
            jwt_shared_secret=_optional_env(JWT_SHARED_SECRET_ENV, lookup),
            jwt_jwks_url=_optional_env(JWT_JWKS_URL_ENV, lookup),
            jwt_tenant_claim=lookup.get(JWT_TENANT_CLAIM_ENV, "tenant_id"),
            jwt_user_claim=lookup.get(JWT_USER_CLAIM_ENV, "sub"),
            jwt_admin_claim=lookup.get(JWT_ADMIN_CLAIM_ENV, "is_admin"),
            jwt_jwks_cache_seconds=int(lookup.get(JWT_JWKS_CACHE_SECONDS_ENV, "300")),
        )


def validate_auth_settings() -> AuthSettings:
    settings = _load_auth_settings()
    if settings.mode == AUTH_MODE_STATIC_TOKENS and not settings.static_tokens:
        raise RuntimeError(f"{AUTH_TOKENS_ENV} is required when {AUTH_MODE_ENV}=static-tokens")
    if settings.mode == AUTH_MODE_JWT:
        algorithms = {algorithm.upper() for algorithm in settings.jwt_algorithms}
        if any(algorithm.startswith("HS") for algorithm in algorithms):
            if not settings.jwt_shared_secret:
                raise RuntimeError(
                    f"{JWT_SHARED_SECRET_ENV} is required for HMAC JWT algorithms in "
                    f"{AUTH_MODE_ENV}=jwt"
                )
        elif not settings.jwt_jwks_url:
            raise RuntimeError(
                f"{JWT_JWKS_URL_ENV} is required for asymmetric JWT algorithms in "
                f"{AUTH_MODE_ENV}=jwt"
            )
    return settings


async def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_minigent_user_id: str | None = Header(default=None),
    x_minigent_tenant_id: str | None = Header(default=None),
    x_minigent_admin: str | None = Header(default=None),
) -> Principal:
    settings = validate_auth_settings()

    if authorization is None and request.cookies.get(SESSION_COOKIE_NAME):
        session_principal = principal_from_session_request(request)
        if session_principal is not None:
            return session_principal

    if settings.mode == AUTH_MODE_DEV_HEADERS:
        return _principal_from_headers(
            x_minigent_user_id=x_minigent_user_id,
            x_minigent_tenant_id=x_minigent_tenant_id,
            x_minigent_admin=x_minigent_admin,
        )
    if settings.mode == AUTH_MODE_STATIC_TOKENS:
        return _principal_from_static_token(authorization, settings)
    if settings.mode == AUTH_MODE_JWT:
        return await _principal_from_jwt(authorization, settings)

    raise RuntimeError(
        f"Unsupported {AUTH_MODE_ENV} '{settings.mode}'. Expected "
        f"'{AUTH_MODE_DEV_HEADERS}', '{AUTH_MODE_STATIC_TOKENS}', or '{AUTH_MODE_JWT}'."
    )


async def require_admin_principal(
    principal: Principal = Depends(require_principal),
) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return principal


def _parse_bool_header(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _extract_bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token. Provide an Authorization header using the Bearer scheme.",
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header. Expected 'Bearer <token>'.",
        )
    return token.strip()


def _load_token_principals(env: Mapping[str, str] | None = None) -> dict[str, Principal]:
    lookup = os.environ if env is None else env
    raw = lookup.get(AUTH_TOKENS_ENV, "").strip()
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


def _load_auth_settings(env: Mapping[str, str] | None = None) -> AuthSettings:
    return AuthSettings.from_env(env)


def _optional_env(name: str, env: Mapping[str, str] | None = None) -> str | None:
    lookup = os.environ if env is None else env
    value = lookup.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _load_json_or_csv_list_env(name: str, env: Mapping[str, str] | None = None) -> list[str]:
    lookup = os.environ if env is None else env
    raw = lookup.get(name, "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{name} must be valid JSON") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise RuntimeError(f"{name} must be a JSON array of strings")
        return [item for item in parsed if item]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _principal_from_headers(
    *,
    x_minigent_user_id: str | None,
    x_minigent_tenant_id: str | None,
    x_minigent_admin: str | None,
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


def _principal_from_static_token(authorization: str | None, settings: AuthSettings) -> Principal:
    if not settings.static_tokens:
        raise RuntimeError(f"{AUTH_TOKENS_ENV} is required when {AUTH_MODE_ENV}=static-tokens")

    bearer_token = _extract_bearer_token(authorization)
    principal = settings.static_tokens.get(bearer_token)
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return principal


async def _principal_from_jwt(authorization: str | None, settings: AuthSettings) -> Principal:
    token = _extract_bearer_token(authorization)
    key = await _resolve_jwt_signing_key(token, settings)

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=settings.jwt_algorithms,
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid JWT: {exc}") from exc

    return _principal_from_claims(claims, settings)


async def _resolve_jwt_signing_key(token: str, settings: AuthSettings) -> Any:
    algorithms = {algorithm.upper() for algorithm in settings.jwt_algorithms}
    if any(algorithm.startswith("HS") for algorithm in algorithms):
        if not settings.jwt_shared_secret:
            raise RuntimeError(
                f"{JWT_SHARED_SECRET_ENV} is required for HMAC JWT algorithms in {AUTH_MODE_ENV}=jwt"
            )
        return settings.jwt_shared_secret

    if settings.jwt_jwks_url:
        return await _get_signing_key_from_jwks(token, settings)

    raise RuntimeError(
        f"{JWT_JWKS_URL_ENV} is required for asymmetric JWT algorithms in {AUTH_MODE_ENV}=jwt"
    )


def _principal_from_claims(claims: dict[str, Any], settings: AuthSettings) -> Principal:
    user_id = claims.get(settings.jwt_user_claim)
    tenant_id = claims.get(settings.jwt_tenant_claim)
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(
            status_code=401,
            detail=f"JWT is missing required '{settings.jwt_user_claim}' claim",
        )
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(
            status_code=401,
            detail=f"JWT is missing required '{settings.jwt_tenant_claim}' claim",
        )
    return Principal(
        user_id=user_id,
        tenant_id=tenant_id,
        is_admin=_coerce_claim_bool(claims.get(settings.jwt_admin_claim)),
    )


def _coerce_claim_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


async def _get_signing_key_from_jwks(token: str, settings: AuthSettings) -> Any:
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise HTTPException(status_code=401, detail="JWT is missing required 'kid' header")

    jwks = await _get_jwks_document(settings.jwt_jwks_url or "", settings.jwt_jwks_cache_seconds)
    key = _find_jwk_by_kid(jwks, kid)
    if key is None:
        jwks = await _get_jwks_document(
            settings.jwt_jwks_url or "",
            settings.jwt_jwks_cache_seconds,
            force_refresh=True,
        )
        key = _find_jwk_by_kid(jwks, kid)
    if key is None:
        raise HTTPException(status_code=401, detail=f"JWT signing key '{kid}' not found")
    return PyJWK.from_dict(key).key


def _find_jwk_by_kid(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    keys = jwks.get("keys")
    if not isinstance(keys, list):
        raise RuntimeError("JWKS response must include a 'keys' array")
    for key in keys:
        if isinstance(key, dict) and key.get("kid") == kid:
            return key
    return None


async def _get_jwks_document(
    jwks_url: str,
    cache_seconds: int,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    cached = _JWKS_CACHE.get(jwks_url)
    now = time.time()
    if not force_refresh and cached is not None and cached[0] > now:
        return cached[1]

    document = await _fetch_jwks_document(jwks_url)
    _JWKS_CACHE[jwks_url] = (now + max(cache_seconds, 0), document)
    return document


async def _fetch_jwks_document(jwks_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            response = await client.get(jwks_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"JWKS request failed: {exc}") from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("JWKS response must be a JSON object")
    return payload
