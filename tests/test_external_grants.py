from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.admin_store import SQLiteTenantConfigStore
from app.external_grants import (
    EXTERNAL_GRANT_PROVIDERS_ENV,
    ExternalGrant,
    ExternalGrantAudit,
    ExternalGrantAuditState,
    ExternalGrantProviderRegistry,
    ExternalGrantResource,
    HTTPExternalGrantProvider,
    build_external_grant_provider_registry_from_env,
)
from app.main import create_app
from app.mcp_identity import MCPIdentityTokenIssuer

ADMIN_HEADERS = {
    "X-Minigent-User-Id": "admin-user",
    "X-Minigent-Tenant-Id": "platform",
    "X-Minigent-Admin": "true",
}


def test_external_grant_provider_settings_are_optional_and_validated() -> None:
    assert build_external_grant_provider_registry_from_env({}).list() == []
    env = {
        "MINIGENT_MCP_IDENTITY_ISSUER": "minigent",
        "MINIGENT_MCP_IDENTITY_PRIVATE_KEY": "test-private-key",
        "MINIGENT_MCP_IDENTITY_KEY_ID": "test-key",
        EXTERNAL_GRANT_PROVIDERS_ENV: json.dumps(
            [
                {
                    "id": "example-grants",
                    "title": "Example grants",
                    "description": "Example external grants.",
                    "base_url": "http://127.0.0.1:8769",
                    "audience": "example",
                    "read_scopes": ["grants:read"],
                    "write_scopes": ["grants:write"],
                    "allowed_permissions": ["read", "read_write"],
                    "resources_path": "/v1/resources",
                    "audit_path": "/v1/resource-grant-audit",
                }
            ]
        ),
    }

    registry = build_external_grant_provider_registry_from_env(env)

    provider = registry.get("example-grants")
    assert provider is not None
    assert provider.base_url == "http://127.0.0.1:8769"
    assert provider.allowed_permissions == ("read", "read_write")
    assert provider.resources_path == "/v1/resources"
    assert provider.audit_path == "/v1/resource-grant-audit"
    assert "test-private-key" not in repr(provider)


@pytest.mark.parametrize(
    "entry,match",
    [
        ({"id": "UPPER"}, "invalid id"),
        ({"id": "valid", "base_url": "file:///tmp/grants"}, "invalid base_url"),
        (
            {"id": "valid", "base_url": "https://user:pass@example.test"},
            "must not contain credentials",
        ),
    ],
)
def test_external_grant_provider_settings_reject_unsafe_entries(
    entry: dict[str, object], match: str
) -> None:
    payload = {
        "title": "Provider",
        "base_url": "https://example.test",
        "audience": "grants",
        "read_scopes": ["grants:read"],
        "write_scopes": ["grants:write"],
        **entry,
    }
    env = {
        "MINIGENT_MCP_IDENTITY_ISSUER": "minigent",
        "MINIGENT_MCP_IDENTITY_PRIVATE_KEY": "test-private-key",
        "MINIGENT_MCP_IDENTITY_KEY_ID": "test-key",
        EXTERNAL_GRANT_PROVIDERS_ENV: json.dumps([payload]),
    }
    with pytest.raises(RuntimeError, match=match):
        build_external_grant_provider_registry_from_env(env)


