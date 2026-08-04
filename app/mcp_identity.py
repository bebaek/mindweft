from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Mapping

import jwt

MCP_IDENTITY_ISSUER_ENV = "MINIGENT_MCP_IDENTITY_ISSUER"
MCP_IDENTITY_PRIVATE_KEY_ENV = "MINIGENT_MCP_IDENTITY_PRIVATE_KEY"
MCP_IDENTITY_KEY_ID_ENV = "MINIGENT_MCP_IDENTITY_KEY_ID"
MCP_IDENTITY_TOKEN_LIFETIME_ENV = "MINIGENT_MCP_IDENTITY_TOKEN_LIFETIME_SECONDS"


@dataclass(frozen=True, repr=False)
class MCPIdentityTokenIssuer:
    issuer: str
    audience: str
    private_key: str
    key_id: str
    lifetime_seconds: int = 300
    algorithm: str = "RS256"

    @classmethod
    def from_env(
        cls,
        *,
        audience: str,
        env: Mapping[str, str] | None = None,
    ) -> MCPIdentityTokenIssuer:
        lookup = os.environ if env is None else env
        issuer = lookup.get(MCP_IDENTITY_ISSUER_ENV, "").strip()
        private_key = lookup.get(MCP_IDENTITY_PRIVATE_KEY_ENV, "")
        key_id = lookup.get(MCP_IDENTITY_KEY_ID_ENV, "").strip()
        if not issuer or not private_key or not key_id:
            raise RuntimeError(
                "Forwarded MCP identity requires issuer, private key, and key ID settings"
            )
        try:
            lifetime = int(lookup.get(MCP_IDENTITY_TOKEN_LIFETIME_ENV, "300"))
        except ValueError as exc:
            raise RuntimeError("MCP identity token lifetime must be an integer") from exc
        if not 30 <= lifetime <= 300:
            raise RuntimeError("MCP identity token lifetime must be between 30 and 300 seconds")
        return cls(
            issuer=issuer,
            audience=audience,
            private_key=private_key,
            key_id=key_id,
            lifetime_seconds=lifetime,
        )

    def issue(
        self,
        *,
        tenant_id: str,
        user_id: str,
        scopes: tuple[str, ...],
    ) -> str:
        if not tenant_id or not user_id:
            raise ValueError("Tenant and user identity are required for forwarded MCP identity")
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.issuer,
                "aud": self.audience,
                "sub": user_id,
                "tenant_id": tenant_id,
                "scope": " ".join(scopes),
                "iat": now,
                "exp": now + self.lifetime_seconds,
                "jti": secrets.token_urlsafe(18),
            },
            self.private_key,
            algorithm=self.algorithm,
            headers={"kid": self.key_id},
        )
