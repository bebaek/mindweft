"""Opt-in, single-process bearer-to-browser handoff for the shared session API.

Tickets and session proofs are kept only as digests. Sessions use the normal
signed cookie, credential/lifecycle validation, status and logout endpoints.
Restarting the server revokes all handoffs. Multi-worker deployments must not
use this in-memory implementation.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response

from app.models import Principal

TICKET_SECONDS = 30
MAX_ENTRIES = 128


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class HandoffState:
    origin: str
    binding: str
    tickets: dict[str, tuple[float, str, Principal]] = field(default_factory=dict)
    sessions: dict[str, tuple[float, str]] = field(default_factory=dict)

    def prune(self) -> None:
        now = time.monotonic()
        for entries in (self.tickets, self.sessions):
            for key, entry in list(entries.items()):
                if entry[0] <= now:
                    del entries[key]

    def check_origin(self, request: Request, *, required: bool = True) -> None:
        origin = request.headers.get("origin")
        if (
            request.headers.get("host") != urlsplit(self.origin).netloc
            or (origin != self.origin if required else origin not in {None, self.origin})
            or request.headers.get("sec-fetch-site") == "cross-site"
            or request.headers.get("x-mindweft-launch-id") != self.binding
        ):
            raise HTTPException(
                status_code=403, detail="Session handoff origin or binding mismatch"
            )


def credential_principal(request: Request, credential_hash: str) -> Principal | None:
    from app.auth import (
        AUTH_MODE_STATIC_TOKENS,
        require_registered_principal,
        validate_auth_settings,
    )

    settings = validate_auth_settings()
    if settings.mode != AUTH_MODE_STATIC_TOKENS:
        return None
    principal = settings.static_token_hashes.get(credential_hash)
    if principal is None:
        principal = next(
            (p for token, p in settings.static_tokens.items() if digest(token) == credential_hash),
            None,
        )
    if principal is None:
        return None
    return require_registered_principal(request, principal)


def validate_handoff_session(request: Request, token: str) -> bool:
    state = getattr(request.app.state, "session_handoff", None)
    if state is None:
        return False
    state.prune()
    entry = state.sessions.get(digest(token))
    proof = request.headers.get("x-mindweft-session-key", "")
    if entry is None or not proof or not secrets.compare_digest(entry[1], digest(proof)):
        return False
    state.check_origin(request, required=request.method not in {"GET", "HEAD", "OPTIONS"})
    return True


def build_handoff_router() -> APIRouter:
    # Imports are deferred to avoid a cycle with the shared session/auth modules.
    import os

    from app.auth import _extract_bearer_token
    from app.session_auth import (
        SessionAuthSettings,
        _encode_session,
        _set_session_cookie,
        validate_session_auth_settings,
    )
    from mindweft_config.unified_config import normalize_mindweft_env

    router = APIRouter()
    env = normalize_mindweft_env(dict(os.environ))
    origin = env.get("MINIGENT_SESSION_HANDOFF_ORIGIN")
    if not origin:
        return router
    binding = env.get("MINIGENT_SESSION_HANDOFF_BINDING", "")
    parsed = urlsplit(origin)
    settings = validate_session_auth_settings()
    if (
        not binding
        or not settings.enabled
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or not (
            parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname == "127.0.0.1")
        )
    ):
        raise RuntimeError(
            "Session handoff requires sessions, a binding and an HTTPS or loopback origin"
        )
    state = HandoffState(origin, binding)

    def attach(request: Request) -> HandoffState:
        request.app.state.session_handoff = state
        state.prune()
        return state

    @router.post("/session/ticket")
    async def ticket(request: Request, response: Response) -> dict[str, str]:
        token = _extract_bearer_token(request.headers.get("authorization"))
        attach(request).check_origin(request, required=False)
        credential_hash = digest(token)
        principal = credential_principal(request, credential_hash)
        if principal is None:
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        if len(state.tickets) >= MAX_ENTRIES:
            raise HTTPException(status_code=429, detail="Too many outstanding sign-in tickets")
        value = secrets.token_urlsafe(32)
        state.tickets[digest(value)] = (
            time.monotonic() + TICKET_SECONDS,
            credential_hash,
            principal,
        )
        response.headers["Cache-Control"] = "no-store"
        return {"ticket": value}

    @router.post("/session/exchange")
    async def exchange(request: Request, response: Response) -> dict[str, str]:
        attach(request).check_origin(request)
        value = request.headers.get("x-mindweft-browser-ticket", "")
        entry = state.tickets.pop(digest(value), None)
        if entry is None:
            raise HTTPException(status_code=401, detail="Invalid or expired sign-in ticket")
        _, credential_hash, issued_principal = entry
        principal = credential_principal(request, credential_hash)
        if principal is None or principal != issued_principal:
            raise HTTPException(status_code=401, detail="Credential is no longer active")
        if len(state.sessions) >= MAX_ENTRIES:
            raise HTTPException(status_code=429, detail="Too many browser sessions")
        settings: SessionAuthSettings = validate_session_auth_settings()
        key = secrets.token_urlsafe(32)
        token = _encode_session(
            principal,
            settings,
            username=credential_hash,
            source="static-token",
            credential_version=0,
        )
        state.sessions[digest(token)] = (time.monotonic() + settings.ttl_seconds, digest(key))
        _set_session_cookie(response, token, settings)
        response.headers["Cache-Control"] = "no-store"
        return {"session_key": key}

    return router