def test_http_external_grant_provider_uses_short_lived_scoped_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    seen: list[tuple[str, str, dict[str, object]]] = []

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 10.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            _ = exc_type, exc, traceback

        async def request(self, method, url, *, headers, json=None):
            token = headers["Authorization"].removeprefix("Bearer ")
            claims = jwt.decode(token, options={"verify_signature": False})
            seen.append((method, url, claims))
            grant = {
                "resource_id": "resource:one",
                "user_id": "user-1",
                "permission": "read_write",
                "enabled": True,
            }
            if method == "GET" and url.endswith("/resources"):
                return httpx.Response(
                    200,
                    json={
                        "resources": [
                            {
                                "resource_id": "resource:one",
                                "kind": "caldav",
                                "label": "Personal calendar",
                                "allowed_permissions": ["read", "read_write"],
                                "configured": True,
                                "enabled": True,
                            }
                        ]
                    },
                )
            if method == "GET" and url.endswith("/audit?limit=25&before_id=42"):
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {
                                "audit_id": 41,
                                "resource_id": "resource:one",
                                "user_id": "user-1",
                                "actor_id": "admin-user",
                                "operation": "resource_grant.disable",
                                "previous": {"permission": "read_write", "enabled": True},
                                "resulting": {"permission": "read_write", "enabled": False},
                                "created_at": "2026-01-01T00:00:00Z",
                            }
                        ],
                        "next_cursor": 41,
                    },
                )
            if method == "GET":
                return httpx.Response(200, json={"grants": [grant]})
            if method == "PUT":
                assert json == grant
                return httpx.Response(200, json=grant)
            return httpx.Response(204)

    monkeypatch.setattr("app.external_grants.httpx.AsyncClient", FakeClient)
    provider = HTTPExternalGrantProvider(
        provider_id="example",
        title="Example",
        description="",
        base_url="https://grants.example.test",
        list_path="/grants",
        upsert_path="/grants",
        delete_path="/grants/{resource_id}",
        allowed_permissions=("read", "read_write"),
        read_scopes=("grants:read",),
        write_scopes=("grants:write",),
        token_issuer=MCPIdentityTokenIssuer(
            issuer="minigent",
            audience="external-grants",
            private_key=private_pem,
            key_id="test-key",
        ),
        resources_path="/resources",
        audit_path="/audit",
    )

    asyncio.run(provider.list_grants(tenant_id="tenant-1", actor_user_id="admin-user"))
    asyncio.run(
        provider.upsert_grant(
            tenant_id="tenant-1",
            actor_user_id="admin-user",
            resource_id="resource:one",
            subject_id="user-1",
            permission="read_write",
            enabled=True,
        )
    )
    asyncio.run(
        provider.delete_grant(
            tenant_id="tenant-1",
            actor_user_id="admin-user",
            resource_id="resource:one",
            subject_id="user-1",
        )
    )

    resources = asyncio.run(
        provider.list_resources(tenant_id="tenant-1", actor_user_id="admin-user")
    )
    assert resources == [
        ExternalGrantResource(
            resource_id="resource:one",
            kind="caldav",
            label="Personal calendar",
            allowed_permissions=("read", "read_write"),
            configured=True,
            enabled=True,
        )
    ]
    audit_entries, next_cursor = asyncio.run(
        provider.list_audit(
            tenant_id="tenant-1",
            actor_user_id="admin-user",
            limit=25,
            before_id=42,
        )
    )
    assert audit_entries[0].operation == "resource_grant.disable"
    assert audit_entries[0].resulting == ExternalGrantAuditState(
        permission="read_write", enabled=False
    )
    assert next_cursor == 41

    assert [item[0] for item in seen] == ["GET", "PUT", "DELETE", "GET", "GET"]
    assert seen[0][2]["scope"] == "grants:read"
    assert seen[1][2]["scope"] == "grants:write"
    assert seen[2][2]["scope"] == "grants:write"
    assert all(item[2]["aud"] == "external-grants" for item in seen)
    assert all(item[2]["tenant_id"] == "tenant-1" for item in seen)
    assert seen[2][1].endswith("/grants/resource%3Aone?user_id=user-1")


