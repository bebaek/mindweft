from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.admin_store import SQLiteTenantConfigStore
from app.llm import MockLLMAdapter
from app.main import create_app
from app.session_auth import (
    SessionAuthSettings,
    hash_password,
    validate_session_auth_settings,
    verify_password,
)
from app.tools import build_local_tool_registry


def _configure_session(monkeypatch: pytest.MonkeyPatch) -> None:
    credential = {
        "admin": {
            "password_hash": hash_password(
                "correct horse battery staple", salt=b"0123456789abcdef"
            ),
            "principal": {
                "user_id": "console-admin",
                "tenant_id": "platform",
                "is_admin": True,
            },
        }
    }
    monkeypatch.setenv("MINIGENT_SESSION_CREDENTIALS", json.dumps(credential))
    monkeypatch.setenv("MINIGENT_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("MINIGENT_SESSION_COOKIE_SECURE", "false")


def _client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _configure_session(monkeypatch)
    app = create_app(
        llm_adapter=MockLLMAdapter(),
        tool_registry=build_local_tool_registry(),
        admin_store=SQLiteTenantConfigStore(str(tmp_path / "admin.db")),
    )
    return TestClient(app)


def test_password_hash_round_trip() -> None:
    encoded = hash_password("correct horse battery staple", salt=b"0123456789abcdef")

    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("incorrect", encoded)
    assert not verify_password("password", "not-a-supported-hash")


def test_session_settings_require_credentials_and_strong_secret() -> None:
    password_hash = hash_password("password", salt=b"0123456789abcdef")
    credentials = json.dumps(
        {
            "admin": {
                "password_hash": password_hash,
                "principal": {"user_id": "admin", "tenant_id": "platform", "is_admin": True},
            }
        }
    )

    with pytest.raises(RuntimeError, match="MINIGENT_SESSION_SECRET is required"):
        validate_session_auth_settings({"MINIGENT_SESSION_CREDENTIALS": credentials})
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        validate_session_auth_settings(
            {
                "MINIGENT_SESSION_CREDENTIALS": credentials,
                "MINIGENT_SESSION_SECRET": "too-short",
            }
        )
    with pytest.raises(RuntimeError, match="MINIGENT_SESSION_CREDENTIALS is required"):
        validate_session_auth_settings({"MINIGENT_SESSION_SECRET": "s" * 32})


def test_disabled_session_status_is_public() -> None:
    settings = SessionAuthSettings.from_env({})
    assert not settings.enabled

    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )
    response = client.get("/auth/session")

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "authenticated": False, "principal": None}


def test_admin_session_requires_admin_store_for_readiness(monkeypatch) -> None:
    _configure_session(monkeypatch)
    client = TestClient(
        create_app(llm_adapter=MockLLMAdapter(), tool_registry=build_local_tool_registry())
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["admin_store"] == "failed"


def test_login_creates_admin_session_and_logout_clears_it(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    initial = client.get("/auth/session")
    assert initial.json() == {"enabled": True, "authenticated": False, "principal": None}

    login = client.post(
        "/auth/session",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers={"Origin": "http://testserver"},
    )
    assert login.status_code == 200
    assert login.json()["principal"] == {
        "user_id": "console-admin",
        "tenant_id": "platform",
        "is_admin": True,
    }
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie

    session = client.get("/auth/session")
    assert session.json()["authenticated"] is True

    tenants = client.get("/admin/tenants")
    assert tenants.status_code == 200
    assert tenants.json()["tenants"] == []

    logout = client.delete("/auth/session", headers={"Origin": "http://testserver"})
    assert logout.status_code == 204
    assert client.get("/auth/session").json()["authenticated"] is False


def test_login_rejects_bad_credentials_and_cross_origin_requests(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    bad_password = client.post(
        "/auth/session",
        json={"username": "admin", "password": "wrong"},
        headers={"Origin": "http://testserver"},
    )
    assert bad_password.status_code == 401
    assert bad_password.json()["detail"] == "Invalid username or password"

    unknown_user = client.post(
        "/auth/session",
        json={"username": "unknown", "password": "wrong"},
        headers={"Origin": "http://testserver"},
    )
    assert unknown_user.status_code == 401
    assert unknown_user.json()["detail"] == "Invalid username or password"

    cross_origin = client.post(
        "/auth/session",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers={"Origin": "https://attacker.example"},
    )
    assert cross_origin.status_code == 403


def test_cookie_authenticated_mutation_requires_same_origin(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    login = client.post(
        "/auth/session",
        json={"username": "admin", "password": "correct horse battery staple"},
        headers={"Origin": "http://testserver"},
    )
    assert login.status_code == 200

    rejected = client.post(
        "/admin/tenants",
        json={"slug": "first", "name": "First tenant"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "Origin header required for session request"

    accepted = client.post(
        "/admin/tenants",
        json={"slug": "first", "name": "First tenant"},
        headers={"Origin": "http://testserver"},
    )
    assert accepted.status_code == 201
