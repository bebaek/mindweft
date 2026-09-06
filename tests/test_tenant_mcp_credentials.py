import json
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.admin_store import SQLiteTenantConfigStore
from app.main import create_app
from app.mcp import MCPHTTPClient

ADMIN = {
    "X-Minigent-User-Id": "admin",
    "X-Minigent-Tenant-Id": "platform",
    "X-Minigent-Admin": "true",
}
OWNER = {"X-Minigent-User-Id": "owner", "X-Minigent-Tenant-Id": "tenant-1"}
URL = "/admin/tenants/tenant-1/mcp-servers/netwise/credential"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    server = {
        "name": "netwise",
        "url": "https://netwise.example/mcp/",
        "headers": {"Authorization": "Bearer old-token"},
        "allowed_tools": ["summary"],
    }
    catalog = [
        {
            "id": "netwise",
            "title": "Netwise",
            "description": "Finance",
            "tenant_credential": "bearer",
            "server": server,
        }
    ]
    monkeypatch.setenv("MINDWEFT_ADMIN_MCP_SERVER_CATALOG", json.dumps(catalog))
    key = Fernet.generate_key().decode()
    store = SQLiteTenantConfigStore(str(tmp_path / "config.db"), encryption_key=key)
    store.upsert_raw_config(
        "tenant-1", {"llm": {"provider": "mock"}, "tools": {"mcp_servers": [server]}}
    )
    app = create_app(admin_store=store, tenant_config_source="store")
    client = TestClient(app)
    assert (
        client.post(
            "/admin/tenants",
            headers=ADMIN,
            json={"id": "tenant-1", "slug": "tenant-one", "name": "Tenant"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/admin/tenants/tenant-1/users",
            headers=ADMIN,
            json={"user_id": "owner", "role": "owner", "status": "active"},
        ).status_code
        == 201
    )
    assert (
        client.put(
            "/admin/tenants/tenant-1/mcp-server-catalog-policy",
            headers=ADMIN,
            json={"item_ids": ["netwise"], "allow_custom_mcp_servers": False},
        ).status_code
        == 200
    )
    discovery = AsyncMock(return_value=[])
    monkeypatch.setattr(MCPHTTPClient, "list_tools", discovery)
    return client, store, discovery, tmp_path, key


def rotate(client, **kwargs):
    return client.put(
        URL, headers=OWNER, json={"token": "new-token", "expected_version": 1, **kwargs}
    )


def test_owner_rotates_restricted_service_encrypted_and_redacted(setup):
    client, store, discovery, path, key = setup
    before = store.get_raw_config("tenant-1")
    result = rotate(client)
    assert result.status_code == 200, result.text
    assert "new-token" not in result.text
    assert result.json()["version"] == 2
    discovery.assert_awaited_once()
    before["tools"]["mcp_servers"][0]["headers"]["Authorization"] = "Bearer new-token"
    assert store.get_raw_config("tenant-1") == before
    reopened = SQLiteTenantConfigStore(str(path / "config.db"), encryption_key=key)
    assert reopened.get_raw_config("tenant-1") == before
    assert b"new-token" not in (path / "config.db").read_bytes()
    audit = client.get("/admin/audit", headers=ADMIN)
    assert "new-token" not in audit.text


def test_failed_discovery_keeps_old_token_and_suppresses_exception(setup):
    client, store, discovery, *_ = setup
    discovery.side_effect = RuntimeError("upstream echoed new-token")
    result = rotate(client)
    assert result.status_code == 400
    assert "new-token" not in result.text
    assert store.get_config_version("tenant-1") == 1
    assert (
        store.get_raw_config("tenant-1")["tools"]["mcp_servers"][0]["headers"]["Authorization"]
        == "Bearer old-token"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"token": "bad token"},
        {"expected_version": 99},
        {"url": "https://evil.example/new-token"},
        {"token": "new-token" * 1000},
    ],
)
def test_invalid_or_stale_input_never_discovers_or_echoes_token(setup, body):
    client, store, discovery, *_ = setup
    result = rotate(client, **body)
    assert result.status_code in {400, 409, 422}
    assert "new-token" not in result.text
    discovery.assert_not_awaited()
    assert store.get_config_version("tenant-1") == 1


