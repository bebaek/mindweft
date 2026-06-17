from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

from app import coding_workspace_runner as runner


def test_console_script_entry_point_loads_runner_main() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(script for script in scripts if script.name == "minigent-coding-workspace")

    loaded = entry_point.load()
    assert loaded.__module__ == "app.coding_workspace_runner"
    assert loaded.__name__ == "main"


def test_coding_config_export_builds_client_argv(monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_BASE_URL", raising=False)
    args = runner.parse_config_args(
        [
            "config",
            "export",
            "--env-file",
            ".env.test",
            "--base-url",
            "http://127.0.0.1:9000",
            "--output",
            "export.toml",
            "--include-runtime",
        ]
    )

    assert runner.build_coding_config_export_client_argv(args) == [
        "--env-file",
        ".env.test",
        "--base-url",
        "http://127.0.0.1:9000",
        "config",
        "export",
        "--local-coding",
        "--coding-env-file",
        ".env.test",
        "--output",
        "export.toml",
        "--include-runtime",
    ]


def test_coding_config_export_uses_env_base_url(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_BASE_URL", "http://127.0.0.1:9100")
    args = runner.parse_config_args(["config", "export", "--env-file", ".env.coding"])

    assert runner.build_coding_config_export_client_argv(args) == [
        "--env-file",
        ".env.coding",
        "--base-url",
        "http://127.0.0.1:9100",
        "config",
        "export",
        "--local-coding",
        "--coding-env-file",
        ".env.coding",
    ]

def test_load_env_file_reads_file_backed_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_TENANT_EXECUTION_CONFIGS", raising=False)
    config_path = tmp_path / "tenant-config.json"
    config_path.write_text('{"demo-tenant":{"llm":{"provider":"mock"}}}\n', encoding="utf-8")
    env_path = tmp_path / ".env.coding"
    env_path.write_text(
        "MINIGENT_TENANT_EXECUTION_CONFIGS_FILE=tenant-config.json\n", encoding="utf-8"
    )

    env = runner.load_env_file(str(env_path))

    assert env["MINIGENT_TENANT_EXECUTION_CONFIGS"] == (
        '{"demo-tenant":{"llm":{"provider":"mock"}}}'
    )


def test_bridge_command_uses_default_read_only_tools(tmp_path: Path) -> None:
    command = runner.build_bridge_command(
        {}, "demo-tenant", "fs-workspace", "127.0.0.1", 8765, tmp_path
    )

    allowed_tool_indexes = [
        index for index, value in enumerate(command) if value == "--allowed-tool"
    ]
    assert [command[index + 1] for index in allowed_tool_indexes] == [
        "list_allowed_directories",
        "list_directory",
        "read_file",
    ]


def test_bridge_command_mirrors_tenant_config_path_globs(tmp_path: Path) -> None:
    env = {
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(
            {
                "demo-tenant": {
                    "tools": {
                        "mcp_servers": [
                            {
                                "name": "fs-workspace",
                                "url": "http://127.0.0.1:8765/mcp",
                                "path_policy": {
                                    "deny_globs": ["**/.env*", "**/.git/**"],
                                    "allow_globs": [
                                        "**/.env*.template",
                                        "**/.env*.driver.sh",
                                    ],
                                },
                            }
                        ]
                    }
                }
            }
        )
    }

    command = runner.build_bridge_command(
        env, "demo-tenant", "fs-workspace", "127.0.0.1", 8765, tmp_path
    )

    deny_indexes = [index for index, value in enumerate(command) if value == "--deny-glob"]
    allow_indexes = [index for index, value in enumerate(command) if value == "--allow-glob"]
    assert [command[index + 1] for index in deny_indexes] == ["**/.env*", "**/.git/**"]
    assert [command[index + 1] for index in allow_indexes] == [
        "**/.env*.template",
        "**/.env*.driver.sh",
    ]


def test_bridge_command_uses_env_configured_path_globs(tmp_path: Path) -> None:
    command = runner.build_bridge_command(
        {
            "MINIGENT_CODING_BRIDGE_DENY_GLOBS": "**/.env*, **/.git/**",
            "MINIGENT_CODING_BRIDGE_ALLOW_GLOBS": "**/.env*.template, **/.env*.driver.sh",
        },
        "demo-tenant",
        "fs-workspace",
        "127.0.0.1",
        8765,
        tmp_path,
    )

    deny_indexes = [index for index, value in enumerate(command) if value == "--deny-glob"]
    allow_indexes = [index for index, value in enumerate(command) if value == "--allow-glob"]
    assert [command[index + 1] for index in deny_indexes] == ["**/.env*", "**/.git/**"]
    assert [command[index + 1] for index in allow_indexes] == [
        "**/.env*.template",
        "**/.env*.driver.sh",
    ]


def test_bridge_command_uses_tenant_config_allowed_tools(tmp_path: Path) -> None:
    env = {
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(
            {
                "demo-tenant": {
                    "tools": {
                        "mcp_servers": [
                            {
                                "name": "fs-workspace",
                                "url": "http://127.0.0.1:8765/mcp",
                                "allowed_tools": [
                                    "list_allowed_directories",
                                    "list_directory",
                                    "read_file",
                                    "read_multiple_files",
                                    "write_file",
                                    "edit_file",
                                    "create_directory",
                                    "directory_tree",
                                    "move_file",
                                    "search_files",
                                    "get_file_info",
                                ],
                            }
                        ]
                    }
                }
            }
        )
    }

    command = runner.build_bridge_command(
        env, "demo-tenant", "fs-workspace", "127.0.0.1", 8765, tmp_path
    )

    allowed_tool_indexes = [
        index for index, value in enumerate(command) if value == "--allowed-tool"
    ]
    assert [command[index + 1] for index in allowed_tool_indexes] == [
        "list_allowed_directories",
        "list_directory",
        "read_file",
        "read_multiple_files",
        "write_file",
        "edit_file",
        "create_directory",
        "directory_tree",
        "move_file",
        "search_files",
        "get_file_info",
    ]


def test_default_tenant_config_adds_text_server_to_inspect_profile() -> None:
    config = runner.default_tenant_config(
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


def test_text_bridge_command_runs_text_mcp_server(tmp_path: Path) -> None:
    command = runner.build_text_bridge_command("text-workspace", "127.0.0.1", 8767, tmp_path)

    allowed_tool_indexes = [
        index for index, value in enumerate(command) if value == "--allowed-tool"
    ]
    assert [command[index + 1] for index in allowed_tool_indexes] == [
        "read_text_file_lines",
        "read_text_file_around",
        "search_text_file",
    ]
    assert "from app.text_mcp_server import main; raise SystemExit(main())" in command
    assert command[-2:] == ["--workspace", str(tmp_path)]


def test_default_tenant_config_adds_shell_test_profile_when_enabled() -> None:
    config = runner.default_tenant_config(
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


def test_shell_bridge_command_runs_shell_mcp_server(tmp_path: Path) -> None:
    command = runner.build_shell_bridge_command("shell-workspace", "127.0.0.1", 8766, tmp_path)

    assert "--allowed-tool" in command
    assert "run_command" in command
    assert "from app.shell_mcp_server import main; raise SystemExit(main())" in command
    assert command[-2:] == ["--workspace", str(tmp_path)]


def test_bridge_command_exposes_multiple_workspaces(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    command = runner.build_bridge_command(
        {}, "demo-tenant", "fs-workspace", "127.0.0.1", 8765, [tmp_path, other_workspace]
    )

    assert command[-2:] == [str(tmp_path), str(other_workspace)]


def test_shell_bridge_command_allows_multiple_workspaces(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    command = runner.build_shell_bridge_command(
        "shell-workspace", "127.0.0.1", 8766, [tmp_path, other_workspace]
    )

    workspace_indexes = [index for index, value in enumerate(command) if value == "--workspace"]
    assert [command[index + 1] for index in workspace_indexes] == [
        str(tmp_path),
        str(other_workspace),
    ]


def test_resolve_workspace_roots_splits_env_value(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"

    assert runner.resolve_workspace_roots(None, f"{one},{two}") == [one.resolve(), two.resolve()]


def test_coding_workspace_skill_lists_multiple_workspace_roots(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"

    skill = runner.coding_workspace_skill(workspace_roots=[one, two])

    assert "Configured workspace roots:" in skill["system_prompt"]
    assert str(one) in skill["system_prompt"]
    assert str(two) in skill["system_prompt"]


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

    injected = runner.inject_coding_workspace_skill(
        config,
        "demo-tenant",
        workspace_roots=[tmp_path, tmp_path / "other"],
    )

    skill = json.loads(injected)["demo-tenant"]["skills"]["items"][0]
    assert skill["system_prompt"].startswith("Base coding prompt.")
    assert "Configured workspace roots:" in skill["system_prompt"]
    assert str(tmp_path) in skill["system_prompt"]
    assert str(tmp_path / "other") in skill["system_prompt"]


def test_shell_bridge_command_passes_allowed_command_prefixes(tmp_path: Path) -> None:
    command = runner.build_shell_bridge_command(
        "shell-workspace",
        "127.0.0.1",
        8766,
        tmp_path,
        allowed_command_prefixes=["git", "uv run pytest"],
    )

    prefix_indexes = [
        index for index, value in enumerate(command) if value == "--allowed-command-prefix"
    ]
    assert [command[index + 1] for index in prefix_indexes] == ["git", "uv run pytest"]


def test_shell_allowed_command_prefixes_from_env() -> None:
    assert runner.shell_allowed_command_prefixes_from_env(
        {"MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES": "git, uv run pytest, rg"}
    ) == ["git", "uv run pytest", "rg"]


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

    assert runner.bridge_allowed_tools_from_config(env, "demo-tenant", "fs-workspace") == []


def test_load_coding_mcp_server_specs_expands_workspace_placeholders(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "custom-workspace",
                        "command": [
                            "custom-mcp",
                            "{workspace_roots}",
                            "--root-csv",
                            "{workspace_roots_csv}",
                        ],
                        "port": 9001,
                        "profiles": ["inspect", "test"],
                        "allowed_tools": ["inspect_repo"],
                        "path_policy": {"deny_globs": ["**/.env*"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    specs = runner.load_coding_mcp_server_specs_from_json(
        specs_path.read_text(encoding="utf-8"),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path, other_workspace],
    )

    assert len(specs) == 1
    assert specs[0].name == "custom-workspace"
    assert specs[0].url == "http://127.0.0.1:9001/mcp"
    assert specs[0].command == [
        "custom-mcp",
        str(tmp_path),
        str(other_workspace),
        "--root-csv",
        f"{tmp_path},{other_workspace}",
    ]
    assert specs[0].profiles == ["inspect", "test"]
    assert specs[0].allowed_tools == ["inspect_repo"]
    assert specs[0].path_policy == {"deny_globs": ["**/.env*"]}


def test_load_coding_mcp_server_specs_defaults_stdio_port_when_omitted(tmp_path: Path) -> None:
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "web-fetch",
                        "command": ["uvx", "mcp-server-fetch"],
                        "profiles": ["inspect"],
                        "allowed_tools": ["fetch"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    specs = runner.load_coding_mcp_server_specs_from_json(
        specs_path.read_text(encoding="utf-8"),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path],
    )

    assert len(specs) == 1
    assert specs[0].name == "web-fetch"
    assert specs[0].port == runner.DEFAULT_BRIDGE_PORT
    assert specs[0].url == "http://127.0.0.1:8765/mcp"
    assert specs[0].command == ["uvx", "mcp-server-fetch"]


def test_load_coding_mcp_server_specs_loads_managed_http_server(tmp_path: Path) -> None:
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "web-search",
                        "transport": "http",
                        "managed": True,
                        "command": [
                            "npx",
                            "-y",
                            "@example/search-mcp",
                            "--api-key",
                            "${SEARCH_API_KEY}",
                        ],
                        "url": "http://127.0.0.1:8766/mcp",
                        "health_url": "http://127.0.0.1:8766/ping",
                        "startup_timeout_seconds": 2,
                        "request_timeout": 45,
                        "timeout_seconds": 50,
                        "env": {"SEARCH_API_KEY": "${SEARCH_API_KEY}"},
                        "headers": {"Authorization": "Bearer ${SEARCH_API_KEY}"},
                        "profiles": ["inspect"],
                        "allowed_tools": ["web_search"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    specs = runner.load_coding_mcp_server_specs_from_json(
        specs_path.read_text(encoding="utf-8"),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path],
        env={"SEARCH_API_KEY": "secret-token"},
    )

    assert len(specs) == 1
    assert specs[0].name == "web-search"
    assert specs[0].transport == "http"
    assert specs[0].managed is True
    assert specs[0].command == [
        "npx",
        "-y",
        "@example/search-mcp",
        "--api-key",
        "secret-token",
    ]
    assert specs[0].url == "http://127.0.0.1:8766/mcp"
    assert specs[0].health_url == "http://127.0.0.1:8766/ping"
    assert specs[0].startup_timeout_seconds == 2
    assert specs[0].request_timeout == 45
    assert specs[0].timeout_seconds == 50
    assert specs[0].env == {"SEARCH_API_KEY": "secret-token"}
    assert specs[0].headers == {"Authorization": "Bearer secret-token"}


def test_tenant_mcp_server_from_spec_preserves_headers() -> None:
    spec = runner.CodingMCPServerSpec(
        name="remote-tools",
        url="https://example.com/mcp",
        transport="http",
        headers={"Authorization": "Bearer token"},
        allowed_tools=["search"],
        timeout_seconds=50,
    )

    assert runner.tenant_mcp_server_from_spec(spec) == {
        "name": "remote-tools",
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer token"},
        "timeout_seconds": 50,
        "allowed_tools": ["search"],
    }


def test_load_coding_mcp_server_specs_requires_command_for_managed_http(tmp_path: Path) -> None:
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps({"servers": [{"name": "managed-http", "transport": "http", "managed": True}]}),
        encoding="utf-8",
    )

    try:
        runner.load_coding_mcp_server_specs_from_json(
            specs_path.read_text(encoding="utf-8"),
            bridge_host="127.0.0.1",
            workspace_roots=[tmp_path],
        )
    except RuntimeError as error:
        assert "requires command" in str(error)
    else:
        raise AssertionError("expected RuntimeError")


def test_default_tenant_config_from_servers_builds_profiles() -> None:
    specs = [
        runner.CodingMCPServerSpec(
            name="fs-workspace",
            url="http://127.0.0.1:8765/mcp",
            command=["fs-server"],
            profiles=["inspect", "edit"],
            allowed_tools=["read_file"],
        ),
        runner.CodingMCPServerSpec(
            name="git-workspace",
            url="http://127.0.0.1:8770/mcp",
            command=["git-server"],
            profiles=["test"],
            allowed_tools=["git_status"],
        ),
    ]

    config = runner.default_tenant_config_from_servers("demo-tenant", specs)

    tenant = config["demo-tenant"]
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


def test_build_mcp_stdio_bridge_command_uses_declarative_spec() -> None:
    spec = runner.CodingMCPServerSpec(
        name="custom-workspace",
        url="http://127.0.0.1:9001/custom",
        command=["custom-mcp"],
        port=9001,
        path="/custom",
        allowed_tools=["inspect_repo"],
        request_timeout=45,
        path_policy={"deny_globs": ["**/.env*"], "allow_globs": ["**/.env*.template"]},
    )

    command = runner.build_mcp_stdio_bridge_command(spec)

    assert "--path" in command
    assert command[command.index("--path") + 1] == "/custom"
    assert command[command.index("--request-timeout") + 1] == "45"
    assert command[command.index("--allowed-tool") + 1] == "inspect_repo"
    assert command[command.index("--deny-glob") + 1] == "**/.env*"
    assert command[command.index("--allow-glob") + 1] == "**/.env*.template"
    assert command[-1] == "custom-mcp"


def test_mcp_server_specs_for_gateway_rewrites_stdio_urls_only() -> None:
    specs = [
        runner.CodingMCPServerSpec(
            name="stdio-workspace",
            url="http://127.0.0.1:9001/mcp",
            command=["stdio-server"],
        ),
        runner.CodingMCPServerSpec(
            name="http-workspace",
            url="http://127.0.0.1:9002/mcp",
            transport="http",
        ),
    ]

    transformed = runner.mcp_server_specs_for_gateway(specs, "http://127.0.0.1:8765/mcp")

    assert transformed[0].url == "http://127.0.0.1:8765/mcp/stdio-workspace"
    assert transformed[1].url == "http://127.0.0.1:9002/mcp"


def test_mcp_gateway_config_from_specs_includes_stdio_servers_only() -> None:
    specs = [
        runner.CodingMCPServerSpec(
            name="stdio-workspace",
            url="http://127.0.0.1:9001/mcp",
            command=["stdio-server"],
            allowed_tools=["read_file"],
            path_policy={"deny_globs": ["**/.env*"]},
            env={"EXAMPLE": "1"},
            request_timeout=45,
        ),
        runner.CodingMCPServerSpec(
            name="http-workspace",
            url="http://127.0.0.1:9002/mcp",
            transport="http",
        ),
    ]

    assert runner.mcp_gateway_config_from_specs(specs) == {
        "servers": [
            {
                "name": "stdio-workspace",
                "command": ["stdio-server"],
                "request_timeout": 45,
                "allowed_tools": ["read_file"],
                "path_policy": {"deny_globs": ["**/.env*"]},
                "env": {"EXAMPLE": "1"},
            }
        ]
    }


def test_redacted_command_for_log_hides_sensitive_values() -> None:
    logged = runner.redacted_command_for_log(
        [
            "server",
            "--api-key",
            "secret-token",
            "--password=also-secret",
            "AUTHORIZATION=Bearer token",
        ]
    )

    assert "secret-token" not in logged
    assert "also-secret" not in logged
    assert "Bearer token" not in logged
    assert "<redacted>" in logged


def test_build_mcp_gateway_command() -> None:
    command = runner.build_mcp_gateway_command(Path("gateway.json"), "127.0.0.1", 8765)

    assert "from app.mcp_stdio_gateway import main; main()" in command
    assert command[command.index("--config") + 1] == "gateway.json"
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8765"
