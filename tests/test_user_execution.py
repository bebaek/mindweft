import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.admin_store import (
    SQLiteTenantConfigStore,
    UserExecutionConfigConflictError,
    UserExecutionCredentialConflictError,
)
from app.execution import FixedTenantExecutionResolver, parse_tenant_execution_config
from app.llm import LLMAdapter
from app.main import create_app
from app.mcp import MCPServerConfig, MCPServerInfo
from app.models import LLMResponse, Message, MessageRole, ToolSpec
from app.tools import ToolRegistry
from app.user_execution import effective_execution_catalog, validate_user_execution_config


class RecordingLLMAdapter(LLMAdapter):
    def __init__(self) -> None:
        self.requests: list[list[Message]] = []
        self.tool_requests: list[list[ToolSpec]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        _ = tools
        self.requests.append(list(messages))
        self.tool_requests.append(list(tools))
        return LLMResponse(content="personal skill reply")

    def describe(self) -> dict[str, object]:
        return {"provider": "recording"}


def _runtime_app(
    store: SQLiteTenantConfigStore,
    adapter: RecordingLLMAdapter,
):
    config = parse_tenant_execution_config(
        "tenant-1",
        {
            "llm": {"provider": "mock"},
            "skills": {
                "items": [
                    {
                        "name": "shared-style",
                        "description": "Shared conventions",
                        "system_prompt": "Shared skill instructions.",
                    }
                ]
            },
        },
    )
    resolver = FixedTenantExecutionResolver(
        adapter,
        ToolRegistry(),
        config=config,
    )
    return create_app(execution_resolver=resolver, admin_store=store)


def _personal_skill_payload(prompt: str) -> dict[str, object]:
    return {
        "skills": {
            "items": [
                {
                    "id": "user:python-style",
                    "name": "python-style",
                    "system_prompt": prompt,
                }
            ]
        },
        "capability_profiles": {
            "items": [
                {
                    "id": "user:personal-tools",
                    "name": "personal-tools",
                    "allowed_local_tools": ["echo"],
                }
            ]
        },
        "agents": {
            "items": [
                {
                    "id": "user:personal-agent",
                    "name": "personal-agent",
                    "skill_refs": ["shared:shared-style", "user:python-style"],
                    "capability_profile_ref": "user:personal-tools",
                }
            ]
        },
        "defaults": {"agent_ref": "user:personal-agent"},
    }


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


def test_user_execution_credential_store_is_encrypted_scoped_and_versioned(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "admin.db"
    store = SQLiteTenantConfigStore(str(db_path), encryption_key="credential-test-key")

    created = store.upsert_user_execution_credential(
        "tenant-1",
        "user-1",
        "oauth:linear",
        header_name="Authorization",
        header_value="Bearer top-secret-token",
        expected_version=0,
    )
    updated = store.upsert_user_execution_credential(
        "tenant-1",
        "user-1",
        "oauth:linear",
        header_name="Authorization",
        header_value="Bearer rotated-token",
        expected_version=created.version,
    )

    assert created.version == 1
    assert updated.version == 2
    assert updated.header_value == "Bearer rotated-token"
    assert store.get_user_execution_credential("tenant-1", "user-2", "oauth:linear") is None
    assert store.get_user_execution_credential("tenant-2", "user-1", "oauth:linear") is None
    metadata = store.list_user_execution_credentials("tenant-1", "user-1")
    assert [(item.credential_ref, item.header_name, item.version) for item in metadata] == [
        ("oauth:linear", "Authorization", 2)
    ]

    with sqlite3.connect(db_path) as connection:
        ciphertext = bytes(
            connection.execute("SELECT value_cipher FROM user_execution_credentials").fetchone()[0]
        )
    assert b"top-secret-token" not in ciphertext
    assert b"rotated-token" not in ciphertext

    try:
        store.upsert_user_execution_credential(
            "tenant-1",
            "user-1",
            "oauth:linear",
            header_name="Authorization",
            header_value="Bearer stale",
            expected_version=1,
        )
    except UserExecutionCredentialConflictError as exc:
        assert exc.actual_version == 2
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected a credential version conflict")


def test_user_execution_credential_store_requires_encryption(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))

    try:
        store.upsert_user_execution_credential(
            "tenant-1",
            "user-1",
            "oauth:linear",
            header_name="Authorization",
            header_value="Bearer secret",
        )
    except RuntimeError as exc:
        assert "encryption is required" in str(exc).lower()
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected encrypted credential storage to be required")


def test_user_execution_credential_api_is_write_only_and_principal_scoped(
    tmp_path: Path,
) -> None:
    store = SQLiteTenantConfigStore(
        str(tmp_path / "admin.db"), encryption_key="credential-test-key"
    )
    app = create_app(admin_store=store)

    with TestClient(app) as client:
        created = client.put(
            "/me/execution-credentials/oauth:linear",
            headers=AUTH_HEADERS,
            json={
                "header_name": "Authorization",
                "header_value": "Bearer api-only-secret",
                "expected_version": 0,
            },
        )
        listed = client.get("/me/execution-credentials", headers=AUTH_HEADERS)
        other_user = client.get("/me/execution-credentials", headers=OTHER_USER_HEADERS)
        conflict = client.put(
            "/me/execution-credentials/oauth:linear",
            headers=AUTH_HEADERS,
            json={
                "header_name": "Authorization",
                "header_value": "Bearer stale",
                "expected_version": 0,
            },
        )
        deleted = client.delete(
            "/me/execution-credentials/oauth:linear?expected_version=1",
            headers=AUTH_HEADERS,
        )

    assert created.status_code == 200
    assert created.json()["credential_ref"] == "oauth:linear"
    assert created.json()["version"] == 1
    assert "api-only-secret" not in created.text
    assert "header_value" not in created.text
    assert listed.status_code == 200
    assert listed.json()["items"][0]["credential_ref"] == "oauth:linear"
    assert "api-only-secret" not in listed.text
    assert other_user.json() == {"items": []}
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["actual_version"] == 1
    assert deleted.status_code == 204


def test_user_execution_credential_api_requires_encryption(tmp_path: Path) -> None:
    app = create_app(admin_store=SQLiteTenantConfigStore(str(tmp_path / "admin.db")))

    with TestClient(app) as client:
        response = client.put(
            "/me/execution-credentials/oauth:linear",
            headers=AUTH_HEADERS,
            json={
                "header_name": "Authorization",
                "header_value": "Bearer secret",
            },
        )

    assert response.status_code == 503
    assert "Encrypted user execution credential storage" in response.json()["detail"]


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


def test_user_resource_api_supports_granular_skill_crud_and_conflicts(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    app = create_app(admin_store=store)
    skill = {
        "id": "user:reviewer",
        "name": "reviewer",
        "description": "Review changes",
        "system_prompt": "Review code carefully.",
    }

    with TestClient(app) as client:
        created = client.put(
            "/me/skills/user:reviewer",
            headers=AUTH_HEADERS,
            json={"resource": skill, "expected_version": 0},
        )
        listed = client.get("/me/skills", headers=AUTH_HEADERS)
        fetched = client.get("/me/skills/user:reviewer", headers=AUTH_HEADERS)
        updated = client.put(
            "/me/skills/user:reviewer",
            headers=AUTH_HEADERS,
            json={
                "resource": {**skill, "description": "Review every change"},
                "expected_version": 1,
            },
        )
        conflict = client.put(
            "/me/skills/user:reviewer",
            headers=AUTH_HEADERS,
            json={"resource": skill, "expected_version": 1},
        )
        other_user = client.get("/me/skills", headers=OTHER_USER_HEADERS)
        deleted = client.delete(
            "/me/skills/user:reviewer?expected_version=2",
            headers=AUTH_HEADERS,
        )
        missing = client.get("/me/skills/user:reviewer", headers=AUTH_HEADERS)

    assert created.status_code == 200
    assert created.json()["resource"]["id"] == "user:reviewer"
    assert created.json()["version"] == 1
    assert listed.json() == {"items": [skill], "version": 1}
    assert fetched.json()["resource"] == skill
    assert updated.status_code == 200
    assert updated.json()["resource"]["description"] == "Review every change"
    assert updated.json()["version"] == 2
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["actual_version"] == 2
    assert other_user.json() == {"items": [], "version": None}
    assert deleted.status_code == 204
    assert missing.status_code == 404


def test_user_resource_api_rejects_url_id_mismatch(tmp_path: Path) -> None:
    app = create_app(admin_store=SQLiteTenantConfigStore(str(tmp_path / "admin.db")))

    with TestClient(app) as client:
        response = client.put(
            "/me/skills/user:reviewer",
            headers=AUTH_HEADERS,
            json={
                "resource": {
                    "id": "user:other",
                    "name": "reviewer",
                    "system_prompt": "Review code.",
                },
                "expected_version": 0,
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "Resource id must match the URL"


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


def test_personal_skills_and_agents_are_principal_scoped_execution_options(
    tmp_path: Path,
) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        _personal_skill_payload("Personal skill instructions."),
    )
    app = _runtime_app(store, RecordingLLMAdapter())

    with TestClient(app) as client:
        owner_options = client.get("/execution-options", headers=AUTH_HEADERS)
        other_options = client.get("/execution-options", headers=OTHER_USER_HEADERS)

    assert owner_options.status_code == 200
    assert owner_options.json()["skills"]["default"] is None
    assert {item["id"] for item in owner_options.json()["skills"]["items"]} == {
        "shared:shared-style",
        "user:python-style",
    }
    personal_skill = next(
        item
        for item in owner_options.json()["skills"]["items"]
        if item["id"] == "user:python-style"
    )
    assert personal_skill == {
        "name": "user:python-style",
        "description": None,
        "id": "user:python-style",
        "display_name": "python-style",
        "source": "user",
        "version": 1,
    }
    assert owner_options.json()["agents"]["default"] == "user:personal-agent"
    assert "user:python-style" not in {
        item["id"] for item in other_options.json()["skills"]["items"]
    }


def test_personal_agent_default_materializes_and_skill_updates_resolve_live(
    tmp_path: Path,
) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    created_config = store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        _personal_skill_payload("Initial personal instructions."),
    )
    adapter = RecordingLLMAdapter()
    app = _runtime_app(store, adapter)

    with TestClient(app) as client:
        created = client.post("/threads", headers=AUTH_HEADERS)
        assert created.status_code == 200
        thread_id = created.json()["thread_id"]
        listed = client.get("/threads", headers=AUTH_HEADERS)
        assert listed.json()["threads"][0]["skill_names"] == [
            "shared-style",
            "user:python-style",
        ]
        assert listed.json()["threads"][0]["capability_profile"] == "user:personal-tools"

        store.upsert_user_execution_config(
            "tenant-1",
            "user-1",
            _personal_skill_payload("Updated personal instructions."),
            expected_version=created_config.version,
        )
        message = client.post(
            f"/threads/{thread_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Use my preferences."},
        )
        assert message.status_code == 200
        run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run.status_code == 200
    assert adapter.requests
    system_messages = [
        message.content for message in adapter.requests[-1] if message.role == MessageRole.SYSTEM
    ]
    assert any("Shared skill instructions." in content for content in system_messages)
    assert any("Updated personal instructions." in content for content in system_messages)
    assert all("Initial personal instructions." not in content for content in system_messages)


def test_personal_skill_thread_cannot_run_as_another_user(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        _personal_skill_payload("Personal instructions."),
    )
    app = _runtime_app(store, RecordingLLMAdapter())

    with TestClient(app) as client:
        created = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"skill_name": "user:python-style"},
        )
        thread_id = created.json()["thread_id"]
        other_create = client.post(
            "/threads",
            headers=OTHER_USER_HEADERS,
            json={"skill_name": "user:python-style"},
        )
        other_run = client.post(
            f"/threads/{thread_id}/run",
            headers=OTHER_USER_HEADERS,
        )

    assert created.status_code == 200
    assert other_create.status_code == 400
    assert "Unknown skill" in other_create.json()["detail"]
    assert other_run.status_code == 403
    assert other_run.json()["detail"] == "Personal execution resources belong to a different user"


