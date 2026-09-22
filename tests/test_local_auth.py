from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import require_principal
from app.local_auth import install_local_auth
from app.models import Principal
from mindweft_workspace.local_credentials import issue_credential

LAUNCH = "a" * 32
ORIGIN = "http://127.0.0.1:8123"


@pytest.fixture
def local(monkeypatch, tmp_path):
    credential = issue_credential(tmp_path.resolve() / "api", launch_id=LAUNCH)
    monkeypatch.setenv("MINDWEFT_LOCAL_API_CREDENTIAL_DIR", str(tmp_path.resolve() / "api"))
    monkeypatch.setenv("MINDWEFT_LOCAL_INSTANCE_NAME", "test")
    monkeypatch.setenv("MINDWEFT_LOCAL_API_ORIGIN", ORIGIN)
    monkeypatch.setenv("MINDWEFT_LOCAL_LAUNCH_ID", LAUNCH)
    from app.admin_store import SQLiteTenantConfigStore
    from app.session_auth import build_session_auth_router
    from mindweft_workspace.provisioning import provision_coding_user

    env = {"MINDWEFT_ADMIN_DB_PATH": str(tmp_path / "admin.db")}
    provision_coding_user(env, token=credential.token, origin=ORIGIN, binding=LAUNCH)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = FastAPI()
    app.state.admin_store = SQLiteTenantConfigStore(env["MINDWEFT_ADMIN_DB_PATH"])
    app.include_router(build_session_auth_router())

    @app.api_route("/protected", methods=["GET", "POST"])
    def protected(principal: Annotated[Principal, Depends(require_principal)]):
        return principal.model_dump()

    install_local_auth(app)
    with TestClient(app, base_url=ORIGIN) as client:
        yield (
            client,
            {"Authorization": f"Bearer {credential.token}", "X-Mindweft-Launch-Id": LAUNCH},
        )


def exchange(client, bearer):
    ticket = client.post("/auth/session/ticket", headers=bearer).json()["ticket"]
    headers = {
        "Origin": ORIGIN,
        "X-Mindweft-Launch-Id": LAUNCH,
        "X-Mindweft-Browser-Ticket": ticket,
    }
    result = client.post("/auth/session/exchange", headers=headers)
    assert result.status_code == 200
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=strict" in result.headers["set-cookie"]
    assert "Domain=" not in result.headers["set-cookie"]
    assert client.post("/auth/session/exchange", headers=headers).status_code == 401
    return {
        "Origin": ORIGIN,
        "X-Mindweft-Launch-Id": LAUNCH,
        "X-Mindweft-Session-Key": result.json()["session_key"],
    }


def test_bearer_and_principal_cannot_be_overridden(local):
    client, bearer = local
    fake = {
        "X-Mindweft-Tenant-Id": "evil",
        "X-Mindweft-User-Id": "evil",
        "X-Mindweft-Admin": "true",
    }
    assert client.get("/protected", headers=fake).status_code == 401
    assert client.get("/protected", headers={**fake, **bearer}).json() == {
        "tenant_id": "demo-tenant",
        "user_id": "demo-user",
        "is_admin": False,
    }
    assert client.post("/auth/session/ticket").status_code == 401


def test_session_exchange_reload_logout_and_csrf(local):
    client, bearer = local
    headers = exchange(client, bearer)
    assert client.get("/auth/session", headers=headers).json()["authenticated"]
    assert client.post("/protected", headers=headers).status_code == 200
    # A cookie stolen by a different loopback port is insufficient.
    assert client.get("/protected").status_code == 401
    assert client.get("/protected", headers={"X-Mindweft-Launch-Id": LAUNCH}).status_code == 401
    assert (
        client.post(
            "/protected", headers={k: v for k, v in headers.items() if k != "Origin"}
        ).status_code
        == 403
    )
    assert (
        client.get("/protected", headers={**headers, "Origin": "http://127.0.0.1:9999"}).status_code
        == 403
    )
    assert client.post("/auth/session/ticket", headers=headers).status_code == 401
    assert client.delete("/auth/session", headers=headers).status_code == 204
    assert not client.get("/auth/session", headers=headers).json()["authenticated"]
    assert client.get("/protected", headers=headers).status_code == 401


