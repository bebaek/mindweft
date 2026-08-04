from pathlib import Path

from fastapi.testclient import TestClient

from app.admin_store import SQLiteTenantConfigStore, UserExecutionConfigConflictError
from app.main import create_app
from app.user_execution import validate_user_execution_config

AUTH_HEADERS = {
    "X-Minigent-User-Id": "user-1",
    "X-Minigent-Tenant-Id": "tenant-1",
}
OTHER_USER_HEADERS = {
    "X-Minigent-User-Id": "user-2",
    "X-Minigent-Tenant-Id": "tenant-1",
}


def _config_payload() -> dict[str, object]:
    return {
        "defaults": {"agent_ref": "user:product-engineer"},
        "skills": {
            "items": [
                {
                    "id": "user:python-style",
                    "name": "python-style",
                    "system_prompt": "Prefer typed Python and pytest.",
                }
            ]
        },
        "mcp_servers": {
            "items": [
                {
                    "id": "user:linear",
                    "name": "linear",
                    "url": "https://mcp.example.com/linear",
                    "credential_ref": "oauth:linear-primary",
                }
            ]
        },
        "capability_profiles": {
            "items": [
                {
                    "id": "user:product-tools",
                    "name": "product-tools",
                    "mcp_server_refs": ["user:linear", "shared:github"],
                }
            ]
        },
        "agents": {
            "items": [
                {
                    "id": "user:product-engineer",
                    "name": "product-engineer",
                    "skill_refs": ["shared:coding-workspace", "user:python-style"],
                    "capability_profile_ref": "user:product-tools",
                }
            ]
        },
    }


def test_user_execution_config_validation_accepts_additive_personal_resources() -> None:
    report = validate_user_execution_config(_config_payload())

    assert report.valid is True
    assert report.errors == []
    assert report.config is not None
    assert report.config.skills.items[0].system_prompt == "Prefer typed Python and pytest."
    assert report.config.capability_profiles.items[0].mcp_server_refs == [
        "user:linear",
        "shared:github",
    ]


def test_user_execution_config_validation_accepts_camel_case_compatibility() -> None:
    report = validate_user_execution_config(
        {
            "skills": {
                "items": [
                    {
                        "id": "user:style",
                        "name": "style",
                        "systemPrompt": "Use my style.",
                    }
                ]
            },
            "mcpServers": {
                "items": [
                    {
                        "id": "user:toolbox",
                        "name": "toolbox",
                        "url": "https://mcp.example.com/tools",
                        "credentialRef": "oauth:toolbox",
                        "allowedTools": ["search"],
                    }
                ]
            },
            "capabilityProfiles": {
                "items": [
                    {
                        "id": "user:tools",
                        "name": "tools",
                        "mcpServerRefs": ["user:toolbox"],
                    }
                ]
            },
            "agents": {
                "items": [
                    {
                        "id": "user:assistant",
                        "name": "assistant",
                        "skillRefs": ["user:style"],
                        "capabilityProfileRef": "user:tools",
                    }
                ]
            },
            "defaults": {"agentRef": "user:assistant"},
        }
    )

    assert report.valid is True
    assert report.config is not None
    assert report.config.mcp_servers.items[0].credential_ref == "oauth:toolbox"
    assert report.config.agents.items[0].capability_profile_ref == "user:tools"


def test_user_execution_config_validation_rejects_missing_personal_reference() -> None:
    payload = _config_payload()
    payload["capability_profiles"] = {
        "items": [
            {
                "id": "user:product-tools",
                "name": "product-tools",
                "mcp_server_refs": ["user:missing"],
            }
        ]
    }

    report = validate_user_execution_config(payload)

    assert report.valid is False
    assert any("unknown personal resources: user:missing" in error for error in report.errors)


def test_user_execution_config_validation_requires_credential_refs_for_auth_headers() -> None:
    payload = _config_payload()
    payload["mcp_servers"] = {
        "items": [
            {
                "id": "user:linear",
                "name": "linear",
                "url": "https://mcp.example.com/linear",
                "headers": {"Authorization": "Bearer secret"},
            }
        ]
    }

    report = validate_user_execution_config(payload)

    assert report.valid is False
    assert any("credential_ref" in error for error in report.errors)


def test_user_execution_store_is_scoped_and_versioned(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    payload = _config_payload()

    created = store.upsert_user_execution_config("tenant-1", "user-1", payload, expected_version=0)
    updated = store.upsert_user_execution_config(
        "tenant-1", "user-1", payload, expected_version=created.version
    )

    assert created.version == 1
    assert updated.version == 2
    assert store.get_user_execution_config("tenant-1", "user-1") == updated
    assert store.get_user_execution_config("tenant-1", "user-2") is None
    assert store.get_user_execution_config("tenant-2", "user-1") is None

    try:
        store.upsert_user_execution_config("tenant-1", "user-1", payload, expected_version=1)
    except UserExecutionConfigConflictError as exc:
        assert exc.actual_version == 2
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected a version conflict")


def test_user_execution_config_api_round_trip_is_principal_scoped(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    app = create_app(admin_store=store)

    with TestClient(app) as client:
        missing = client.get("/me/execution-config", headers=AUTH_HEADERS)
        created = client.put(
            "/me/execution-config",
            headers=AUTH_HEADERS,
            json={"config": _config_payload(), "expected_version": 0},
        )
        fetched = client.get("/me/execution-config", headers=AUTH_HEADERS)
        other_user = client.get("/me/execution-config", headers=OTHER_USER_HEADERS)
        conflict = client.put(
            "/me/execution-config",
            headers=AUTH_HEADERS,
            json={"config": _config_payload(), "expected_version": 0},
        )
        deleted = client.delete("/me/execution-config?expected_version=1", headers=AUTH_HEADERS)

    assert missing.status_code == 404
    assert created.status_code == 200
    assert created.json()["version"] == 1
    assert created.json()["config"]["agents"]["items"][0]["id"] == "user:product-engineer"
    assert fetched.status_code == 200
    assert other_user.status_code == 404
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["actual_version"] == 1
    assert deleted.status_code == 204


def test_user_execution_config_validate_endpoint_returns_errors() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/me/execution-config/validate",
            headers=AUTH_HEADERS,
            json={
                "config": {"skills": {"items": [{"id": "shared:not-personal", "name": "invalid"}]}}
            },
        )

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert "user:" in response.json()["errors"][0]


def test_user_execution_config_storage_endpoint_requires_configured_store() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/me/execution-config", headers=AUTH_HEADERS)

    assert response.status_code == 503