def test_deleted_personal_skill_fails_existing_thread_without_fallback(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    config = store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        _personal_skill_payload("Personal instructions."),
    )
    app = _runtime_app(store, RecordingLLMAdapter())

    with TestClient(app) as client:
        created = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"skill_name": "user:python-style"},
        )
        thread_id = created.json()["thread_id"]
        store.upsert_user_execution_config(
            "tenant-1",
            "user-1",
            {},
            expected_version=config.version,
        )
        run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run.status_code == 409
    assert "Unknown skill 'user:python-style'" in run.json()["detail"]


def test_personal_capability_profile_narrows_runtime_tools(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        {
            "capability_profiles": {
                "items": [
                    {
                        "id": "user:tools",
                        "name": "tools",
                        "allowed_local_tools": ["echo"],
                    }
                ]
            }
        },
    )
    adapter = RecordingLLMAdapter()
    app = _runtime_app(store, adapter)

    with TestClient(app) as client:
        response = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"capability_profile": "user:tools"},
        )
        assert response.status_code == 200
        thread_id = response.json()["thread_id"]
        message = client.post(
            f"/threads/{thread_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Use an allowed tool."},
        )
        assert message.status_code == 200
        run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run.status_code == 200
    assert adapter.tool_requests
    assert {tool.name for tool in adapter.tool_requests[-1]} == {"echo"}


