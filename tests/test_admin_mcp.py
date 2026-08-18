from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mcp.types import LATEST_PROTOCOL_VERSION

from app.admin_mcp import (
    ADMIN_CHAT_SETUP_TOOL,
    LEGACY_ADMIN_TOOL_ALIASES,
    build_admin_chat_tool_registry,
)
from app.admin_mutations import AdminMutationService
from app.admin_store import SQLiteTenantConfigStore
from app.execution import ADMIN_EXECUTION_CONFIG_KEY
from app.llm import MockLLMAdapter
from app.main import create_app
from app.models import Principal
from app.tools import build_local_tool_registry

ADMIN_HEADERS = {
    "X-Minigent-User-Id": "admin-1",
    "X-Minigent-Tenant-Id": "tenant-1",
    "X-Minigent-Admin": "true",
}
MINDWEFT_ADMIN_HEADERS = {
    "X-Mindweft-User-Id": "admin-1",
    "X-Mindweft-Tenant-Id": "tenant-1",
    "X-Mindweft-Admin": "true",
}
USER_HEADERS = {
    "X-Minigent-User-Id": "user-1",
    "X-Minigent-Tenant-Id": "tenant-1",
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
}
MCP_METADATA = {
    "io.modelcontextprotocol/protocolVersion": LATEST_PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "minigent-test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _request(
    client: TestClient, request_id: int, method: str, params: dict, headers: dict[str, str]
):
    mcp_headers = {**MCP_HEADERS, **headers, "Mcp-Method": method}
    if method == "tools/call" and isinstance(params.get("name"), str):
        mcp_headers["Mcp-Name"] = params["name"]
    return client.post(
        "/mcp",
        headers=mcp_headers,
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


def test_admin_mcp_requires_admin_and_exposes_only_read_tools() -> None:
    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    with TestClient(app) as client:
        rejected = _request(client, 1, "server/discover", {"_meta": MCP_METADATA}, USER_HEADERS)
        assert rejected.status_code == 403

        discovered = _request(
            client, 2, "server/discover", {"_meta": MCP_METADATA}, MINDWEFT_ADMIN_HEADERS
        )
        listed = _request(client, 3, "tools/list", {"_meta": MCP_METADATA}, ADMIN_HEADERS)
        status = _request(
            client,
            4,
            "tools/call",
            {"name": "get_setup_status", "arguments": {}, "_meta": MCP_METADATA},
            ADMIN_HEADERS,
        )

    assert discovered.status_code == 200
    assert discovered.json()["result"]["supportedVersions"] == [LATEST_PROTOCOL_VERSION]
    assert listed.status_code == 200
    assert {tool["name"] for tool in listed.json()["result"]["tools"]} == {
        "get_setup_status",
        "diagnose_tenant_setup",
        "list_mcp_server_catalog_access",
    }
    assert status.status_code == 200
    result = status.json()["result"]["structuredContent"]
    assert "readiness_checks" in result
    assert "authorization" not in str(result).lower()


def _chat_with_tool(client: TestClient, headers: dict[str, str], tool_name: str) -> str:
    created = client.post("/threads", headers=headers)
    assert created.status_code == 200
    thread_id = created.json()["thread_id"]
    message = client.post(
        f"/threads/{thread_id}/messages",
        headers=headers,
        json={"content": f"/tool {tool_name}"},
    )
    assert message.status_code == 200
    run = client.post(f"/threads/{thread_id}/run", headers=headers)
    assert run.status_code == 200
    return run.json()["reply"]


def test_admin_mutation_confirmation_is_bound_and_single_use(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    app = create_app(admin_store=store, tenant_config_source="store")

    with TestClient(app) as client:
        created = client.post(
            "/admin/tenants",
            headers=ADMIN_HEADERS,
            json={"id": "tenant-1", "slug": "tenant-1", "name": "Before"},
        )
        assert created.status_code == 201
        service = AdminMutationService(app)
        proposal = service.propose_tenant_update(
            admin_user_id="admin-1",
            tenant_id="tenant-1",
            changes={"name": "After"},
        )
        assert proposal["requires_confirmation"] is True
        assert proposal["diff"]["name"] == {"from": "Before", "to": "After"}

        with pytest.raises(HTTPException):
            service.confirm(admin_user_id="different-admin", token=proposal["confirmation_id"])
        confirmed = service.confirm(admin_user_id="admin-1", token=proposal["confirmation_id"])
        assert confirmed["confirmed"] is True
        updated_tenant = store.get_tenant("tenant-1")
        assert updated_tenant is not None
        assert updated_tenant.name == "After"
        with pytest.raises(HTTPException):
            service.confirm(admin_user_id="admin-1", token=proposal["confirmation_id"])


def test_admin_mutation_confirmation_supports_domains_entitlements_and_rejects_secrets(
    tmp_path: Path,
) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    app = create_app(admin_store=store, tenant_config_source="store")

    with TestClient(app) as client:
        created = client.post(
            "/admin/tenants",
            headers=ADMIN_HEADERS,
            json={"id": "tenant-1", "slug": "tenant-1", "name": "Tenant"},
        )
        assert created.status_code == 201
        service = AdminMutationService(app)

        domain_proposal = service.propose_domain_add(
            admin_user_id="admin-1", tenant_id="tenant-1", domain="App.Example.COM."
        )
        assert domain_proposal["diff"] == {"domain": {"from": None, "to": "app.example.com"}}
        domain_result = service.confirm(
            admin_user_id="admin-1", token=domain_proposal["confirmation_id"]
        )
        assert domain_result["confirmed"] is True
        assert store.list_tenant_domains("tenant-1")[0].domain == "app.example.com"

        entitlement_proposal = service.propose_entitlements(
            admin_user_id="admin-1",
            tenant_id="tenant-1",
            features={"chat": True},
            limits={"requests": 100},
        )
        entitlement_result = service.confirm(
            admin_user_id="admin-1", token=entitlement_proposal["confirmation_id"]
        )
        assert entitlement_result["confirmed"] is True
        entitlements = store.get_tenant_entitlements("tenant-1")
        assert entitlements is not None
        assert entitlements.features == {"chat": True}
        assert entitlements.limits == {"requests": 100}

        with pytest.raises(HTTPException):
            service.propose_tenant_update(
                admin_user_id="admin-1",
                tenant_id="tenant-1",
                changes={"metadata": {"api_key": "must-not-enter-chat"}},
            )


def test_admin_chat_registry_advertises_only_canonical_mindweft_tools() -> None:
    registry = build_admin_chat_tool_registry(
        SimpleNamespace(state=SimpleNamespace(admin_store=None)),
        Principal(user_id="admin-1", tenant_id="tenant-1", is_admin=True),
    )
    names = {spec.name for spec in registry.specs()}

    assert set(LEGACY_ADMIN_TOOL_ALIASES.values()) <= names
    assert all(name.startswith("mindweft_admin_") for name in names)
    assert not names.intersection(LEGACY_ADMIN_TOOL_ALIASES)


def test_admin_chat_can_call_mindweft_admin_tools_in_process() -> None:
    app = create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    with TestClient(app) as client:
        admin_reply = _chat_with_tool(client, ADMIN_HEADERS, ADMIN_CHAT_SETUP_TOOL)
        user_reply = _chat_with_tool(client, USER_HEADERS, ADMIN_CHAT_SETUP_TOOL)

    assert admin_reply.startswith("Tool result:")
    assert "readiness_checks" in admin_reply
    assert user_reply == f"Mock reply: /tool {ADMIN_CHAT_SETUP_TOOL}"


def test_platform_admin_can_configure_chat_execution_without_a_tenant(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    app = create_app(admin_store=store, tenant_config_source="store")

    with TestClient(app) as client:
        missing = client.get("/admin/execution-config", headers=ADMIN_HEADERS)
        rejected = client.put(
            "/admin/execution-config",
            headers=USER_HEADERS,
            json={"config": {"llm": {"provider": "mock"}}},
        )
        reserved_tenant = client.post(
            "/admin/tenants",
            headers=ADMIN_HEADERS,
            json={
                "id": ADMIN_EXECUTION_CONFIG_KEY,
                "slug": "reserved-admin",
                "name": "Reserved Admin",
            },
        )
        validated = client.post(
            "/admin/execution-config/validate",
            headers=ADMIN_HEADERS,
            json={"config": {"llm": {"provider": "mock"}}},
        )
        saved = client.put(
            "/admin/execution-config",
            headers=ADMIN_HEADERS,
            json={
                "config": {
                    "llm": {"provider": "mock", "api_key": "admin-secret"},
                    "tools": {"allowed_local_tools": []},
                }
            },
        )
        fetched = client.get("/admin/execution-config", headers=ADMIN_HEADERS)
        stored_before_delete = store.get_raw_config(ADMIN_EXECUTION_CONFIG_KEY)
        tenant_configs = client.get("/admin/execution-config-tenants", headers=ADMIN_HEADERS)
        echo_reply = _chat_with_tool(client, ADMIN_HEADERS, "echo")
        admin_reply = _chat_with_tool(client, ADMIN_HEADERS, ADMIN_CHAT_SETUP_TOOL)
        deleted = client.delete("/admin/execution-config", headers=ADMIN_HEADERS)
        put_audit = client.get(
            f"/admin/tenants/{ADMIN_EXECUTION_CONFIG_KEY}/audit-records",
            params={"action": "admin_execution_config.put"},
            headers=ADMIN_HEADERS,
        )
        delete_audit = client.get(
            f"/admin/tenants/{ADMIN_EXECUTION_CONFIG_KEY}/audit-records",
            params={"action": "admin_execution_config.delete"},
            headers=ADMIN_HEADERS,
        )
        missing_after_delete = client.get("/admin/execution-config", headers=ADMIN_HEADERS)

    assert missing.status_code == 404
    assert rejected.status_code == 403
    assert reserved_tenant.status_code == 400
    assert validated.status_code == 200
    assert validated.json()["valid"] is True
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert fetched.json()["config"]["llm"]["api_key"] == "<redacted>"
    assert stored_before_delete is not None
    assert stored_before_delete["llm"]["api_key"] == "admin-secret"
    assert tenant_configs.json() == {"tenants": []}
    assert echo_reply == "Mock reply: /tool echo"
    assert admin_reply.startswith("Tool result:")
    assert deleted.status_code == 204
    assert put_audit.status_code == 200
    assert put_audit.json()["total"] == 1
    put_record = put_audit.json()["audit_records"][0]
    assert put_record["resource_type"] == "execution_config"
    assert put_record["resource_id"] == ADMIN_EXECUTION_CONFIG_KEY
    assert put_record["new_values"]["llm"]["api_key"] == "<redacted>"
    assert delete_audit.status_code == 200
    assert delete_audit.json()["total"] == 1
    delete_record = delete_audit.json()["audit_records"][0]
    assert delete_record["old_values"]["llm"]["api_key"] == "<redacted>"
    assert delete_record["new_values"] is None
    assert missing_after_delete.status_code == 404
    assert store.get_raw_config(ADMIN_EXECUTION_CONFIG_KEY) is None


def test_platform_execution_update_refreshes_other_replica_cache(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_raw_config(
        ADMIN_EXECUTION_CONFIG_KEY,
        {
            "llm": {"provider": "mock"},
            "tools": {"allowed_local_tools": ["echo"]},
        },
    )
    writer_app = create_app(admin_store=store, tenant_config_source="store")
    other_replica_app = create_app(admin_store=store, tenant_config_source="store")

    with TestClient(writer_app) as writer, TestClient(other_replica_app) as other_replica:
        original_reply = _chat_with_tool(other_replica, ADMIN_HEADERS, "echo")
        updated = writer.put(
            "/admin/execution-config",
            headers=ADMIN_HEADERS,
            json={
                "config": {
                    "llm": {"provider": "mock"},
                    "tools": {"allowed_local_tools": []},
                }
            },
        )
        refreshed_reply = _chat_with_tool(other_replica, ADMIN_HEADERS, "echo")

    assert original_reply.startswith("Tool result:")
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert refreshed_reply == "Mock reply: /tool echo"


def test_admin_chat_uses_deployment_execution_when_tenant_has_no_config(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    app = create_app(admin_store=store, tenant_config_source="store")

    with TestClient(app) as client:
        admin_reply = _chat_with_tool(client, ADMIN_HEADERS, ADMIN_CHAT_SETUP_TOOL)
        user_create = client.post("/threads", headers=USER_HEADERS)

    assert admin_reply.startswith("Tool result:")
    assert "readiness_checks" in admin_reply
    assert user_create.status_code == 403
    assert user_create.json()["detail"] == ("Tenant 'tenant-1' has no execution configuration")


def test_admin_chat_does_not_require_a_tenant_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDWEFT_TENANT_REGISTRY_REQUIRED", "true")
    monkeypatch.setenv("MINDWEFT_TENANT_USER_REGISTRY_REQUIRED", "true")
    app = create_app(
        admin_store=SQLiteTenantConfigStore(str(tmp_path / "admin.db")),
        tenant_config_source="store",
    )

    with TestClient(app) as client:
        admin_reply = _chat_with_tool(client, ADMIN_HEADERS, ADMIN_CHAT_SETUP_TOOL)

    assert admin_reply.startswith("Tool result:")
    assert "readiness_checks" in admin_reply
