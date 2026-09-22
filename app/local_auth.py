"""Loopback transport guards and the separate MCP gateway service credential.

API identity is resolved exclusively by normal bearer/session authentication.
This middleware never manufactures a user principal or intercepts auth routes.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import JSONResponse

from mindweft_workspace.local_credentials import read_credential


def install_local_auth(app: FastAPI, *, gateway: bool = False) -> None:
    role = "GATEWAY" if gateway else "API"
    directory = os.environ.get(f"MINDWEFT_LOCAL_{role}_CREDENTIAL_DIR")
    if not directory:
        return
    launch_id = os.environ["MINDWEFT_LOCAL_LAUNCH_ID"]
    origin = os.environ[f"MINDWEFT_LOCAL_{role}_ORIGIN"]
    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise RuntimeError("Local authentication requires a literal loopback origin")
    verifier = read_credential(Path(directory), launch_id=launch_id).verifier() if gateway else None
    if not gateway:
        from app.auth import AUTH_MODE_STATIC_TOKENS, validate_auth_settings

        if validate_auth_settings().mode != AUTH_MODE_STATIC_TOKENS:
            raise RuntimeError("Coding instances require provisioned static-token authentication")

    def denied(status=401, detail="Authentication required"):
        return JSONResponse(
            {"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    @app.middleware("http")
    async def guard_local_transport(request: Request, call_next):
        if request.headers.get("host") != parsed.netloc:
            return denied(403)
        request_origin = request.headers.get("origin")
        if request_origin is not None and request_origin != origin:
            return denied(403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return denied(403)
        expected = request.headers.get("x-mindweft-launch-id")
        if expected is not None and expected != launch_id:
            return denied(409, "Local instance restarted; reopen with mindweft instances open.")
        path = request.url.path
        public = request.method in {"GET", "HEAD"} and (
            path in {"/health", "/health/live"}
            or (not gateway and (path == "/local-instance" or path.startswith("/console/")))
        )
        # These endpoints perform their own shared authentication/CSRF checks.
        authentication = not gateway and path in {
            "/auth/session",
            "/auth/session/ticket",
            "/auth/session/exchange",
        }
        if not public and not authentication:
            authorization = request.headers.get("authorization")
            if gateway:
                if not (
                    authorization
                    and authorization.startswith("Bearer ")
                    and verifier
                    and verifier.accepts(authorization[7:], launch_id=launch_id)
                ):
                    return denied()
            else:
                from app.auth import require_principal

                try:
                    await require_principal(request, authorization=authorization)
                except HTTPException as exc:
                    return denied(exc.status_code, exc.detail)
        result = await call_next(request)
        if not gateway and path.startswith("/console/"):
            result.headers["Referrer-Policy"] = "no-referrer"
        return result