def test_personal_capability_profile_cannot_broaden_tenant_local_tools(
    tmp_path: Path,
) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        {
            "capability_profiles": {
                "items": [
                    {
                        "id": "user:tools",
                        "name": "tools",
                        "allowed_local_tools": ["retrieve_knowledge"],
                    }
                ]
            }
        },
    )
    app = _runtime_app(store, RecordingLLMAdapter())

    with TestClient(app) as client:
        response = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"capability_profile": "user:tools"},
        )

    assert response.status_code == 400
    assert "tenant-unavailable local tools: retrieve_knowledge" in response.json()["detail"]


def test_personal_capability_profile_resolves_tenant_shared_mcp_servers(
    tmp_path: Path,
) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        {
            "capability_profiles": {
                "items": [
                    {
                        "id": "user:docs",
                        "name": "docs",
                        "mcp_server_refs": ["shared:docs"],
                    }
                ]
            }
        },
    )
    tenant_config = parse_tenant_execution_config(
        "tenant-1",
        {
            "tools": {
                "mcp_servers": [
                    {"name": "docs", "url": "https://docs.example/mcp"},
                    {"name": "other", "url": "https://other.example/mcp"},
                ]
            }
        },
    )
    catalog = effective_execution_catalog(
        tenant_config,
        store,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    capability = catalog.resolve_capability_profile("user:docs", use_default=False)

    assert capability is not None
    constraints = catalog.personal_capability_constraints(capability)
    assert constraints.allowed_local_tools is None
    assert constraints.shared_mcp_server_names == {"docs"}


def test_unauthenticated_personal_mcp_server_runs_when_tenant_allows_custom_servers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeMCPClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self._config = config

        async def list_tools(self) -> list[ToolSpec]:
            return [
                ToolSpec(
                    name=f"{self._config.name}.ping",
                    description="Ping personal server",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            return {"tool": tool_name, "arguments": arguments}

        def server_info(self) -> MCPServerInfo:
            return MCPServerInfo(
                name=self._config.name,
                url=self._config.url,
                protocol_version=self._config.protocol_version,
                session_id="personal-session",
                server_name="personal-server",
                server_version="1.0",
            )

    monkeypatch.setattr("app.tools.MCPHTTPClient", FakeMCPClient)
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        {
            "mcp_servers": {
                "items": [
                    {
                        "id": "user:third-party",
                        "name": "third-party",
                        "url": "https://1.1.1.1/mcp",
                    }
                ]
            },
            "capability_profiles": {
                "items": [
                    {
                        "id": "user:tools",
                        "name": "tools",
                        "mcp_server_refs": ["user:third-party"],
                    }
                ]
            },
        },
    )
    adapter = RecordingLLMAdapter()
    app = _runtime_app(store, adapter)

    with TestClient(app) as client:
        created = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"capability_profile": "user:tools"},
        )
        assert created.status_code == 200
        thread_id = created.json()["thread_id"]
        message = client.post(
            f"/threads/{thread_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Use the personal service."},
        )
        assert message.status_code == 200
        run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run.status_code == 200
    assert adapter.tool_requests
    assert "user:third-party.ping" in {tool.name for tool in adapter.tool_requests[-1]}


