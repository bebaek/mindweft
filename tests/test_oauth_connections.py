import asyncio
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.admin_store import SQLiteTenantConfigStore
from app.execution import build_execution_resolver_from_env
from app.main import create_app
from app.models import Tenant, TenantStatus, TenantUser, TenantUserRole, TenantUserStatus
from app.oauth import GenericOAuthProvider, OAuthCredentials
from app.oauth_connections import (
    connection_config,
    connection_key,
    connection_store,
    named_provider,
)

HEADERS = {"X-Mindweft-Tenant-Id": "t1", "X-Mindweft-User-Id": "u1"}
BASE = "/oauth/connections"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    definition = dict(
        provider_id="openai-codex",
        client_id="client",
        authorize_url="https://provider.test/authorize",
        token_url="https://provider.test/token",
        redirect_uri="http://127.0.0.1:8123/oauth/connections/callback",
        scope="openid",
        auth_params={},
    )
    definitions = {"work": definition, "personal": {**definition, "client_id": "second-client"}}
    monkeypatch.setenv("MINDWEFT_OAUTH_CONNECTIONS", json.dumps(definitions))
    monkeypatch.setenv("MINDWEFT_OAUTH_STORE_PATH", str(tmp_path / "oauth.db"))
    monkeypatch.setenv(
        "MINDWEFT_OAUTH_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"x" * 32).decode()
    )
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    for tenant in ("t1", "t2"):
        store.create_tenant(Tenant(id=tenant, slug=tenant, name=tenant, status=TenantStatus.ACTIVE))
        for user, role in (
            ("u1", TenantUserRole.OWNER),
            ("u2", TenantUserRole.ADMIN),
            ("member", TenantUserRole.MEMBER),
        ):
            store.create_tenant_user(
                TenantUser(
                    id=f"{tenant}-{user}",
                    tenant_id=tenant,
                    user_id=user,
                    role=role,
                    status=TenantUserStatus.ACTIVE,
                )
            )
    monkeypatch.setenv("MINDWEFT_TENANT_REGISTRY_REQUIRED", "true")
    monkeypatch.setenv("MINDWEFT_TENANT_USER_REGISTRY_REQUIRED", "true")
    with TestClient(create_app(admin_store=store), base_url="http://127.0.0.1:8123") as client:
        yield client, definitions


def login(client, name="work"):
    response = client.post(f"{BASE}/{name}/login", headers=HEADERS)
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]


def complete(client, state, headers=HEADERS):
    return client.post(f"{BASE}/complete", headers=headers, json={"state": state, "code": "code"})


@pytest.fixture
def exchange(monkeypatch):
    calls = []

    async def exchange(self, *, code, flow):
        calls.append(self.provider_id)
        return OAuthCredentials("access-secret", "refresh-secret", time.time() + 3600, "account-1")

    monkeypatch.setattr(GenericOAuthProvider, "exchange_login", exchange)
    return calls


def test_separate_accounts_and_tenants_with_no_legacy_fallback(configured, exchange):
    client, _ = configured
    state1, state2 = login(client), login(client, "personal")
    assert complete(client, state2).status_code == 200
    assert complete(client, state1).status_code == 200
    assert complete(client, state1).status_code == 400
    assert len(exchange) == 2
    listing = client.get(BASE, headers=HEADERS)
    assert listing.headers["cache-control"] == "no-store"
    assert all(item["connected"] for item in listing.json()["items"])
    assert "access-secret" not in listing.text and "refresh-secret" not in listing.text
    other = client.get(BASE, headers={**HEADERS, "X-Mindweft-Tenant-Id": "t2"})
    assert all(not item["connected"] for item in other.json()["items"])
    assert client.delete(f"{BASE}/work", headers=HEADERS).status_code == 204
    assert asyncio.run(named_provider("t1", "personal").get_credentials()) is not None
    connection_store().set(
        "openai-codex", OAuthCredentials("legacy", "legacy-refresh", time.time() + 3600)
    )
    assert asyncio.run(named_provider("t1", "work").get_credentials()) is None


@pytest.mark.parametrize(
    "operation", ["disconnect", "new-login", "config-change", "other-user", "other-tenant"]
)
def test_stale_or_wrong_principal_cannot_complete(configured, exchange, monkeypatch, operation):
    client, definitions = configured
    state = login(client)
    headers = HEADERS
    if operation == "disconnect":
        client.delete(f"{BASE}/work", headers=HEADERS)
    elif operation == "new-login":
        login(client)
    elif operation == "config-change":
        definitions["work"]["client_id"] = "changed"
        monkeypatch.setenv("MINDWEFT_OAUTH_CONNECTIONS", json.dumps(definitions))
    elif operation == "other-user":
        headers = {**HEADERS, "X-Mindweft-User-Id": "u2"}
    else:
        headers = {**HEADERS, "X-Mindweft-Tenant-Id": "t2"}
    assert complete(client, state, headers).status_code in {400, 409}
    assert asyncio.run(named_provider("t1", "work").get_credentials()) is None


def test_disconnect_during_exchange_cannot_resurrect_credentials(configured, monkeypatch):
    client, _ = configured
    state = login(client)

    async def exchange(self, *, code, flow):
        store = connection_store()
        store.advance_connection_epoch(
            connection_key("t1", "work", connection_config("work")), disconnect=True
        )
        return OAuthCredentials("late-access", "late-refresh", time.time() + 3600)

    monkeypatch.setattr(GenericOAuthProvider, "exchange_login", exchange)
    assert complete(client, state).status_code == 409
    assert asyncio.run(named_provider("t1", "work").get_credentials()) is None


