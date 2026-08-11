from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from pathlib import Path

import pytest

from app import shell_mcp_server as legacy_shell
from app import text_mcp_server as legacy_text
from minigent_workspace.servers import shell, text

PROJECT_ROOT = Path(__file__).parents[1]


def test_legacy_text_server_imports_delegate_to_workspace_package() -> None:
    assert legacy_text.TextMCPServer is text.TextMCPServer
    assert legacy_text.main is text.main


def test_legacy_shell_server_imports_delegate_to_workspace_package() -> None:
    assert legacy_shell.ShellMCPServer is shell.ShellMCPServer
    assert legacy_shell.main is shell.main


@pytest.mark.parametrize(
    ("script_name", "module_name"),
    [
        ("minigent-text-mcp", "minigent_workspace.servers.text"),
        ("minigent-shell-mcp", "minigent_workspace.servers.shell"),
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
    "module_name",
    [
        "minigent_workspace.servers.text",
        "minigent_workspace.servers.shell",
        "app.text_mcp_server",
        "app.shell_mcp_server",
    ],
)
def test_workspace_server_modules_support_help(module_name: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module_name, "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--workspace" in result.stdout