def test_tenant_policy_can_block_personal_mcp_servers(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_tenant_mcp_server_catalog_policy(
        "tenant-1",
        item_ids=[],
        allow_custom_mcp_servers=False,
        require_subject_assignment=False,
        updated_by="admin",
    )
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        {
            "mcp_servers": {
                "items": [
                    {
                        "id": "user:third-party",
                        "name": "third-party",
                        "url": "https://1.1.1.1/mcp",
                    }
                ]
            },
            "capability_profiles": {
                "items": [
                    {
                        "id": "user:tools",
                        "name": "tools",
                        "mcp_server_refs": ["user:third-party"],
                    }
                ]
            },
        },
    )
    app = _runtime_app(store, RecordingLLMAdapter())

    with TestClient(app) as client:
        response = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"capability_profile": "user:tools"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Tenant policy does not allow user-owned MCP servers"


def test_personal_mcp_server_rejects_private_network_targets(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        {
            "mcp_servers": {
                "items": [
                    {
                        "id": "user:internal",
                        "name": "internal",
                        "url": "https://127.0.0.1/mcp",
                    }
                ]
            },
            "capability_profiles": {
                "items": [
                    {
                        "id": "user:tools",
                        "name": "tools",
                        "mcp_server_refs": ["user:internal"],
                    }
                ]
            },
        },
    )
    app = _runtime_app(store, RecordingLLMAdapter())

    with TestClient(app) as client:
        response = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"capability_profile": "user:tools"},
        )

    assert response.status_code == 400
    assert "cannot access local or private network hosts" in response.json()["detail"]


