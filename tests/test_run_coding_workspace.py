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
        "--base-url",
        "http://127.0.0.1:9100",
        "config",
        "export",
        "--local-coding",
        "--coding-env-file",
        ".env.coding",
    ]


def test_coding_config_export_can_skip_env_file(monkeypatch) -> None:
    monkeypatch.delenv("MINIGENT_BASE_URL", raising=False)
    args = runner.parse_config_args(["config", "export", "--no-env-file"])

    assert runner.build_coding_config_export_client_argv(args) == [
        "config",
        "export",
        "--local-coding",
        "--no-coding-env-file",
    ]


def test_load_config_command_env_sets_dotenv_without_overriding(
    tmp_path: Path, monkeypatch
) -> None:
    env_path = tmp_path / ".env.coding"
    env_path.write_text(
        "MINIGENT_BASE_URL=http://from-dotenv.example\nMINIGENT_CODING_TENANT_ID=tenant\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIGENT_BASE_URL", "http://from-env.example")
    monkeypatch.delenv("MINIGENT_CODING_TENANT_ID", raising=False)

    runner.load_config_command_env(str(env_path))

    assert runner.os.environ["MINIGENT_DOTENV_FILE"] == str(env_path)
    assert runner.os.environ["MINIGENT_BASE_URL"] == "http://from-env.example"
    assert runner.os.environ["MINIGENT_CODING_TENANT_ID"] == "tenant"


def test_coding_workspace_state_defaults_use_xdg_state_home(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path)}

    runner.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(tmp_path / "minigent" / "attachments.db")


def test_coding_workspace_state_defaults_use_home_fallback(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}

    runner.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(
        tmp_path / ".local" / "state" / "minigent" / "attachments.db"
    )


def test_coding_workspace_state_defaults_preserve_attachment_override(tmp_path: Path) -> None:
    configured_path = tmp_path / "custom-attachments.db"
    env = {
        "HOME": str(tmp_path),
        "MINIGENT_ATTACHMENT_DB_PATH": str(configured_path),
    }

    runner.apply_coding_workspace_state_defaults(env)

    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(configured_path)


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


def test_load_env_file_can_skip_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_CODING_WORKSPACES", raising=False)
    env_path = tmp_path / ".env.coding"
    env_path.write_text("MINIGENT_CODING_WORKSPACES=/should/not/read\n", encoding="utf-8")

    env = runner.load_env_file(None)

    assert env.get("MINIGENT_CODING_WORKSPACES") != "/should/not/read"


def test_load_env_file_can_suppress_missing_default_message(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    runner.load_env_file(".env.coding", warn_if_missing=False)

    assert "env file not found" not in capsys.readouterr().out


def test_load_env_file_warns_for_explicit_missing_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    runner.load_env_file("missing.env", warn_if_missing=True)

    assert (
        "env file not found; continuing with current environment: missing.env"
        in capsys.readouterr().out
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
    assert "from minigent_workspace.servers.text import main; raise SystemExit(main())" in command
    assert command[-2:] == ["--workspace", str(tmp_path)]


def test_text_bridge_command_allows_multiple_workspaces(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    command = runner.build_text_bridge_command(
        "text-workspace", "127.0.0.1", 8767, [tmp_path, other_workspace]
    )

    workspace_indexes = [index for index, value in enumerate(command) if value == "--workspace"]
    assert [command[index + 1] for index in workspace_indexes] == [
        str(tmp_path),
        str(other_workspace),
    ]


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
    assert "from minigent_workspace.servers.shell import main; raise SystemExit(main())" in command
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


def test_coding_workspace_skill_lists_multiple_workspace_roots(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"

    skill = runner.coding_workspace_skill(workspace_roots=[one, two])

    assert "Configured workspace roots:" in skill["system_prompt"]
    assert str(one) in skill["system_prompt"]
    assert str(two) in skill["system_prompt"]


def test_coding_workspace_skill_includes_active_scope(tmp_path: Path) -> None:
    skill = runner.coding_workspace_skill(workspace_roots=[tmp_path], workspace_scope="repo")

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
        runner.CodingMCPServerSpec(
            name="fs-workspace",
            url="http://127.0.0.1:8765/mcp/fs-workspace",
            transport="stdio",
            command=["fs-server"],
        ),
        runner.CodingMCPServerSpec(
            name="web-search",
            url="http://127.0.0.1:8766/mcp",
            transport="http",
        ),
    ]

    assert runner.tenant_gateway_mcp_server_mismatches(
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
        runner.CodingMCPServerSpec(
            name="text-workspace",
            url="http://127.0.0.1:8765/mcp/text-workspace",
            transport="stdio",
            command=["text-server"],
        )
    ]

    assert (
        runner.tenant_gateway_mcp_server_mismatches(
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

    assert runner.bridge_allowed_tools_from_config(env, "demo-tenant", "fs-workspace") == []


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

    assert "from minigent_workspace.bridge.stdio import main; main()" in command
    assert "--path" in command
    assert command[command.index("--path") + 1] == "/custom"
    assert command[command.index("--request-timeout") + 1] == "45"
    assert command[command.index("--allowed-tool") + 1] == "inspect_repo"
    assert command[command.index("--deny-glob") + 1] == "**/.env*"
    assert command[command.index("--allow-glob") + 1] == "**/.env*.template"
    assert command[-1] == "custom-mcp"


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
        runner.CodingMCPServerSpec(
            name="fs-workspace",
            url="http://127.0.0.1:8765/mcp/fs-workspace",
            allowed_tools=["write_file", "edit_file"],
            path_policy={"deny_globs": ["**/.env*"]},
        ),
        runner.CodingMCPServerSpec(
            name="text-workspace",
            url="http://127.0.0.1:8765/mcp/text-workspace",
            allowed_tools=["read_text_file_lines"],
        ),
    ]

    injected = json.loads(runner.inject_coding_mcp_servers(raw_config, "demo-tenant", specs))

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

    assert "from minigent_workspace.bridge.gateway import main; main()" in command
    assert command[command.index("--config") + 1] == "gateway.json"
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8765"
