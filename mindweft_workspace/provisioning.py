"""Launcher-only provisioning of ordinary tenant membership and auth settings."""

from __future__ import annotations

import hashlib
import json
import secrets

from app.admin_store import SQLiteTenantConfigStore
from app.models import Principal, Tenant, TenantStatus, TenantUser, TenantUserRole, TenantUserStatus

# Retain historical IDs so existing threads and personal settings keep their owner.
DEFAULT_TENANT = "demo-tenant"
DEFAULT_USER = "demo-user"


def provision_coding_user(env: dict[str, str], *, token: str, origin: str, binding: str) -> None:
    store = SQLiteTenantConfigStore(env["MINDWEFT_ADMIN_DB_PATH"])
    if store.get_tenant(DEFAULT_TENANT) is None:
        store.create_tenant(
            Tenant(
                id=DEFAULT_TENANT,
                slug="coding",
                name="Coding workspace",
                status=TenantStatus.ACTIVE,
            )
        )
    if store.get_tenant_user_by_user_id(DEFAULT_TENANT, DEFAULT_USER) is None:
        store.create_tenant_user(
            TenantUser(
                id="coding-owner",
                tenant_id=DEFAULT_TENANT,
                user_id=DEFAULT_USER,
                display_name="Coding user",
                role=TenantUserRole.OWNER,
                status=TenantUserStatus.ACTIVE,
            )
        )
    # Existing status and roles are never reset: suspension survives a restart.
    principal = Principal(tenant_id=DEFAULT_TENANT, user_id=DEFAULT_USER, is_admin=False)
    # Do not inherit an unrelated login or bearer credential into this instance.
    for key in list(env):
        if key.startswith(
            ("MINDWEFT_AUTH_", "MINIGENT_AUTH_", "MINDWEFT_SESSION_", "MINIGENT_SESSION_")
        ):
            del env[key]
    env.update(
        {
            "MINDWEFT_AUTH_MODE": "static-tokens",
            "MINDWEFT_AUTH_TOKEN_HASHES": json.dumps(
                {hashlib.sha256(token.encode()).hexdigest(): principal.model_dump()}
            ),
            "MINDWEFT_SESSION_SECRET": secrets.token_urlsafe(48),
            "MINDWEFT_SESSION_COOKIE_SECURE": "false",
            "MINDWEFT_SESSION_HANDOFF_ONLY": "true",
            "MINDWEFT_SESSION_COOKIE_NAME": "mindweft_session_" + binding,
            "MINDWEFT_SESSION_HANDOFF_ORIGIN": origin,
            "MINDWEFT_SESSION_HANDOFF_BINDING": binding,
            "MINDWEFT_TENANT_REGISTRY_REQUIRED": "true",
            "MINDWEFT_TENANT_USER_REGISTRY_REQUIRED": "true",
        }
    )
