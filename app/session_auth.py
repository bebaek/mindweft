from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from jwt import InvalidTokenError
from pydantic import BaseModel, Field

from app.models import Principal
from app.rate_limits import RateLimitPolicy
from minigent_config.unified_config import normalize_mindweft_env

SESSION_CREDENTIALS_ENV = "MINIGENT_SESSION_CREDENTIALS"
SESSION_SECRET_ENV = "MINIGENT_SESSION_SECRET"
SESSION_TTL_SECONDS_ENV = "MINIGENT_SESSION_TTL_SECONDS"
SESSION_COOKIE_SECURE_ENV = "MINIGENT_SESSION_COOKIE_SECURE"
SESSION_ALLOWED_ORIGINS_ENV = "MINIGENT_SESSION_ALLOWED_ORIGINS"
SESSION_LOGIN_RATE_LIMIT_CAPACITY_ENV = "MINIGENT_SESSION_LOGIN_RATE_LIMIT_CAPACITY"
SESSION_LOGIN_RATE_LIMIT_REFILL_ENV = "MINIGENT_SESSION_LOGIN_RATE_LIMIT_REFILL_PER_SECOND"
SESSION_COOKIE_NAME = "mindweft_session"
LEGACY_SESSION_COOKIE_NAME = "minigent_session"
SESSION_TOKEN_ISSUER = "mindweft-console"
LEGACY_SESSION_TOKEN_ISSUER = "minigent-console"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_KEY_LENGTH = 32
_SCRYPT_MAX_MEMORY = 64 * 1024 * 1024
_DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$HfrUe6rycPIFqZxD1xFJMVljivEdVIZMerV/HA9C870="
)


@dataclass(frozen=True)
class SessionCredential:
    password_hash: str
    principal: Principal


@dataclass(frozen=True)
class SessionAuthSettings:
    credentials: dict[str, SessionCredential]
    secret: str | None
    ttl_seconds: int
    cookie_secure: bool
    allowed_origins: tuple[str, ...]
    login_rate_limit: RateLimitPolicy

    @property
    def enabled(self) -> bool:
        return self.secret is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SessionAuthSettings:
        lookup = normalize_mindweft_env(dict(os.environ if env is None else env))
        return cls(
            credentials=_load_credentials(lookup),
            secret=_optional_env(SESSION_SECRET_ENV, lookup),
            ttl_seconds=_positive_int_env(SESSION_TTL_SECONDS_ENV, lookup, default=28_800),
            cookie_secure=_bool_env(SESSION_COOKIE_SECURE_ENV, lookup, default=True),
            allowed_origins=tuple(_list_env(SESSION_ALLOWED_ORIGINS_ENV, lookup)),
            login_rate_limit=RateLimitPolicy(
                user_capacity=_non_negative_int_env(
                    SESSION_LOGIN_RATE_LIMIT_CAPACITY_ENV, lookup, default=10
                ),
                user_refill_per_second=_positive_float_env(
                    SESSION_LOGIN_RATE_LIMIT_REFILL_ENV, lookup, default=1.0 / 60.0
                ),
            ),
        )


class SessionLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class SessionStatusResponse(BaseModel):
    enabled: bool
    authenticated: bool
    principal: Principal | None = None


class PasswordSetupRequest(BaseModel):
    token: str = Field(min_length=32, max_length=512)


class PasswordSetupCompleteRequest(PasswordSetupRequest):
    password: str = Field(min_length=12, max_length=1024)


class PasswordSetupStatusResponse(BaseModel):
    valid: bool
    username: str | None = None
    expires_at: datetime | None = None


def validate_session_auth_settings(
    env: Mapping[str, str] | None = None,
) -> SessionAuthSettings:
    settings = SessionAuthSettings.from_env(env)
    if settings.credentials and settings.secret is None:
        raise RuntimeError(
            f"{_canonical_env_name(SESSION_SECRET_ENV)} is required when {_canonical_env_name(SESSION_CREDENTIALS_ENV)} is configured"
        )
    if settings.secret is not None and len(settings.secret.encode()) < 32:
        raise RuntimeError(f"{_canonical_env_name(SESSION_SECRET_ENV)} must be at least 32 bytes")
    return settings