def test_admin_external_grant_api_is_optional_audited_and_provider_neutral(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    grants = [
        ExternalGrant(
            resource_id="resource:one",
            subject_id="user-1",
            permission="read",
            enabled=True,
            updated_by="seed",
        )
    ]

    async def list_grants(self, *, tenant_id: str, actor_user_id: str):
        assert tenant_id == "tenant-1"
        assert actor_user_id == "admin-user"
        return list(grants)

    async def upsert_grant(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        resource_id: str,
        subject_id: str,
        permission: str,
        enabled: bool,
    ):
        _ = self
        grant = ExternalGrant(
            resource_id=resource_id,
            subject_id=subject_id,
            permission=permission,
            enabled=enabled,
            updated_by=actor_user_id,
        )
        grants[:] = [
            item
            for item in grants
            if (item.resource_id, item.subject_id) != (resource_id, subject_id)
        ] + [grant]
        return grant

    async def delete_grant(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        resource_id: str,
        subject_id: str,
    ):
        _ = self, tenant_id, actor_user_id
        grants[:] = [
            item
            for item in grants
            if (item.resource_id, item.subject_id) != (resource_id, subject_id)
        ]

    async def list_resources(self, *, tenant_id: str, actor_user_id: str):
        _ = self
        assert tenant_id == "tenant-1"
        assert actor_user_id == "admin-user"
        return [
            ExternalGrantResource(
                resource_id="resource:one",
                kind="caldav",
                label="Personal calendar",
                allowed_permissions=("read", "read_write"),
                configured=True,
                enabled=True,
            )
        ]

    async def list_audit(
        self,
        *,
        tenant_id: str,
        actor_user_id: str,
        limit: int,
        before_id: int | None = None,
    ):
        _ = self
        assert tenant_id == "tenant-1"
        assert actor_user_id == "admin-user"
        assert limit == 100
        assert before_id is None
        return (
            [
                ExternalGrantAudit(
                    audit_id=7,
                    resource_id="resource:one",
                    subject_id="user-1",
                    actor_id="admin-user",
                    operation="resource_grant.permission_change",
                    previous=ExternalGrantAuditState(permission="read", enabled=True),
                    resulting=ExternalGrantAuditState(permission="read_write", enabled=True),
                    created_at="2026-01-01T00:00:00Z",
                )
            ],
            None,
        )

    monkeypatch.setattr(HTTPExternalGrantProvider, "list_resources", list_resources)
    monkeypatch.setattr(HTTPExternalGrantProvider, "list_audit", list_audit)
    monkeypatch.setattr(HTTPExternalGrantProvider, "list_grants", list_grants)
    monkeypatch.setattr(HTTPExternalGrantProvider, "upsert_grant", upsert_grant)
    monkeypatch.setattr(HTTPExternalGrantProvider, "delete_grant", delete_grant)
    provider = HTTPExternalGrantProvider(
        provider_id="example",
        title="Example",
        description="Example grants",
        base_url="https://example.test",
        list_path="/grants",
        upsert_path="/grants",
        delete_path="/grants/{resource_id}",
        allowed_permissions=("read", "read_write"),
        read_scopes=("grants:read",),
        write_scopes=("grants:write",),
        token_issuer=MCPIdentityTokenIssuer(
            issuer="minigent",
            audience="example",
            private_key="unused",
            key_id="test",
        ),
        resources_path="/resources",
        audit_path="/audit",
    )
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    app = create_app(admin_store=store, tenant_config_source="store-with-defaults")
    app.state.external_grant_provider_registry = ExternalGrantProviderRegistry([provider])
    client = TestClient(app)
    assert (
        client.post(
            "/admin/tenants",
            headers=ADMIN_HEADERS,
            json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant One"},
        ).status_code
        == 201
    )

    assert (
        client.post(
            "/admin/tenants/tenant-1/users",
            headers=ADMIN_HEADERS,
            json={
                "user_id": "user-1",
                "email": "user-1@example.test",
                "display_name": "User One",
                "role": "member",
                "status": "suspended",
            },
        ).status_code
        == 201
    )

    providers = client.get("/admin/external-grant-providers", headers=ADMIN_HEADERS)
    assert providers.status_code == 200
    assert providers.json()["providers"][0]["id"] == "example"
    assert providers.json()["providers"][0]["resource_discovery_available"] is True
    assert providers.json()["providers"][0]["audit_available"] is True
    listed = client.get("/admin/tenants/tenant-1/external-grants/example", headers=ADMIN_HEADERS)
    assert listed.status_code == 200
    assert listed.json()["grants"][0]["resource_id"] == "resource:one"
    provider_resources = client.get(
        "/admin/tenants/tenant-1/external-grants/example/resources",
        headers=ADMIN_HEADERS,
    )
    assert provider_resources.status_code == 200
    assert provider_resources.json()["resources"] == [
        {
            "resource_id": "resource:one",
            "kind": "caldav",
            "label": "Personal calendar",
            "allowed_permissions": ["read", "read_write"],
            "configured": True,
            "enabled": True,
        }
    ]
    provider_audit = client.get(
        "/admin/tenants/tenant-1/external-grants/example/audit",
        headers=ADMIN_HEADERS,
    )
    assert provider_audit.status_code == 200
    assert provider_audit.json()["entries"][0]["operation"] == ("resource_grant.permission_change")
    assert provider_audit.json()["entries"][0]["previous"] == {
        "permission": "read",
        "enabled": True,
    }

    updated = client.put(
        "/admin/tenants/tenant-1/external-grants/example",
        headers=ADMIN_HEADERS,
        json={
            "resource_id": "resource:one",
            "subject_id": "user-1",
            "permission": "read_write",
            "enabled": False,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["permission"] == "read_write"
    assert updated.json()["enabled"] is False

    inactive_enable = client.put(
        "/admin/tenants/tenant-1/external-grants/example",
        headers=ADMIN_HEADERS,
        json={
            "resource_id": "resource:one",
            "subject_id": "user-1",
            "permission": "read_write",
            "enabled": True,
        },
    )
    assert inactive_enable.status_code == 409
    assert "must be active" in inactive_enable.json()["detail"]

    missing_subject = client.put(
        "/admin/tenants/tenant-1/external-grants/example",
        headers=ADMIN_HEADERS,
        json={
            "resource_id": "resource:two",
            "subject_id": "missing-user",
            "permission": "read",
            "enabled": True,
        },
    )
    assert missing_subject.status_code == 404
    assert "not found" in missing_subject.json()["detail"]

    deleted = client.delete(
        "/admin/tenants/tenant-1/external-grants/example/resource%3Aone?subject_id=user-1",
        headers=ADMIN_HEADERS,
    )
    assert deleted.status_code == 204
    assert grants == []
    audit = client.get("/admin/tenants/tenant-1/audit-records", headers=ADMIN_HEADERS)
    actions = {record["action"] for record in audit.json()["audit_records"]}
    assert {"external_grant.upsert", "external_grant.delete"} <= actions

    app.state.external_grant_provider_registry = ExternalGrantProviderRegistry()
    missing = client.get("/admin/external-grant-providers", headers=ADMIN_HEADERS)
    assert missing.status_code == 200
    assert missing.json() == {"providers": []}
