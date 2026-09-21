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
    monkeypatch.setenv("MINDWEFT_LOCAL_API_ORIGIN", ORIGIN)
    monkeypatch.setenv("MINDWEFT_LOCAL_LAUNCH_ID", LAUNCH)
    # Deliberately leave insecure underlying auth configured: middleware must
    # block fallback even if someone misconfigures the environment.
    monkeypatch.setenv("MINDWEFT_AUTH_MODE", "dev-headers")
    app = FastAPI()

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
    ticket = client.post("/local-auth/ticket", headers=bearer).json()["ticket"]
    headers = {
        "Origin": ORIGIN,
        "X-Mindweft-Launch-Id": LAUNCH,
        "X-Mindweft-Browser-Ticket": ticket,
    }
    result = client.post("/local-auth/exchange", headers=headers)
    assert result.status_code == 200
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=strict" in result.headers["set-cookie"]
    assert "Domain=" not in result.headers["set-cookie"]
    assert client.post("/local-auth/exchange", headers=headers).status_code == 401
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
    assert client.post("/local-auth/ticket").status_code == 401


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
    assert client.post("/local-auth/ticket", headers=headers).status_code == 401
    assert client.delete("/auth/session", headers=headers).status_code == 200
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
    import app.local_auth as auth

    client, bearer = local
    headers = exchange(client, bearer)
    ticket = client.post("/local-auth/ticket", headers=bearer).json()["ticket"]
    now = auth.time.monotonic()
    monkeypatch.setattr(auth.time, "monotonic", lambda: now + auth.SESSION_SECONDS + 1)
    assert (
        client.post(
            "/local-auth/exchange",
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
        assert client.post("/local-auth/ticket").status_code == 401
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
        assert client.get("/local-auth/ticket").status_code == 404
