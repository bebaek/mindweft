"""Deployment-defined OAuth connections with tenant-isolated account credentials.

Connection definitions are trusted deployment configuration (not user-selected
HTTP endpoints). No named connection falls back to legacy/global credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, replace
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.responses import RedirectResponse

from app.models import Principal
from app.oauth import (
    GenericOAuthConfig,
    GenericOAuthProvider,
    SQLiteEncryptedOAuthStore,
    build_oauth_credential_store_from_env,
)
from app.tenants import require_active_tenant_principal
from mindweft_config.unified_config import preferred_mindweft_env

CALLBACK_PATH = "/oauth/connections/callback"


def connection_name(ref: str) -> str:
    name = ref.removeprefix("shared:")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("OAuth connection must be a named shared connection")
    return name


def configured_connections() -> dict[str, GenericOAuthConfig]:
    raw = preferred_mindweft_env("OAUTH_CONNECTIONS") or "{}"
    try:
        entries = json.loads(raw)
        if not isinstance(entries, dict):
            raise ValueError
        result = {}
        for name, entry in entries.items():
            if connection_name(name) != name or not isinstance(entry, dict):
                raise ValueError
            # Accept the existing snake_case/camelCase conventions.
            normalized = {
                re.sub(r"([A-Z])", lambda m: "_" + m[1].lower(), k): v for k, v in entry.items()
            }
            normalized.setdefault("auth_params", {})
            config = GenericOAuthConfig(**normalized)
            for value in (config.provider_id, config.client_id, config.scope):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError
            for value in (config.authorize_url, config.token_url):
                parsed = urlsplit(value)
                if (
                    parsed.scheme != "https"
                    or not parsed.netloc
                    or parsed.username
                    or parsed.fragment
                ):
                    raise ValueError
            redirect = urlsplit(config.redirect_uri)
            if (
                not redirect.netloc
                or redirect.path != CALLBACK_PATH
                or redirect.query
                or redirect.fragment
                or redirect.username
            ):
                raise ValueError
            if not (
                redirect.scheme == "https"
                or (redirect.scheme == "http" and redirect.hostname == "127.0.0.1")
            ):
                raise ValueError
            if not isinstance(config.auth_params, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in config.auth_params.items()
            ):
                raise ValueError
            reserved = {
                "state",
                "client_id",
                "redirect_uri",
                "response_type",
                "scope",
                "code_challenge",
                "code_challenge_method",
            }
            if reserved.intersection(config.auth_params):
                raise ValueError
            result[name] = config
        return result
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("Invalid named OAuth connection configuration") from None


def connection_config(ref: str) -> GenericOAuthConfig:
    name = connection_name(ref)
    config = configured_connections().get(name)
    if config is None:
        raise ValueError(f"Unknown OAuth connection '{name}'")
    return config


def connection_key(tenant_id: str, ref: str, config: GenericOAuthConfig) -> str:
    # Bind accounts to the complete provider definition; changing endpoints must
    # never send an old account's refresh token to the new endpoint.
    value = json.dumps([tenant_id, connection_name(ref), asdict(config)], sort_keys=True)
    return "connection:v1:" + hashlib.sha256(value.encode()).hexdigest()


def connection_store() -> SQLiteEncryptedOAuthStore:
    store = build_oauth_credential_store_from_env()
    if not isinstance(store, SQLiteEncryptedOAuthStore):
        raise RuntimeError("Named OAuth connections require encrypted SQLite OAuth storage")
    return store


def named_provider(tenant_id: str, ref: str) -> GenericOAuthProvider:
    config = connection_config(ref)
    return GenericOAuthProvider(
        config=config,
        store=connection_store(),
        credential_key=connection_key(tenant_id, ref, config),
    )


async def require_connection_manager(
    request: Request, principal: Annotated[Principal, Depends(require_active_tenant_principal)]
) -> Principal:
    context = request.state.tenant_context
    if not principal.is_admin and (
        (context.status is not None and context.status.value != "active")
        or context.user_role is None
        or context.user_role.value not in {"owner", "admin"}
    ):
        raise HTTPException(status_code=403, detail="Tenant manager access required")
    return principal


class CompleteConnection(BaseModel):
    state: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=4096)


def build_oauth_connections_router() -> APIRouter:
    router = APIRouter(prefix="/oauth/connections", tags=["oauth-connections"])

    def target(principal: Principal, name: str):
        try:
            config = connection_config(name)
            return config, connection_store(), connection_key(principal.tenant_id, name, config)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except RuntimeError:
            raise HTTPException(
                status_code=503,
                detail="Named OAuth configuration or encrypted storage is unavailable",
            ) from None

    def status(name, config, credentials):
        return {
            "name": name,
            "ref": "shared:" + name,
            "provider_id": config.provider_id,
            "connected": credentials is not None,
            "expired": credentials.expires_at <= time.time() if credentials else None,
            "expires_at": credentials.expires_at if credentials else None,
            "account_id": credentials.account_id if credentials else None,
            "verification": "stored-credentials-only",
        }

    @router.get("")
    async def list_connections(
        response: Response, principal: Annotated[Principal, Depends(require_connection_manager)]
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            definitions = configured_connections()
            store = connection_store() if definitions else None
        except RuntimeError:
            raise HTTPException(
                status_code=503,
                detail="Named OAuth configuration or encrypted storage is unavailable",
            ) from None
        return {
            "items": [
                status(
                    name,
                    config,
                    store.get(connection_key(principal.tenant_id, name, config)) if store else None,
                )
                for name, config in definitions.items()
            ]
        }

    @router.post("/complete")
    async def complete(
        body: CompleteConnection,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(require_connection_manager)],
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            store = connection_store()
        except RuntimeError:
            raise HTTPException(
                status_code=503, detail="Encrypted OAuth storage is unavailable"
            ) from None
        flow = store.pop(body.state)
        context = flow.context if flow else None
        if (
            flow is None
            or not context
            or context.get("tenant_id") != principal.tenant_id
            or context.get("user_id") != principal.user_id
        ):
            raise HTTPException(
                status_code=400,
                detail="OAuth login is invalid, expired, or belongs to another user",
            )
        name = context["name"]
        config, _, key = target(principal, name)
        if key != context.get("key") or config.redirect_uri != flow.redirect_uri:
            raise HTTPException(status_code=409, detail="OAuth connection changed; restart sign-in")
        provider = GenericOAuthProvider(config=config, store=store, credential_key=key)
        try:
            # Exchange without persisting until the epoch is checked atomically.
            credentials = await provider.exchange_login(code=body.code, flow=flow)
        except Exception:
            raise HTTPException(
                status_code=502, detail="OAuth token exchange failed; restart sign-in"
            ) from None
        # A suspension during token exchange must not authorize a credential write.
        await require_active_tenant_principal(request, principal)
        await require_connection_manager(request, principal)
        if not store.set_for_connection_epoch(key, context["epoch"], credentials):
            raise HTTPException(
                status_code=409,
                detail="OAuth connection was disconnected or a newer sign-in started",
            )
        return status(name, config, credentials)

    @router.get("/callback")
    async def callback(state: str = "", code: str = "", error: str = ""):
        # Public transport only: the authenticated completion endpoint owns exchange.
        if error or not state or not code or len(state) > 256 or len(code) > 4096:
            fragment = urlencode(
                {"oauth_error": "Sign-in failed or was cancelled. Restart it in tenant settings."}
            )
        else:
            fragment = urlencode({"oauth_state": state, "oauth_code": code})
        return RedirectResponse(
            "/console/#" + fragment,
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @router.post("/{name}/login")
    async def login(
        name: str,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(require_connection_manager)],
    ):
        config, store, key = target(principal, name)
        expected = urlsplit(config.redirect_uri)
        if expected.netloc != request.headers.get("host") or expected.scheme != request.url.scheme:
            raise HTTPException(
                status_code=409,
                detail="OAuth callback origin does not match this server; configure its registered redirect URI",
            )
        provider = GenericOAuthProvider(config=config, store=store, credential_key=key)
        start, flow = provider.start_login()
        epoch = store.advance_connection_epoch(key)
        flow = replace(
            flow,
            context={
                "tenant_id": principal.tenant_id,
                "user_id": principal.user_id,
                "name": connection_name(name),
                "key": key,
                "epoch": epoch,
            },
        )
        store.put(start.state, flow)
        response.headers["Cache-Control"] = "no-store"
        return {"authorization_url": start.authorization_url}

    @router.post("/{name}/import/pi")
    async def import_pi(
        name: str,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(require_connection_manager)],
    ):
        from app.admin_api import _parse_pi_openai_oauth_credential

        config, store, key = target(principal, name)
        if config.provider_id != "openai-codex":
            raise HTTPException(
                status_code=400, detail="Pi import is only supported for openai-codex connections"
            )
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 256 * 1024:
                raise HTTPException(status_code=413, detail="Credential import exceeds 256 KiB")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid credential JSON") from None
        credentials = _parse_pi_openai_oauth_credential(payload)
        epoch = store.advance_connection_epoch(key)
        if not store.set_for_connection_epoch(key, epoch, credentials):
            raise HTTPException(status_code=409, detail="OAuth connection changed; retry import")
        response.headers["Cache-Control"] = "no-store"
        return status(connection_name(name), config, credentials)

    @router.delete("/{name}", status_code=204)
    async def disconnect(
        name: str,
        response: Response,
        principal: Annotated[Principal, Depends(require_connection_manager)],
    ):
        _, store, key = target(principal, name)
        store.advance_connection_epoch(key, disconnect=True)
        response.headers["Cache-Control"] = "no-store"

    return router