def test_manager_permissions_and_callback_origin(configured):
    client, _ = configured
    headers = {**HEADERS, "X-Mindweft-User-Id": "member"}
    for method, path in (("get", BASE), ("post", BASE + "/work/login"), ("delete", BASE + "/work")):
        assert getattr(client, method)(path, headers=headers).status_code == 403
    assert (
        client.post(BASE + "/work/login", headers={**HEADERS, "Host": "127.0.0.1:9999"}).status_code
        == 409
    )
    response = client.get(BASE + "/callback?code=code&state=state", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/console/#")
    assert response.headers["referrer-policy"] == "no-referrer"


def test_named_default_runtime_binds_each_tenant(configured, monkeypatch):
    for prefix in ("MINDWEFT_", "MINIGENT_"):
        monkeypatch.setenv(prefix + "TENANT_EXECUTION_CONFIGS", "")
        monkeypatch.setenv(prefix + "LLM_PROFILES", "")
        monkeypatch.setenv(prefix + "LLM_DEFAULT_PROFILE", "")
    monkeypatch.setenv("MINDWEFT_LLM_PROVIDER", "generic-oauth")
    monkeypatch.setenv("MINDWEFT_LLM_MODEL", "model")
    monkeypatch.setenv("MINDWEFT_LLM_URL", "https://provider.test/responses")
    monkeypatch.setenv("MINDWEFT_LLM_OAUTH_CONNECTION_REF", "shared:work")
    resolver = build_execution_resolver_from_env()
    a, b = resolver.resolve("t1"), resolver.resolve("t2")
    assert a.config.tenant_id == "t1" and b.config.tenant_id == "t2"
    assert (
        a.llm_adapter._oauth_provider._credential_key
        != b.llm_adapter._oauth_provider._credential_key
    )
    assert a.llm_adapter._oauth_provider._fallback_credential_key is None


def test_bound_login_cannot_be_completed_through_legacy_provider(configured):
    client, _ = configured
    state = login(client)
    flow = connection_store().pop(state)
    with pytest.raises(ValueError, match="connection-specific"):
        asyncio.run(named_provider("t1", "personal").complete_login(code="code", flow=flow))


def test_import_and_refresh_only_touch_selected_connection(configured, monkeypatch):
    client, _ = configured
    payload = {
        "type": "oauth",
        "access": "old",
        "refresh": "refresh-work",
        "expires": 1,
        "accountId": "work-account",
    }
    assert client.post(BASE + "/work/import/pi", headers=HEADERS, json=payload).status_code == 200
    seen = []

    async def refresh(self, credentials):
        seen.append(credentials.refresh_token)
        return OAuthCredentials("new", "rotated", time.time() + 3600)

    monkeypatch.setattr(GenericOAuthProvider, "refresh", refresh)
    assert asyncio.run(named_provider("t1", "work").get_credentials()).access_token == "new"
    assert seen == ["refresh-work"]
    assert asyncio.run(named_provider("t1", "personal").get_credentials()) is None


def test_named_connection_toml_projection(configured, tmp_path):
    from mindweft_config.unified_config import load_unified_config_env

    _, definitions = configured
    lines = [
        "[llm]",
        'default = "work-model"',
        "[llm.providers.work-model]",
        'provider = "generic-oauth"',
        'model = "test"',
        'url = "https://provider.test/responses"',
        'oauth_connection_ref = "shared:work"',
    ]
    for name, definition in definitions.items():
        lines.append(f"[oauth.connections.{name}]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in definition.items())
    path = tmp_path / "named-oauth.toml"
    path.write_text("\n".join(lines))
    result = load_unified_config_env(path, source_env={})
    assert json.loads(result["MINIGENT_OAUTH_CONNECTIONS"]) == definitions
    assert result["MINIGENT_LLM_OAUTH_CONNECTION_REF"] == "shared:work"
    assert (
        json.loads(result["MINIGENT_LLM_PROFILES"])["work-model"]["oauth_connection_ref"]
        == "shared:work"
    )


def test_named_connection_requires_encryption_and_valid_definition(configured, monkeypatch):
    from app.oauth_connections import configured_connections

    client, definitions = configured
    monkeypatch.delenv("MINDWEFT_OAUTH_ENCRYPTION_KEY")
    monkeypatch.delenv("MINIGENT_OAUTH_ENCRYPTION_KEY", raising=False)
    assert client.get(BASE, headers=HEADERS).status_code == 503
    definitions["work"]["auth_params"] = {"state": "attacker"}
    monkeypatch.setenv("MINDWEFT_OAUTH_CONNECTIONS", json.dumps(definitions))
    with pytest.raises(RuntimeError, match="Invalid named"):
        configured_connections()


def test_named_flow_persists_and_expires(configured, exchange):
    from dataclasses import replace

    client, _ = configured
    state = login(client)
    # Reopening the encrypted store preserves the bound flow and owner.
    store = connection_store()
    flow = store.pop(state)
    assert flow.context["user_id"] == "u1"
    store.put(state, replace(flow, created_at=time.time() - 700))
    assert complete(client, state).status_code == 400
    assert exchange == []
