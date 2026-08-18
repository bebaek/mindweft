from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from pathlib import Path

import pytest

from app import mcp_stdio_bridge as legacy_bridge
from app import mcp_stdio_gateway as legacy_gateway
from app import shell_mcp_server as legacy_shell
from app import text_mcp_server as legacy_text
from minigent_workspace.bridge import gateway, stdio
from minigent_workspace.servers import shell, text

PROJECT_ROOT = Path(__file__).parents[1]


def test_legacy_text_server_imports_delegate_to_workspace_package() -> None:
    assert legacy_text.TextMCPServer is text.TextMCPServer
    assert legacy_text.main is text.main


def test_legacy_shell_server_imports_delegate_to_workspace_package() -> None:
    assert legacy_shell.ShellMCPServer is shell.ShellMCPServer
    assert legacy_shell.main is shell.main


def test_legacy_bridge_imports_delegate_to_workspace_package() -> None:
    assert legacy_bridge.BridgeSettings is stdio.BridgeSettings
    assert legacy_bridge.StdioMCPBridge is stdio.StdioMCPBridge
    assert legacy_bridge.main is stdio.main


def test_legacy_gateway_imports_delegate_to_workspace_package() -> None:
    assert legacy_gateway.GatewaySettings is gateway.GatewaySettings
    assert legacy_gateway.create_gateway_app is gateway.create_gateway_app
    assert legacy_gateway.main is gateway.main


@pytest.mark.parametrize(
    ("script_name", "module_name"),
    [
        ("mindweft-text-mcp", "mindweft_workspace.servers.text"),
        ("mindweft-shell-mcp", "mindweft_workspace.servers.shell"),
        ("mindweft-mcp-stdio-bridge", "mindweft_workspace.bridge.stdio"),
        ("mindweft-mcp-stdio-gateway", "mindweft_workspace.bridge.gateway"),
        ("minigent-text-mcp", "mindweft_workspace.servers.text"),
        ("minigent-shell-mcp", "mindweft_workspace.servers.shell"),
        ("minigent-mcp-stdio-bridge", "mindweft_workspace.bridge.stdio"),
        ("minigent-mcp-stdio-gateway", "mindweft_workspace.bridge.gateway"),
    ],
)
def test_workspace_server_console_scripts_use_canonical_modules(
    script_name: str, module_name: str
) -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(script for script in scripts if script.name == script_name)

    loaded = entry_point.load()

    assert loaded.__module__ == module_name
    assert loaded.__name__ == "main"


@pytest.mark.parametrize(
    ("module_name", "expected_option"),
    [
        ("minigent_workspace.servers.text", "--workspace"),
        ("minigent_workspace.servers.shell", "--workspace"),
        ("app.text_mcp_server", "--workspace"),
        ("app.shell_mcp_server", "--workspace"),
        ("minigent_workspace.bridge.stdio", "--name"),
        ("minigent_workspace.bridge.gateway", "--config"),
        ("app.mcp_stdio_bridge", "--name"),
        ("app.mcp_stdio_gateway", "--config"),
    ],
)
def test_workspace_modules_support_help(module_name: str, expected_option: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module_name, "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert expected_option in result.stdout