def build_session_auth_router() -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["authentication"])

    @router.get("/session", response_model=SessionStatusResponse)
    async def session_status(request: Request) -> SessionStatusResponse:
        settings = validate_session_auth_settings()
        principal = principal_from_session_request(request, settings=settings, required=False)
        return SessionStatusResponse(
            enabled=settings.enabled,
            authenticated=principal is not None,
            principal=principal,
        )

    @router.post("/session", response_model=SessionStatusResponse)
    async def login(
        body: SessionLoginRequest, request: Request, response: Response
    ) -> SessionStatusResponse:
        settings = validate_session_auth_settings()
        if not settings.enabled:
            raise HTTPException(status_code=404, detail="Session authentication is not configured")
        require_same_origin(request, settings)
        _enforce_login_rate_limit(request, body.username, settings)
        environment_username = body.username
        credential = settings.credentials.get(environment_username)
        local_username = body.username.strip().lower()
        store = getattr(request.app.state, "admin_store", None)
        identity = (
            store.get_local_identity(local_username) if credential is None and store else None
        )
        expected_hash = (
            credential.password_hash
            if credential is not None
            else identity.password_hash
            if identity is not None
            else _DUMMY_PASSWORD_HASH
        )
        if not verify_password(body.password, expected_hash) or (
            credential is None and identity is None
        ):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        if credential is not None:
            principal = credential.principal
            source = "environment"
            version = 0
        else:
            if identity is None:  # pragma: no cover - narrowed by credential check
                raise HTTPException(status_code=401, detail="Invalid username or password")
            principal = _principal_for_local_identity(request, identity)
            if principal is None:  # pragma: no cover - required=True raises instead
                raise HTTPException(status_code=403, detail="Account or tenant is not active")
            source = "local"
            version = identity.credential_version
        token = _encode_session(
            principal,
            settings,
            username=environment_username if credential is not None else local_username,
            source=source,
            credential_version=version,
        )
        _set_session_cookie(response, token, settings)
        return SessionStatusResponse(enabled=True, authenticated=True, principal=principal)

    @router.post("/password/setup/status", response_model=PasswordSetupStatusResponse)
    async def password_setup_status(
        body: PasswordSetupRequest, request: Request
    ) -> PasswordSetupStatusResponse:
        settings = validate_session_auth_settings()
        if not settings.enabled:
            raise HTTPException(status_code=404, detail="Session authentication is not configured")
        require_same_origin(request, settings)
        setup = _valid_password_setup(request, body.token)
        if setup is None:
            return PasswordSetupStatusResponse(valid=False)
        return PasswordSetupStatusResponse(
            valid=True,
            username=setup.username,
            expires_at=setup.expires_at,
        )

    @router.post("/password/setup", response_model=SessionStatusResponse)
    async def complete_password_setup(
        body: PasswordSetupCompleteRequest, request: Request, response: Response
    ) -> SessionStatusResponse:
        settings = validate_session_auth_settings()
        if not settings.enabled:
            raise HTTPException(status_code=404, detail="Session authentication is not configured")
        require_same_origin(request, settings)
        setup = _valid_password_setup(request, body.token)
        if setup is None:
            raise HTTPException(status_code=400, detail="Password setup link is invalid or expired")
        store = _require_admin_store(request)
        identity = store.consume_password_setup(
            token_hash=_token_hash(body.token),
            password_hash=hash_password(body.password),
            now=datetime.now(timezone.utc),
        )
        if identity is None:
            raise HTTPException(status_code=409, detail="Password setup link has already been used")
        principal = _principal_for_local_identity(request, identity)
        if principal is None:  # pragma: no cover - required=True raises instead
            raise HTTPException(status_code=403, detail="Account or tenant is not active")
        token = _encode_session(
            principal,
            settings,
            username=identity.username,
            source="local",
            credential_version=identity.credential_version,
        )
        _set_session_cookie(response, token, settings)
        return SessionStatusResponse(enabled=True, authenticated=True, principal=principal)

    @router.delete("/session", status_code=204)
    async def logout(request: Request, response: Response) -> None:
        settings = validate_session_auth_settings()
        if settings.enabled:
            require_same_origin(request, settings)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        response.delete_cookie(
            LEGACY_SESSION_COOKIE_NAME,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )

    return router


def has_session_cookie(request: Request) -> bool:
    return SESSION_COOKIE_NAME in request.cookies or LEGACY_SESSION_COOKIE_NAME in request.cookies


