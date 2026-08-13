from __future__ import annotations

from app import coding_workspace_runner as legacy_runner
from minigent_workspace import cli, runtime_settings


def test_runner_reexports_canonical_runtime_settings_helpers() -> None:
    assert legacy_runner.WorkspaceRuntimeSettings is runtime_settings.WorkspaceRuntimeSettings
    assert (
        legacy_runner.resolve_workspace_runtime_settings
        is runtime_settings.resolve_workspace_runtime_settings
    )


def test_resolve_workspace_runtime_settings_uses_defaults() -> None:
    args = cli.parse_args([])

    settings = runtime_settings.resolve_workspace_runtime_settings(args, {})

    assert settings == runtime_settings.WorkspaceRuntimeSettings(
        api_host="127.0.0.1",
        api_port=8000,
        bridge_host="127.0.0.1",
        bridge_port=8765,
        bridge_name="fs-workspace",
        bridge_url="http://127.0.0.1:8765/mcp",
        gateway_enabled=False,
        gateway_port=8765,
        gateway_path_prefix="/mcp",
        gateway_url_prefix="http://127.0.0.1:8765/mcp",
        text_enabled=False,
        text_bridge_name="text-workspace",
        text_bridge_port=8767,
        text_bridge_url="http://127.0.0.1:8767/mcp",
        shell_enabled=False,
        shell_bridge_name="shell-workspace",
        shell_bridge_port=8766,
        shell_bridge_url="http://127.0.0.1:8766/mcp",
    )


def test_resolve_workspace_runtime_settings_uses_environment() -> None:
    args = cli.parse_args([])
    env = {
        "MINIGENT_HOST": "api.local",
        "MINIGENT_PORT": "9000",
        "MINIGENT_CODING_BRIDGE_HOST": "bridge.local",
        "MINIGENT_CODING_BRIDGE_PORT": "9001",
        "MINIGENT_CODING_BRIDGE_NAME": "files",
        "MINIGENT_CODING_MCP_GATEWAY_ENABLED": "true",
        "MINIGENT_CODING_MCP_GATEWAY_PORT": "9002",
        "MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX": "gateway/",
        "MINIGENT_CODING_TEXT_ENABLED": "yes",
        "MINIGENT_CODING_TEXT_BRIDGE_NAME": "reader",
        "MINIGENT_CODING_TEXT_BRIDGE_PORT": "9003",
        "MINIGENT_CODING_SHELL_ENABLED": "1",
        "MINIGENT_CODING_SHELL_BRIDGE_NAME": "terminal",
        "MINIGENT_CODING_SHELL_BRIDGE_PORT": "9004",
    }

    settings = runtime_settings.resolve_workspace_runtime_settings(args, env)

    assert settings.api_host == "api.local"
    assert settings.api_port == 9000
    assert settings.bridge_url == "http://bridge.local:9001/mcp"
    assert settings.bridge_name == "files"
    assert settings.gateway_enabled is True
    assert settings.gateway_path_prefix == "gateway/"
    assert settings.gateway_url_prefix == "http://bridge.local:9002/gateway"
    assert settings.text_enabled is True
    assert settings.text_bridge_name == "reader"
    assert settings.text_bridge_url == "http://bridge.local:9003/mcp"
    assert settings.shell_enabled is True
    assert settings.shell_bridge_name == "terminal"
    assert settings.shell_bridge_url == "http://bridge.local:9004/mcp"


def test_resolve_workspace_runtime_settings_cli_overrides_environment() -> None:
    args = cli.parse_args(
        [
            "--api-host",
            "cli-api",
            "--api-port",
            "7000",
            "--bridge-host",
            "cli-bridge",
            "--bridge-port",
            "7001",
            "--bridge-name",
            "cli-files",
            "--mcp-gateway",
            "--mcp-gateway-port",
            "7002",
            "--mcp-gateway-path-prefix",
            "/tools",
            "--enable-text",
            "--text-bridge-name",
            "cli-text",
            "--text-bridge-port",
            "7003",
            "--enable-shell",
            "--shell-bridge-name",
            "cli-shell",
            "--shell-bridge-port",
            "7004",
        ]
    )
    env = {
        "MINIGENT_HOST": "env-api",
        "MINIGENT_PORT": "9000",
        "MINIGENT_CODING_BRIDGE_HOST": "env-bridge",
    }

    settings = runtime_settings.resolve_workspace_runtime_settings(args, env)

    assert settings.api_host == "cli-api"
    assert settings.api_port == 7000
    assert settings.bridge_name == "cli-files"
    assert settings.bridge_url == "http://cli-bridge:7001/mcp"
    assert settings.gateway_url_prefix == "http://cli-bridge:7002/tools"
    assert settings.text_bridge_name == "cli-text"
    assert settings.text_bridge_url == "http://cli-bridge:7003/mcp"
    assert settings.shell_bridge_name == "cli-shell"
    assert settings.shell_bridge_url == "http://cli-bridge:7004/mcp"
