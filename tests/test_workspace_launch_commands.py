from __future__ import annotations

import json
from pathlib import Path

from minigent_workspace import launch_commands
from minigent_workspace.mcp_specs import CodingMCPServerSpec


def test_bridge_command_uses_default_read_only_tools(tmp_path: Path) -> None:
    command = launch_commands.build_bridge_command(
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

    command = launch_commands.build_bridge_command(
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
    command = launch_commands.build_bridge_command(
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

    command = launch_commands.build_bridge_command(
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


def test_text_bridge_command_runs_text_mcp_server(tmp_path: Path) -> None:
    command = launch_commands.build_text_bridge_command(
        "text-workspace", "127.0.0.1", 8767, tmp_path
    )

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

    command = launch_commands.build_text_bridge_command(
        "text-workspace", "127.0.0.1", 8767, [tmp_path, other_workspace]
    )

    workspace_indexes = [index for index, value in enumerate(command) if value == "--workspace"]
    assert [command[index + 1] for index in workspace_indexes] == [
        str(tmp_path),
        str(other_workspace),
    ]


def test_shell_bridge_command_runs_shell_mcp_server(tmp_path: Path) -> None:
    command = launch_commands.build_shell_bridge_command(
        "shell-workspace", "127.0.0.1", 8766, tmp_path
    )

    assert "--allowed-tool" in command
    assert "run_command" in command
    assert "from minigent_workspace.servers.shell import main; raise SystemExit(main())" in command
    assert command[-2:] == ["--workspace", str(tmp_path)]


def test_bridge_command_exposes_multiple_workspaces(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    command = launch_commands.build_bridge_command(
        {}, "demo-tenant", "fs-workspace", "127.0.0.1", 8765, [tmp_path, other_workspace]
    )

    assert command[-2:] == [str(tmp_path), str(other_workspace)]


def test_shell_bridge_command_allows_multiple_workspaces(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    command = launch_commands.build_shell_bridge_command(
        "shell-workspace", "127.0.0.1", 8766, [tmp_path, other_workspace]
    )

    workspace_indexes = [index for index, value in enumerate(command) if value == "--workspace"]
    assert [command[index + 1] for index in workspace_indexes] == [
        str(tmp_path),
        str(other_workspace),
    ]


def test_shell_bridge_command_passes_allowed_command_prefixes(tmp_path: Path) -> None:
    command = launch_commands.build_shell_bridge_command(
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


def test_shell_allowed_command_prefixes_prefer_mindweft_environment() -> None:
    prefixes = launch_commands.shell_allowed_command_prefixes_from_env(
        {
            "MINDWEFT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES": "git, uv run pytest",
            "MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES": "legacy",
        }
    )

    assert prefixes == ["git", "uv run pytest"]


def test_shell_allowed_command_prefixes_from_env() -> None:
    assert launch_commands.shell_allowed_command_prefixes_from_env(
        {"MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES": "git, uv run pytest, rg"}
    ) == ["git", "uv run pytest", "rg"]


def test_build_mcp_stdio_bridge_command_uses_declarative_spec() -> None:
    spec = CodingMCPServerSpec(
        name="custom-workspace",
        url="http://127.0.0.1:9001/custom",
        command=["custom-mcp"],
        port=9001,
        path="/custom",
        allowed_tools=["inspect_repo"],
        request_timeout=45,
        path_policy={"deny_globs": ["**/.env*"], "allow_globs": ["**/.env*.template"]},
    )

    command = launch_commands.build_mcp_stdio_bridge_command(spec)

    assert "from minigent_workspace.bridge.stdio import main; main()" in command
    assert "--path" in command
    assert command[command.index("--path") + 1] == "/custom"
    assert command[command.index("--request-timeout") + 1] == "45"
    assert command[command.index("--allowed-tool") + 1] == "inspect_repo"
    assert command[command.index("--deny-glob") + 1] == "**/.env*"
    assert command[command.index("--allow-glob") + 1] == "**/.env*.template"
    assert command[-1] == "custom-mcp"


def test_build_mcp_gateway_command() -> None:
    command = launch_commands.build_mcp_gateway_command(Path("gateway.json"), "127.0.0.1", 8765)

    assert "from minigent_workspace.bridge.gateway import main; main()" in command
    assert command[command.index("--config") + 1] == "gateway.json"
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "8765"