def test_encrypted_personal_mcp_credential_is_resolved_live_at_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen_headers: list[dict[str, str]] = []

    class FakeMCPClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self._config = config

        async def list_tools(self) -> list[ToolSpec]:
            seen_headers.append(dict(self._config.headers))
            return [
                ToolSpec(
                    name=f"{self._config.name}.ping",
                    description="Ping credential-backed server",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            return {"tool": tool_name, "arguments": arguments}

        def server_info(self) -> MCPServerInfo:
            return MCPServerInfo(
                name=self._config.name,
                url=self._config.url,
                protocol_version=self._config.protocol_version,
                session_id="credential-session",
                server_name="credential-server",
                server_version="1.0",
            )

    monkeypatch.setattr("app.tools.MCPHTTPClient", FakeMCPClient)
    store = SQLiteTenantConfigStore(
        str(tmp_path / "admin.db"), encryption_key="credential-test-key"
    )
    initial = store.upsert_user_execution_credential(
        "tenant-1",
        "user-1",
        "oauth:third-party",
        header_name="Authorization",
        header_value="Bearer initial-secret",
        expected_version=0,
    )
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        {
            "mcp_servers": {
                "items": [
                    {
                        "id": "user:third-party",
                        "name": "third-party",
                        "url": "https://1.1.1.1/mcp",
                        "credential_ref": "oauth:third-party",
                    }
                ]
            },
            "capability_profiles": {
                "items": [
                    {
                        "id": "user:tools",
                        "name": "tools",
                        "mcp_server_refs": ["user:third-party"],
                    }
                ]
            },
        },
    )
    adapter = RecordingLLMAdapter()
    app = _runtime_app(store, adapter)

    with TestClient(app) as client:
        created = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"capability_profile": "user:tools"},
        )
        assert created.status_code == 200
        thread_id = created.json()["thread_id"]
        store.upsert_user_execution_credential(
            "tenant-1",
            "user-1",
            "oauth:third-party",
            header_name="Authorization",
            header_value="Bearer rotated-secret",
            expected_version=initial.version,
        )
        message = client.post(
            f"/threads/{thread_id}/messages",
            headers=AUTH_HEADERS,
            json={"content": "Use the authenticated service."},
        )
        assert message.status_code == 200
        run = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)

    assert run.status_code == 200
    assert seen_headers
    assert seen_headers[-1]["Authorization"] == "Bearer rotated-secret"
    assert "initial-secret" not in run.text
    assert "rotated-secret" not in run.text


def test_personal_mcp_server_reports_missing_credential(tmp_path: Path) -> None:
    store = SQLiteTenantConfigStore(str(tmp_path / "admin.db"))
    store.upsert_user_execution_config(
        "tenant-1",
        "user-1",
        {
            "mcp_servers": {
                "items": [
                    {
                        "id": "user:third-party",
                        "name": "third-party",
                        "url": "https://tools.example/mcp",
                        "credential_ref": "oauth:third-party",
                    }
                ]
            },
            "capability_profiles": {
                "items": [
                    {
                        "id": "user:tools",
                        "name": "tools",
                        "mcp_server_refs": ["user:third-party"],
                    }
                ]
            },
        },
    )
    app = _runtime_app(store, RecordingLLMAdapter())

    with TestClient(app) as client:
        response = client.post(
            "/threads",
            headers=AUTH_HEADERS,
            json={"capability_profile": "user:tools"},
        )

    assert response.status_code == 400
    assert "credential 'oauth:third-party' was not found" in response.json()["detail"]


def test_user_execution_config_storage_endpoint_requires_configured_store() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/me/execution-config", headers=AUTH_HEADERS)

    assert response.status_code == 503
