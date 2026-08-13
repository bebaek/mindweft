from __future__ import annotations

import json
from pathlib import Path

from minigent_workspace import tenant_config


def test_apply_tenant_runtime_environment_builds_default_config(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    spec = tenant_config.CodingMCPServerSpec(
        name="fs-workspace",
        url="http://127.0.0.1:8765/mcp",
    )

    tenant_config.apply_tenant_runtime_environment(
        env,
        "demo-tenant",
        [spec],
        workspace_roots=[tmp_path],
        workspace_scope="default",
    )

    assert env["MINIGENT_AUTH_MODE"] == "dev-headers"
    assert env["MINIGENT_LLM_PROVIDER"] == "mock"
    assert env["MINIGENT_CODING_TENANT_ID"] == "demo-tenant"
    assert env["MINIGENT_CODING_OAUTH_GLOBAL_FALLBACK"] == "true"
    tenant = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])["demo-tenant"]
    assert tenant["tools"]["mcp_servers"][0]["name"] == "fs-workspace"
    assert tenant["skills"]["items"][0]["workspace_scope"] == "default"
    assert str(tmp_path) in tenant["skills"]["items"][0]["system_prompt"]


def test_apply_tenant_runtime_environment_injects_servers_and_skill(tmp_path: Path) -> None:
    env = {
        "MINIGENT_AUTH_MODE": "static",
        "MINIGENT_LLM_PROVIDER": "openai",
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(
            {
                "demo-tenant": {
                    "tools": {"mcp_servers": []},
                    "skills": {
                        "default_skill": "existing",
                        "items": [{"name": "existing", "system_prompt": "Existing prompt."}],
                    },
                }
            }
        ),
    }
    spec = tenant_config.CodingMCPServerSpec(
        name="fs-workspace",
        url="http://127.0.0.1:8765/mcp",
    )

    tenant_config.apply_tenant_runtime_environment(
        env,
        "demo-tenant",
        [spec],
        workspace_roots=[tmp_path],
        workspace_scope=None,
    )

    assert env["MINIGENT_AUTH_MODE"] == "static"
    assert env["MINIGENT_LLM_PROVIDER"] == "openai"
    tenant = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])["demo-tenant"]
    assert tenant["tools"]["mcp_servers"][0]["name"] == "fs-workspace"
    assert tenant["skills"]["items"][1]["name"] == "coding-workspace"


def test_apply_tenant_runtime_environment_can_skip_skill_injection(tmp_path: Path) -> None:
    env = {
        "MINIGENT_CODING_INJECT_WORKSPACE_SKILL": "false",
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(
            {
                "demo-tenant": {
                    "tools": {"mcp_servers": []},
                    "skills": {"default_skill": "existing", "items": []},
                }
            }
        ),
    }

    tenant_config.apply_tenant_runtime_environment(
        env,
        "demo-tenant",
        [],
        workspace_roots=[tmp_path],
        workspace_scope=None,
    )

    tenant = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])["demo-tenant"]
    assert tenant["skills"] == {"default_skill": "existing", "items": []}


def test_default_tenant_config_adds_text_server_to_inspect_profile() -> None:
    config = tenant_config.default_tenant_config(
        "demo-tenant",
        "fs-workspace",
        "http://127.0.0.1:8765/mcp",
        text_enabled=True,
        text_bridge_name="text-workspace",
        text_bridge_url="http://127.0.0.1:8767/mcp",
    )

    tenant = config["demo-tenant"]
    assert tenant["tools"]["mcp_servers"][1]["name"] == "text-workspace"
    assert tenant["tools"]["mcp_servers"][1]["allowed_tools"] == [
        "read_text_file_lines",
        "read_text_file_around",
        "search_text_file",
    ]
    assert tenant["capability_profiles"]["items"][0]["mcp_server_names"] == [
        "fs-workspace",
        "text-workspace",
    ]


