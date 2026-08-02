from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from jwt import InvalidTokenError
from pydantic import BaseModel, Field

from app.models import Principal

SESSION_CREDENTIALS_ENV = "MINIGENT_SESSION_CREDENTIALS"
SESSION_SECRET_ENV = "MINIGENT_SESSION_SECRET"
SESSION_TTL_SECONDS_ENV = "MINIGENT_SESSION_TTL_SECONDS"
SESSION_COOKIE_SECURE_ENV = "MINIGENT_SESSION_COOKIE_SECURE"
SESSION_ALLOWED_ORIGINS_ENV = "MINIGENT_SESSION_ALLOWED_ORIGINS"
SESSION_COOKIE_NAME = "minigent_session"
SESSION_TOKEN_ISSUER = "minigent-console"

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

    @property
    def enabled(self) -> bool:
        return bool(self.credentials)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SessionAuthSettings:
        lookup = os.environ if env is None else env
        return cls(
            credentials=_load_credentials(lookup),
            secret=_optional_env(SESSION_SECRET_ENV, lookup),
            ttl_seconds=_positive_int_env(SESSION_TTL_SECONDS_ENV, lookup, default=28_800),
            cookie_secure=_bool_env(SESSION_COOKIE_SECURE_ENV, lookup, default=True),
            allowed_origins=tuple(_list_env(SESSION_ALLOWED_ORIGINS_ENV, lookup)),
        )


class SessionLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class SessionStatusResponse(BaseModel):
    enabled: bool
    authenticated: bool
    principal: Principal | None = None


def validate_session_auth_settings(
    env: Mapping[str, str] | None = None,
) -> SessionAuthSettings:
    settings = SessionAuthSettings.from_env(env)
    if settings.enabled:
        if settings.secret is None:
            raise RuntimeError(
                f"{SESSION_SECRET_ENV} is required when {SESSION_CREDENTIALS_ENV} is configured"
            )
        if len(settings.secret.encode()) < 32:
            raise RuntimeError(f"{SESSION_SECRET_ENV} must be at least 32 bytes")
    elif settings.secret is not None:
        raise RuntimeError(
            f"{SESSION_CREDENTIALS_ENV} is required when {SESSION_SECRET_ENV} is configured"
        )
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
        credential = settings.credentials.get(body.username)
        expected_hash = credential.password_hash if credential is not None else _DUMMY_PASSWORD_HASH
        password_valid = verify_password(body.password, expected_hash)
        if credential is None or not password_valid:
            raise HTTPException(status_code=401, detail="Invalid username or password")
        token = _encode_session(credential.principal, settings)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=settings.ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        return SessionStatusResponse(
            enabled=True,
            authenticated=True,
            principal=credential.principal,
        )

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

    return router


def principal_from_session_request(
    request: Request,
    *,
    settings: SessionAuthSettings | None = None,
    required: bool = True,
) -> Principal | None:
    settings = settings or validate_session_auth_settings()
    token = request.cookies.get(SESSION_COOKIE_NAME)
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
            issuer=SESSION_TOKEN_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub", "tenant_id", "is_admin"]},
        )
        principal = Principal(
            user_id=payload["sub"],
            tenant_id=payload["tenant_id"],
            is_admin=payload["is_admin"],
        )
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


def _encode_session(principal: Principal, settings: SessionAuthSettings) -> str:
    if settings.secret is None:  # pragma: no cover - validated by caller
        raise RuntimeError(f"{SESSION_SECRET_ENV} is required")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": SESSION_TOKEN_ISSUER,
            "sub": principal.user_id,
            "tenant_id": principal.tenant_id,
            "is_admin": principal.is_admin,
            "iat": now,
            "exp": now + settings.ttl_seconds,
        },
        settings.secret,
        algorithm="HS256",
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
        raise RuntimeError(f"{SESSION_CREDENTIALS_ENV} must be valid JSON") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError(f"{SESSION_CREDENTIALS_ENV} must be a non-empty JSON object")
    credentials: dict[str, SessionCredential] = {}
    for username, value in parsed.items():
        if not isinstance(username, str) or not username.strip():
            raise RuntimeError(f"{SESSION_CREDENTIALS_ENV} usernames must be non-empty strings")
        if not isinstance(value, dict):
            raise RuntimeError(f"{SESSION_CREDENTIALS_ENV} credential values must be objects")
        password_hash = value.get("password_hash")
        principal_value: Any = value.get("principal")
        if not isinstance(password_hash, str) or not _valid_password_hash(password_hash):
            raise RuntimeError(
                f"{SESSION_CREDENTIALS_ENV} credential '{username}' has an invalid password_hash"
            )
        try:
            principal = Principal.model_validate(principal_value)
        except ValueError as exc:
            raise RuntimeError(
                f"{SESSION_CREDENTIALS_ENV} credential '{username}' has an invalid principal"
            ) from exc
        credentials[username] = SessionCredential(password_hash=password_hash, principal=principal)
    return credentials


def _valid_password_hash(value: str) -> bool:
    return _parse_password_hash(value) is not None


def _optional_env(name: str, env: Mapping[str, str]) -> str | None:
    value = env.get(name)
    return value.strip() if value and value.strip() else None


def _positive_int_env(name: str, env: Mapping[str, str], *, default: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
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
    raise RuntimeError(f"{name} must be a boolean")


def _list_env(name: str, env: Mapping[str, str]) -> list[str]:
    raw = env.get(name, "").strip()
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