def test_dns_rebinding_cross_origin_and_stale_launch(local):
    client, bearer = local
    for headers in (
        {"Host": "attacker.test"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        assert client.get("/protected", headers={**bearer, **headers}).status_code == 403
    assert (
        client.get("/protected", headers={**bearer, "X-Mindweft-Launch-Id": "b" * 32}).status_code
        == 409
    )


def test_ticket_expiry_and_session_expiry(local, monkeypatch):
    import app.session_handoff as auth

    client, bearer = local
    headers = exchange(client, bearer)
    ticket = client.post("/auth/session/ticket", headers=bearer).json()["ticket"]
    now = auth.time.monotonic()
    monkeypatch.setattr(auth.time, "monotonic", lambda: now + 28801)
    assert (
        client.post(
            "/auth/session/exchange",
            headers={
                "Origin": ORIGIN,
                "X-Mindweft-Launch-Id": LAUNCH,
                "X-Mindweft-Browser-Ticket": ticket,
            },
        ).status_code
        == 401
    )
    assert not client.get("/auth/session", headers=headers).json()["authenticated"]


def test_gateway_requires_separate_bearer(monkeypatch, tmp_path):
    directory = tmp_path.resolve() / "gateway"
    credential = issue_credential(directory, launch_id=LAUNCH)
    monkeypatch.setenv("MINDWEFT_LOCAL_GATEWAY_CREDENTIAL_DIR", str(directory))
    monkeypatch.setenv("MINDWEFT_LOCAL_GATEWAY_ORIGIN", ORIGIN)
    monkeypatch.setenv("MINDWEFT_LOCAL_LAUNCH_ID", LAUNCH)
    from mindweft_workspace.bridge.gateway import GatewaySettings, create_gateway_app

    with TestClient(create_gateway_app(GatewaySettings(bridges=[])), base_url=ORIGIN) as client:
        assert client.post("/mcp/fs-workspace", json={}).status_code == 401
        assert client.post("/auth/session/ticket").status_code == 401
        assert (
            client.post(
                "/mcp/fs-workspace", headers={"Authorization": "Bearer " + "x" * 43}, json={}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/mcp/fs-workspace",
                headers={"Authorization": f"Bearer {credential.token}"},
                json={},
            ).status_code
            == 404
        )


def test_ordinary_deployment_unchanged(monkeypatch):
    monkeypatch.delenv("MINDWEFT_LOCAL_API_CREDENTIAL_DIR", raising=False)
    app = FastAPI()

    @app.get("/public")
    def public():
        return {"ok": True}

    install_local_auth(app)
    with TestClient(app) as client:
        assert client.get("/public").json() == {"ok": True}
        assert client.get("/auth/session/ticket").status_code == 404


@pytest.mark.parametrize("target", ["tenant", "membership"])
def test_suspension_blocks_bearer_session_and_pending_ticket(local, target):
    from app.models import TenantStatus, TenantUserStatus

    client, bearer = local
    headers = exchange(client, bearer)
    ticket = client.post("/auth/session/ticket", headers=bearer).json()["ticket"]
    store = client.app.state.admin_store
    if target == "tenant":
        store.update_tenant("demo-tenant", status=TenantStatus.SUSPENDED)
    else:
        store.update_tenant_user("demo-tenant", "coding-owner", status=TenantUserStatus.SUSPENDED)
    assert client.get("/protected", headers=bearer).status_code == 403
    assert client.get("/protected", headers=headers).status_code == 403
    assert not client.get("/auth/session", headers=headers).json()["authenticated"]
    assert client.post("/auth/session/ticket", headers=bearer).status_code == 403
    assert (
        client.post(
            "/auth/session/exchange", headers={**headers, "X-Mindweft-Browser-Ticket": ticket}
        ).status_code
        == 403
    )


def test_rotation_revokes_bearer_session_and_pending_ticket(local, monkeypatch):
    import json

    from app.session_handoff import digest

    client, bearer = local
    headers = exchange(client, bearer)
    ticket = client.post("/auth/session/ticket", headers=bearer).json()["ticket"]
    monkeypatch.setenv(
        "MINDWEFT_AUTH_TOKEN_HASHES",
        json.dumps(
            {digest("replacement-credential"): {"tenant_id": "demo-tenant", "user_id": "demo-user"}}
        ),
    )
    assert client.get("/protected", headers=bearer).status_code == 401
    assert client.get("/protected", headers=headers).status_code == 401
    assert not client.get("/auth/session", headers=headers).json()["authenticated"]
    assert (
        client.post(
            "/auth/session/exchange", headers={**headers, "X-Mindweft-Browser-Ticket": ticket}
        ).status_code
        == 401
    )


def test_logout_revokes_replayed_cookie_and_keeps_other_session(local):
    client, bearer = local
    headers = exchange(client, bearer)
    cookie = dict(client.cookies)
    other_headers = exchange(client, bearer)
    other_cookie = dict(client.cookies)
    assert cookie != other_cookie
    client.cookies.clear()
    client.cookies.update(cookie)
    assert client.delete("/auth/session", headers=headers).status_code == 204
    client.cookies.update(cookie)
    assert client.get("/protected", headers=headers).status_code == 401
    client.cookies.clear()
    client.cookies.update(other_cookie)
    assert client.get("/protected", headers=other_headers).status_code == 200


def test_normal_auth_works_without_local_middleware_and_ignores_injected_principal(local):
    from app.session_auth import build_session_auth_router

    local_client, bearer = local
    app = FastAPI()
    app.state.admin_store = local_client.app.state.admin_store
    app.include_router(build_session_auth_router())

    @app.middleware("http")
    async def obsolete_shortcut(request, call_next):
        request.state.local_principal = Principal(tenant_id="evil", user_id="evil", is_admin=True)
        return await call_next(request)

    @app.get("/protected")
    async def protected(principal: Annotated[Principal, Depends(require_principal)]):
        return principal.model_dump()

    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/protected").status_code == 401
        assert client.get("/protected", headers=bearer).json()["user_id"] == "demo-user"
        headers = exchange(client, bearer)
        assert client.get("/protected", headers=headers).json()["is_admin"] is False
        assert client.get("/auth/session", headers=headers).json()["authenticated"]
        assert client.get("/protected").status_code == 401
        assert client.delete("/auth/session", headers=headers).status_code == 204


def test_provisioning_preserves_membership_and_suspension(local, monkeypatch):
    from app.models import TenantUserRole, TenantUserStatus
    from mindweft_workspace.provisioning import provision_coding_user

    client, _ = local
    store = client.app.state.admin_store
    store.update_tenant_user(
        "demo-tenant", "coding-owner", role=TenantUserRole.MEMBER, status=TenantUserStatus.SUSPENDED
    )
    env = {"MINDWEFT_ADMIN_DB_PATH": str(store.db_path)}
    provision_coding_user(env, token="new-credential", origin=ORIGIN, binding="b" * 32)
    membership = store.get_tenant_user_by_user_id("demo-tenant", "demo-user")
    assert membership.role == TenantUserRole.MEMBER
    assert membership.status == TenantUserStatus.SUSPENDED
    assert env["MINDWEFT_AUTH_MODE"] == "static-tokens"
    assert "new-credential" not in env["MINDWEFT_AUTH_TOKEN_HASHES"]


def test_new_server_rejects_old_browser_session(local):
    from app.session_auth import build_session_auth_router

    client, bearer = local
    headers = exchange(client, bearer)
    cookie = dict(client.cookies)
    app = FastAPI()
    app.state.admin_store = client.app.state.admin_store
    app.include_router(build_session_auth_router())
    with TestClient(app, base_url=ORIGIN) as restarted:
        restarted.cookies.update(cookie)
        assert not restarted.get("/auth/session", headers=headers).json()["authenticated"]
        exchange(restarted, bearer)
        # The old proof cannot authorize a newly issued session.
        assert not restarted.get("/auth/session", headers=headers).json()["authenticated"]


def test_provisioned_identity_uses_normal_application_routes(local):
    from app.llm import MockLLMAdapter
    from app.main import create_app
    from app.tools import build_local_tool_registry

    local_client, bearer = local
    app = create_app(
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(),
        admin_store=local_client.app.state.admin_store,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/threads").status_code == 401
        assert client.get("/execution-options", headers=bearer).status_code == 200
        created = client.post("/threads", headers=bearer)
        assert created.status_code == 200
        headers = exchange(client, bearer)
        assert client.get("/threads", headers=headers).status_code == 200
        assert client.get("/auth/session", headers=headers).json()["principal"] == {
            "tenant_id": "demo-tenant",
            "user_id": "demo-user",
            "is_admin": False,
        }


def test_password_sessions_cannot_bypass_cross_port_proof(local, monkeypatch):
    import json

    from app.models import Principal
    from app.session_auth import SessionAuthSettings, _encode_session, hash_password

    client, _ = local
    principal = Principal(user_id="demo-user", tenant_id="demo-tenant")
    monkeypatch.setenv(
        "MINDWEFT_SESSION_CREDENTIALS",
        json.dumps(
            {
                "user": {
                    "password_hash": hash_password("test-password"),
                    "principal": principal.model_dump(),
                }
            }
        ),
    )
    response = client.post(
        "/auth/session",
        headers={"Origin": ORIGIN},
        json={"username": "user", "password": "test-password"},
    )
    assert response.status_code == 404
    settings = SessionAuthSettings.from_env()
    token = _encode_session(
        principal, settings, username="user", source="environment", credential_version=0
    )
    client.cookies.set(settings.cookie_name, token)
    assert not client.get("/auth/session").json()["authenticated"]
    assert client.get("/protected").status_code == 401


def test_callback_is_transport_only_for_local_launcher(local):
    # The same callback may be reached cross-site, but never exchanges a token.
    from app.oauth_connections import build_oauth_connections_router

    client, _ = local
    client.app.include_router(build_oauth_connections_router())
    response = client.get(
        "/oauth/connections" + "/callback?code=test&state=test",
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (
        client.post(
            "/oauth/connections" + "/work/login", headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