def test_default_tenant_config_adds_shell_test_profile_when_enabled() -> None:
    config = tenant_config.default_tenant_config(
        "demo-tenant",
        "fs-workspace",
        "http://127.0.0.1:8765/mcp",
        shell_enabled=True,
        shell_bridge_name="shell-workspace",
        shell_bridge_url="http://127.0.0.1:8766/mcp",
    )

    tenant = config["demo-tenant"]
    assert tenant["tools"]["mcp_servers"][1] == {
        "name": "shell-workspace",
        "url": "http://127.0.0.1:8766/mcp",
        "headers": {},
        "allowed_tools": ["run_command"],
    }
    assert tenant["capability_profiles"]["default_profile"] == "inspect"
    assert tenant["capability_profiles"]["items"][1] == {
        "name": "test",
        "allowed_local_tools": ["current_time", "calculator"],
        "mcp_server_names": ["fs-workspace", "shell-workspace"],
    }


def test_coding_workspace_skill_lists_multiple_workspace_roots(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"

    skill = tenant_config.coding_workspace_skill(workspace_roots=[one, two])

    assert "Configured workspace roots:" in skill["system_prompt"]
    assert str(one) in skill["system_prompt"]
    assert str(two) in skill["system_prompt"]


def test_coding_workspace_skill_includes_active_scope(tmp_path: Path) -> None:
    skill = tenant_config.coding_workspace_skill(workspace_roots=[tmp_path], workspace_scope="repo")

    assert skill["workspace_scope"] == "repo"
    assert "Active workspace scope: repo" in skill["system_prompt"]
    assert "Stay within these roots" in skill["system_prompt"]


def test_inject_coding_workspace_skill_enriches_existing_skill_with_workspaces(
    tmp_path: Path,
) -> None:
    config = json.dumps(
        {
            "demo-tenant": {
                "skills": {
                    "items": [{"name": "coding-workspace", "system_prompt": "Base coding prompt."}]
                }
            }
        }
    )

    injected = tenant_config.inject_coding_workspace_skill(
        config,
        "demo-tenant",
        workspace_roots=[tmp_path, tmp_path / "other"],
    )

    skill = json.loads(injected)["demo-tenant"]["skills"]["items"][0]
    assert skill["system_prompt"].startswith("Base coding prompt.")
    assert "Configured workspace roots:" in skill["system_prompt"]
    assert str(tmp_path) in skill["system_prompt"]
    assert str(tmp_path / "other") in skill["system_prompt"]


def test_tenant_gateway_mcp_server_mismatches_detects_missing_stdio_spec() -> None:
    env = {
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(
            {
                "demo-tenant": {
                    "tools": {
                        "mcp_servers": [
                            {
                                "name": "fs-workspace",
                                "url": "http://127.0.0.1:8765/mcp/fs-workspace",
                            },
                            {
                                "name": "text-workspace",
                                "url": "http://127.0.0.1:8765/mcp/text-workspace",
                            },
                            {
                                "name": "web-search",
                                "url": "http://127.0.0.1:8766/mcp",
                            },
                        ]
                    }
                }
            }
        )
    }
    specs = [
        tenant_config.CodingMCPServerSpec(
            name="fs-workspace",
            url="http://127.0.0.1:8765/mcp/fs-workspace",
            transport="stdio",
            command=["fs-server"],
        ),
        tenant_config.CodingMCPServerSpec(
            name="web-search",
            url="http://127.0.0.1:8766/mcp",
            transport="http",
        ),
    ]

    assert tenant_config.tenant_gateway_mcp_server_mismatches(
        env,
        "demo-tenant",
        gateway_url_prefix="http://127.0.0.1:8765/mcp",
        specs=specs,
    ) == ["text-workspace"]


def test_tenant_gateway_mcp_server_mismatches_accepts_loaded_stdio_specs() -> None:
    env = {
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(
            {
                "demo-tenant": {
                    "tools": {
                        "mcp_servers": [
                            {
                                "name": "text-workspace",
                                "url": "http://127.0.0.1:8765/mcp/text-workspace",
                            }
                        ]
                    }
                }
            }
        )
    }
    specs = [
        tenant_config.CodingMCPServerSpec(
            name="text-workspace",
            url="http://127.0.0.1:8765/mcp/text-workspace",
            transport="stdio",
            command=["text-server"],
        )
    ]

    assert (
        tenant_config.tenant_gateway_mcp_server_mismatches(
            env,
            "demo-tenant",
            gateway_url_prefix="http://127.0.0.1:8765/mcp",
            specs=specs,
        )
        == []
    )


def test_bridge_allowed_tools_allows_unfiltered_explicit_null() -> None:
    env = {
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(
            {
                "demo-tenant": {
                    "tools": {
                        "mcp_servers": [
                            {
                                "name": "fs-workspace",
                                "url": "http://127.0.0.1:8765/mcp",
                                "allowed_tools": None,
                            }
                        ]
                    }
                }
            }
        )
    }

    assert tenant_config.bridge_allowed_tools_from_config(env, "demo-tenant", "fs-workspace") == []


def test_tenant_mcp_server_from_spec_preserves_headers() -> None:
    spec = tenant_config.CodingMCPServerSpec(
        name="remote-tools",
        url="https://example.com/mcp",
        transport="http",
        headers={"Authorization": "Bearer token"},
        allowed_tools=["search"],
        timeout_seconds=50,
    )

    assert tenant_config.tenant_mcp_server_from_spec(spec) == {
        "name": "remote-tools",
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer token"},
        "timeout_seconds": 50,
        "allowed_tools": ["search"],
    }


def test_default_tenant_config_from_servers_builds_profiles() -> None:
    specs = [
        tenant_config.CodingMCPServerSpec(
            name="fs-workspace",
            url="http://127.0.0.1:8765/mcp",
            command=["fs-server"],
            profiles=["inspect", "edit"],
            allowed_tools=["read_file"],
        ),
        tenant_config.CodingMCPServerSpec(
            name="git-workspace",
            url="http://127.0.0.1:8770/mcp",
            command=["git-server"],
            profiles=["test"],
            allowed_tools=["git_status"],
        ),
    ]

    config = tenant_config.default_tenant_config_from_servers("demo-tenant", specs)

    tenant = config["demo-tenant"]
    assert "llm" not in tenant
    assert tenant["tools"]["mcp_servers"] == [
        {
            "name": "fs-workspace",
            "url": "http://127.0.0.1:8765/mcp",
            "headers": {},
            "timeout_seconds": 30.0,
            "allowed_tools": ["read_file"],
        },
        {
            "name": "git-workspace",
            "url": "http://127.0.0.1:8770/mcp",
            "headers": {},
            "timeout_seconds": 30.0,
            "allowed_tools": ["git_status"],
        },
    ]
    assert tenant["capability_profiles"]["items"] == [
        {
            "name": "inspect",
            "allowed_local_tools": ["current_time", "calculator"],
            "mcp_server_names": ["fs-workspace"],
        },
        {
            "name": "edit",
            "allowed_local_tools": ["current_time", "calculator"],
            "mcp_server_names": ["fs-workspace"],
        },
        {
            "name": "test",
            "allowed_local_tools": ["current_time", "calculator"],
            "mcp_server_names": ["git-workspace"],
        },
    ]


def test_inject_coding_mcp_servers_synthesizes_tenant_projection() -> None:
    raw_config = json.dumps(
        {
            "demo-tenant": {
                "tools": {
                    "allowed_local_tools": ["calculator"],
                    "mcp_servers": [
                        {
                            "name": "fs-workspace",
                            "url": "http://old.example/mcp",
                            "allowed_tools": ["read_file", "write_file"],
                        }
                    ],
                }
            }
        }
    )
    specs = [
        tenant_config.CodingMCPServerSpec(
            name="fs-workspace",
            url="http://127.0.0.1:8765/mcp/fs-workspace",
            allowed_tools=["write_file", "edit_file"],
            path_policy={"deny_globs": ["**/.env*"]},
        ),
        tenant_config.CodingMCPServerSpec(
            name="text-workspace",
            url="http://127.0.0.1:8765/mcp/text-workspace",
            allowed_tools=["read_text_file_lines"],
        ),
    ]

    injected = json.loads(tenant_config.inject_coding_mcp_servers(raw_config, "demo-tenant", specs))

    servers = injected["demo-tenant"]["tools"]["mcp_servers"]
    assert servers[0] == {
        "name": "fs-workspace",
        "url": "http://127.0.0.1:8765/mcp/fs-workspace",
        "headers": {},
        "timeout_seconds": 30.0,
        "allowed_tools": ["write_file"],
        "path_policy": {"deny_globs": ["**/.env*"]},
    }
    assert servers[1]["name"] == "text-workspace"
    assert servers[1]["allowed_tools"] == ["read_text_file_lines"]