def principal_from_session_request(
    request: Request,
    *,
    settings: SessionAuthSettings | None = None,
    required: bool = True,
) -> Principal | None:
    settings = settings or validate_session_auth_settings()
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token is None:
        token = request.cookies.get(LEGACY_SESSION_COOKIE_NAME)
    if not token:
        if required:
            raise HTTPException(status_code=401, detail="Authentication required")
        return None
    if not settings.enabled or settings.secret is None:
        if required:
            raise HTTPException(status_code=401, detail="Invalid session")
        return None
    try:
        payload = jwt.decode(
            token,
            settings.secret,
            algorithms=["HS256"],
            issuer=(SESSION_TOKEN_ISSUER, LEGACY_SESSION_TOKEN_ISSUER),
            options={
                "require": [
                    "exp",
                    "iat",
                    "iss",
                    "sub",
                    "tenant_id",
                    "is_admin",
                    "username",
                    "source",
                    "credential_version",
                ]
            },
        )
        principal = _principal_from_session_payload(request, payload, settings)
        if principal is None:
            raise ValueError("Session credential is no longer active")
    except (InvalidTokenError, KeyError, TypeError, ValueError):
        if required:
            raise HTTPException(status_code=401, detail="Invalid or expired session") from None
        return None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_same_origin(request, settings)
    return principal


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("Password must not be empty")
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_KEY_LENGTH,
        maxmem=_SCRYPT_MAX_MEMORY,
    )
    return "$".join(
        [
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(derived).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    parsed = _parse_password_hash(encoded)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_SCRYPT_MAX_MEMORY,
        )
    except ValueError:
        return False
    return secrets.compare_digest(actual, expected)


def _parse_password_hash(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    try:
        algorithm, n_text, r_text, p_text, salt_text, expected_text = encoded.split("$")
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(expected_text, validate=True)
    except (ValueError, TypeError):
        return None
    if algorithm != "scrypt" or (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
        return None
    if len(salt) < 16 or len(expected) != _SCRYPT_KEY_LENGTH:
        return None
    return n, r, p, salt, expected


def require_same_origin(request: Request, settings: SessionAuthSettings) -> None:
    origin = request.headers.get("origin")
    if not origin:
        raise HTTPException(status_code=403, detail="Origin header required for session request")
    allowed = set(settings.allowed_origins)
    allowed.add(_request_origin(request))
    if origin.rstrip("/") not in {item.rstrip("/") for item in allowed}:
        raise HTTPException(status_code=403, detail="Cross-origin session request rejected")


def _encode_session(
    principal: Principal,
    settings: SessionAuthSettings,
    *,
    username: str,
    source: str,
    credential_version: int,
) -> str:
    if settings.secret is None:  # pragma: no cover - validated by caller
        raise RuntimeError(f"{_canonical_env_name(SESSION_SECRET_ENV)} is required")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": SESSION_TOKEN_ISSUER,
            "sub": principal.user_id,
            "tenant_id": principal.tenant_id,
            "is_admin": principal.is_admin,
            "username": username,
            "source": source,
            "credential_version": credential_version,
            "iat": now,
            "exp": now + settings.ttl_seconds,
        },
        settings.secret,
        algorithm="HS256",
    )


def _set_session_cookie(response: Response, token: str, settings: SessionAuthSettings) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def _principal_from_session_payload(
    request: Request,
    payload: dict[str, Any],
    settings: SessionAuthSettings,
) -> Principal | None:
    username = payload.get("username")
    source = payload.get("source")
    version = payload.get("credential_version")
    if not isinstance(username, str) or not isinstance(source, str) or not isinstance(version, int):
        return None
    if source == "environment":
        credential = settings.credentials.get(username)
        if credential is None or version != 0:
            return None
        principal = credential.principal
    elif source == "local":
        store = getattr(request.app.state, "admin_store", None)
        identity = store.get_local_identity(username) if store is not None else None
        if identity is None or identity.credential_version != version:
            return None
        principal = _principal_for_local_identity(request, identity, required=False)
        if principal is None:
            return None
    else:
        return None
    if (
        principal.user_id != payload.get("sub")
        or principal.tenant_id != payload.get("tenant_id")
        or principal.is_admin != payload.get("is_admin")
    ):
        return None
    return principal


def _principal_for_local_identity(
    request: Request, identity: Any, *, required: bool = True
) -> Principal | None:
    store = _require_admin_store(request)
    tenant = store.get_tenant(identity.tenant_id)
    membership = store.get_tenant_user_by_user_id(identity.tenant_id, identity.user_id)
    active = (
        not identity.disabled
        and tenant is not None
        and tenant.status.value == "active"
        and membership is not None
        and membership.status.value == "active"
    )
    if not active:
        if required:
            raise HTTPException(status_code=403, detail="Account or tenant is not active")
        return None
    return Principal(user_id=identity.user_id, tenant_id=identity.tenant_id, is_admin=False)


def _valid_password_setup(request: Request, token: str) -> Any | None:
    store = _require_admin_store(request)
    setup = store.get_password_setup(_token_hash(token))
    if setup is None or setup.used_at is not None or setup.expires_at <= datetime.now(timezone.utc):
        return None
    tenant = store.get_tenant(setup.tenant_id)
    membership = store.get_tenant_user_by_user_id(setup.tenant_id, setup.user_id)
    if (
        tenant is None
        or tenant.status.value not in {"provisioning", "active"}
        or membership is None
        or membership.status.value not in {"invited", "active"}
    ):
        return None
    return setup


def _require_admin_store(request: Request) -> Any:
    store = getattr(request.app.state, "admin_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Administration store is not configured")
    return store


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _enforce_login_rate_limit(
    request: Request, username: str, settings: SessionAuthSettings
) -> None:
    policy = settings.login_rate_limit
    if not policy.enabled:
        return
    limiter = request.app.state.rate_limiter
    username_key = hashlib.sha256(username.strip().lower().encode()).hexdigest()
    decision = limiter.consume("session-login", "session-auth", username_key, policy)
    if not decision.allowed:
        retry_after = max(1, decision.retry_after_seconds)
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def _request_origin(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    scheme = forwarded_proto or request.url.scheme
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def _load_credentials(env: Mapping[str, str]) -> dict[str, SessionCredential]:
    raw = env.get(SESSION_CREDENTIALS_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{_canonical_env_name(SESSION_CREDENTIALS_ENV)} must be valid JSON"
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError(
            f"{_canonical_env_name(SESSION_CREDENTIALS_ENV)} must be a non-empty JSON object"
        )
    credentials: dict[str, SessionCredential] = {}
    for username, value in parsed.items():
        if not isinstance(username, str) or not username.strip():
            raise RuntimeError(
                f"{_canonical_env_name(SESSION_CREDENTIALS_ENV)} usernames must be non-empty strings"
            )
        if not isinstance(value, dict):
            raise RuntimeError(
                f"{_canonical_env_name(SESSION_CREDENTIALS_ENV)} credential values must be objects"
            )
        password_hash = value.get("password_hash")
        principal_value: Any = value.get("principal")
        if not isinstance(password_hash, str) or not _valid_password_hash(password_hash):
            raise RuntimeError(
                f"{_canonical_env_name(SESSION_CREDENTIALS_ENV)} credential '{username}' has an invalid password_hash"
            )
        try:
            principal = Principal.model_validate(principal_value)
        except ValueError as exc:
            raise RuntimeError(
                f"{_canonical_env_name(SESSION_CREDENTIALS_ENV)} credential '{username}' has an invalid principal"
            ) from exc
        credentials[username] = SessionCredential(password_hash=password_hash, principal=principal)
    return credentials


def _valid_password_hash(value: str) -> bool:
    return _parse_password_hash(value) is not None


def _optional_env(name: str, env: Mapping[str, str]) -> str | None:
    value = env.get(name)
    return value.strip() if value and value.strip() else None


def _canonical_env_name(name: str) -> str:
    return name.replace("MINIGENT_", "MINDWEFT_", 1)


def _positive_int_env(name: str, env: Mapping[str, str], *, default: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_canonical_env_name(name)} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{_canonical_env_name(name)} must be greater than zero")
    return value


def _non_negative_int_env(name: str, env: Mapping[str, str], *, default: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_canonical_env_name(name)} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{_canonical_env_name(name)} must be zero or greater")
    return value


def _positive_float_env(name: str, env: Mapping[str, str], *, default: float) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_canonical_env_name(name)} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{_canonical_env_name(name)} must be greater than zero")
    return value


def _bool_env(name: str, env: Mapping[str, str], *, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{_canonical_env_name(name)} must be a boolean")


def _list_env(name: str, env: Mapping[str, str]) -> list[str]:
    raw = env.get(name, "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{_canonical_env_name(name)} must be valid JSON") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise RuntimeError(f"{_canonical_env_name(name)} must be a JSON array of strings")
        return [item for item in parsed if item]
    return [item.strip() for item in raw.split(",") if item.strip()]
