"""Credential-only, single-process authentication for the loopback launcher.

Ordinary deployments do not install this middleware. Gateway credentials are
separate from API credentials; gateway access never accepts browser cookies.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from app.models import Principal
from mindweft_workspace.local_credentials import read_credential

SESSION_SECONDS = 8 * 60 * 60
TICKET_SECONDS = 30
MAX_ENTRIES = 128


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def install_local_auth(app: FastAPI, *, gateway: bool = False) -> None:
    role = "GATEWAY" if gateway else "API"
    directory = os.environ.get(f"MINDWEFT_LOCAL_{role}_CREDENTIAL_DIR")
    if not directory:
        return
    launch_id = os.environ["MINDWEFT_LOCAL_LAUNCH_ID"]
    verifier = read_credential(Path(directory), launch_id=launch_id).verifier()
    origin = os.environ[f"MINDWEFT_LOCAL_{role}_ORIGIN"]
    # Only the launcher-provided literal loopback origin is accepted.
    from urllib.parse import urlsplit

    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise RuntimeError("Local authentication requires a literal loopback origin")
    cookie_name = "mindweft_local_" + launch_id
    principal = Principal(tenant_id="demo-tenant", user_id="demo-user", is_admin=False)
    tickets: dict[bytes, float] = {}
    sessions: dict[bytes, tuple[float, bytes]] = {}

    def response(body, status=200):
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    def denied(status=401):
        return response(
            {"detail": "Local authentication required; reopen with mindweft instances open."},
            status,
        )

    @app.middleware("http")
    async def local_auth(request: Request, call_next):
        if request.headers.get("host") != parsed.netloc:
            return denied(403)
        request_origin = request.headers.get("origin")
        if request_origin is not None and request_origin != origin:
            return denied(403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return denied(403)
        expected = request.headers.get("x-mindweft-launch-id")
        if expected is not None and expected != launch_id:
            return response(
                {"detail": "Local instance restarted; reopen with mindweft instances open."}, 409
            )
        now = time.monotonic()
        for key, expiry in list(tickets.items()):
            if expiry <= now:
                del tickets[key]
        for key, (expiry, _) in list(sessions.items()):
            if expiry <= now:
                del sessions[key]
        authorization = request.headers.get("authorization", "")
        bearer = authorization.startswith("Bearer ") and verifier.accepts(
            authorization[7:], launch_id=launch_id
        )
        cookie = request.cookies.get(cookie_name, "")
        session = sessions.get(_digest(cookie)) if cookie else None
        # Cookies are shared across ports. A JS-held, origin-scoped proof prevents
        # a cookie stolen by a different loopback origin from being sufficient.
        proof = request.headers.get("x-mindweft-session-key", "")
        browser = bool(
            not gateway
            and session
            and proof
            and secrets.compare_digest(session[1], _digest(proof))
            and expected == launch_id
        )
        path = request.url.path
        if not gateway and path == "/local-auth/ticket" and request.method == "POST":
            if not bearer:
                return denied()
            if len(tickets) >= MAX_ENTRIES:
                return denied(429)
            ticket = secrets.token_urlsafe(32)
            tickets[_digest(ticket)] = now + TICKET_SECONDS
            return response({"ticket": ticket})
        if not gateway and path == "/local-auth/exchange" and request.method == "POST":
            if request_origin != origin or expected != launch_id:
                return denied(403)
            # A bounded header avoids parsing an untrusted/unbounded request body.
            ticket = request.headers.get("x-mindweft-browser-ticket", "")
            expiry = tickets.pop(_digest(ticket), None)
            if expiry is None or expiry <= now:
                return denied()
            if len(sessions) >= MAX_ENTRIES:
                return denied(429)
            token, key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            sessions[_digest(token)] = (now + SESSION_SECONDS, _digest(key))
            result = response({"session_key": key})
            result.set_cookie(
                cookie_name,
                token,
                httponly=True,
                samesite="strict",
                max_age=SESSION_SECONDS,
                path="/",
            )
            return result
        if not gateway and path == "/auth/session":
            if request.method == "GET":
                return response(
                    {
                        "enabled": True,
                        "authenticated": browser,
                        "principal": principal.model_dump() if browser else None,
                    }
                )
            if request.method == "DELETE" and browser and request_origin == origin:
                sessions.pop(_digest(cookie), None)
                result = response({"enabled": True, "authenticated": False, "principal": None})
                result.delete_cookie(cookie_name, path="/")
                return result
            return denied()
        public = request.method in {"GET", "HEAD"} and (
            path in {"/health", "/health/live"}
            or (not gateway and (path == "/local-instance" or path.startswith("/console/")))
        )
        if not public:
            if not bearer and not browser:
                return denied()
            if (
                browser
                and not bearer
                and request.method not in {"GET", "HEAD"}
                and request_origin != origin
            ):
                return denied(403)
            request.state.local_principal = principal
        result = await call_next(request)
        if not gateway and path.startswith("/console/"):
            result.headers["Referrer-Policy"] = "no-referrer"
        return result
