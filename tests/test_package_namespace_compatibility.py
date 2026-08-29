from __future__ import annotations

import importlib
import importlib.metadata
import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parents[1]
_PRODUCTION_IMPORT_PATTERN = re.compile(
    r"\b(?:from|import)\s+minigent_(?:client|config|mcp|workspace)\b"
)


def test_distribution_uses_canonical_mindweft_name() -> None:
    distribution = importlib.metadata.distribution("mindweft")

    assert distribution.metadata["Name"] == "mindweft"
    assert distribution.version == "0.1.0"


@pytest.mark.parametrize(
    ("canonical_name", "legacy_name"),
    [
        ("mindweft_client.api_client", "minigent_client.api_client"),
        ("mindweft_client.application", "minigent_client.application"),
        ("mindweft_config.unified_config", "minigent_config.unified_config"),
        ("mindweft_mcp.protocol", "minigent_mcp.protocol"),
        ("mindweft_workspace.runtime_plan", "minigent_workspace.runtime_plan"),
        ("mindweft_workspace.bridge.stdio", "minigent_workspace.bridge.stdio"),
        ("mindweft_workspace.servers.text", "minigent_workspace.servers.text"),
    ],
)
def test_legacy_modules_alias_canonical_implementations(
    canonical_name: str,
    legacy_name: str,
) -> None:
    legacy_module = importlib.import_module(legacy_name)
    canonical_module = importlib.import_module(canonical_name)

    assert legacy_module is canonical_module
    assert canonical_module.__name__ == canonical_name
    assert Path(canonical_module.__file__).is_relative_to(
        _PROJECT_ROOT / canonical_name.split(".")[0]
    )


def test_production_code_imports_only_canonical_package_namespaces() -> None:
    unexpected: list[str] = []
    roots = [
        _PROJECT_ROOT / "app",
        _PROJECT_ROOT / "scripts",
        _PROJECT_ROOT / "mindweft_archive",
        _PROJECT_ROOT / "mindweft_client",
        _PROJECT_ROOT / "mindweft_config",
        _PROJECT_ROOT / "mindweft_mcp",
        _PROJECT_ROOT / "mindweft_workspace",
    ]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if _PRODUCTION_IMPORT_PATTERN.search(line):
                    unexpected.append(f"{path.relative_to(_PROJECT_ROOT)}:{line_number}: {line}")

    assert unexpected == []


@pytest.mark.parametrize(
    ("script_name", "module_name"),
    [
        ("mindweft", "mindweft_client.application"),
        ("mindweft-client", "mindweft_client.cli"),
        ("mindweft-coding-workspace", "mindweft_workspace.application"),
        ("mindweft-mcp-stdio-bridge", "mindweft_workspace.bridge.stdio"),
        ("mindweft-mcp-stdio-gateway", "mindweft_workspace.bridge.gateway"),
        ("mindweft-shell-mcp", "mindweft_workspace.servers.shell"),
        ("mindweft-text-mcp", "mindweft_workspace.servers.text"),
    ],
)
def test_mindweft_entry_points_use_canonical_modules(
    script_name: str,
    module_name: str,
) -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(script for script in scripts if script.name == script_name)

    assert entry_point.value == f"{module_name}:main"
    assert entry_point.load() is importlib.import_module(module_name).main