def test_concurrent_writer_wins_without_lost_update(setup):
    client, store, discovery, *_ = setup

    async def concurrent_update():
        payload = store.get_raw_config("tenant-1")
        payload["llm"]["model"] = "concurrent-model"
        store.upsert_raw_config("tenant-1", payload)
        return []

    discovery.side_effect = concurrent_update
    assert rotate(client).status_code == 409
    payload = store.get_raw_config("tenant-1")
    assert payload["llm"]["model"] == "concurrent-model"
    assert payload["tools"]["mcp_servers"][0]["headers"]["Authorization"] == "Bearer old-token"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Minigent-User-Id": "member", "X-Minigent-Tenant-Id": "tenant-1"},
        {"X-Minigent-User-Id": "owner", "X-Minigent-Tenant-Id": "tenant-2"},
    ],
)
def test_non_owner_cannot_rotate(setup, headers):
    client, _, discovery, *_ = setup
    assert (
        client.put(
            URL, headers=headers, json={"token": "new-token", "expected_version": 1}
        ).status_code
        == 403
    )
    discovery.assert_not_awaited()


def test_unassigned_service_cannot_rotate(setup):
    client, _, discovery, *_ = setup
    client.put(
        "/admin/tenants/tenant-1/mcp-server-catalog-policy",
        headers=ADMIN,
        json={"item_ids": [], "allow_custom_mcp_servers": False},
    )
    assert rotate(client).status_code == 403
    discovery.assert_not_awaited()


@pytest.mark.parametrize("change", ["url", "forward_identity", "allowed_tools", "disabled"])
def test_rotation_does_not_bypass_service_policy(setup, change):
    client, store, discovery, *_ = setup
    payload = store.get_raw_config("tenant-1")
    server = payload["tools"]["mcp_servers"][0]
    if change == "url":
        server["url"] = "https://other.example/mcp"
    elif change == "forward_identity":
        server["forward_identity"] = True
    elif change == "allowed_tools":
        server["allowed_tools"] = ["unauthorized_tool"]
    else:
        payload["tools"]["mcp_servers"] = []
    store.upsert_raw_config("tenant-1", payload)
    assert rotate(client, expected_version=2).status_code in {400, 404}
    discovery.assert_not_awaited()


@pytest.mark.parametrize("alias", ["tenant_credential", "tenantCredential"])
def test_catalog_credential_aliases(alias):
    from app.admin_api import AdminMCPServerCatalogItem

    item = AdminMCPServerCatalogItem.model_validate(
        {
            "id": "netwise",
            "title": "Netwise",
            "description": "Finance",
            "server": {},
            alias: "bearer",
        }
    )
    assert item.tenant_credential == "bearer"


def test_nonsecret_catalog_opt_in_and_forward_identity_rejection():
    from app.admin_api import AdminStoreSettings

    item = {
        "id": "netwise",
        "title": "Netwise",
        "description": "Finance",
        "server": {"name": "netwise", "url": "https://netwise.example/mcp"},
    }
    env = {
        "MINDWEFT_ADMIN_MCP_SERVER_CATALOG": json.dumps([item]),
        "MINDWEFT_ADMIN_TENANT_MCP_BEARER_SERVERS": '["netwise"]',
    }
    assert AdminStoreSettings.from_env(env).mcp_server_catalog[0].tenant_credential == "bearer"
    item["server"]["forwardIdentity"] = True
    env["MINDWEFT_ADMIN_MCP_SERVER_CATALOG"] = json.dumps([item])
    with pytest.raises(RuntimeError, match="forwarded identity"):
        AdminStoreSettings.from_env(env)


def test_deployment_owned_service_rejects_rotation(setup):
    client, _, discovery, *_ = setup
    settings = client.app.state.admin_store_settings
    from dataclasses import replace

    catalog = tuple(
        item.model_copy(update={"tenant_credential": None}) for item in settings.mcp_server_catalog
    )
    client.app.state.admin_store_settings = replace(settings, mcp_server_catalog=catalog)
    assert rotate(client).status_code == 403
    discovery.assert_not_awaited()
