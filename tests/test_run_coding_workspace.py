from __future__ import annotations

import importlib.metadata
import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_runner() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_coding_workspace.py"
    spec = importlib.util.spec_from_file_location("run_coding_workspace", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def test_console_script_entry_point_loads_runner_main() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(
        script for script in scripts if script.name == "minigent-coding-workspace"
    )

    loaded = entry_point.load()
    assert loaded.__module__ == "scripts.run_coding_workspace"
    assert loaded.__name__ == "main"


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
                        "command": ["custom-mcp", "{workspace_roots}", "--root-csv", "{workspace_roots_csv}"],
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

    specs = runner.load_coding_mcp_server_specs(
        specs_path,
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
            "allowed_tools": ["read_file"],
        },
        {
            "name": "git-workspace",
            "url": "http://127.0.0.1:8770/mcp",
            "headers": {},
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
        path_policy={"deny_globs": ["**/.env*"], "allow_globs": ["**/.env*.template"]},
    )

    command = runner.build_mcp_stdio_bridge_command(spec)

    assert "--path" in command
    assert command[command.index("--path") + 1] == "/custom"
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
                "allowed_tools": ["read_file"],
                "path_policy": {"deny_globs": ["**/.env*"]},
                "env": {"EXAMPLE": "1"},
            }
        ]
    }


def test_build_mcp_gateway_command() -> None:
    command = runner.build_mcp_gateway_command(Path("gateway.json"), "127.0.0.1", 8765)

    assert "from app.mcp_stdio_gateway import main; main()" in command
    assert command[command.index("--config") + 1] == "gateway.json"
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8765"
