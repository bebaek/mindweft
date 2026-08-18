#!/usr/bin/env python3
"""Smoke-test an installed Mindweft distribution from outside its source tree."""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, distribution, entry_points
from pathlib import Path

EXPECTED_ENTRY_POINTS = {
    "mindweft": "mindweft_client.application:main",
    "mindweft-client": "mindweft_client.cli:main",
    "mindweft-coding-workspace": "mindweft_workspace.application:main",
    "mindweft-mcp-stdio-bridge": "mindweft_workspace.bridge.stdio:main",
    "mindweft-mcp-stdio-gateway": "mindweft_workspace.bridge.gateway:main",
    "mindweft-shell-mcp": "mindweft_workspace.servers.shell:main",
    "mindweft-text-mcp": "mindweft_workspace.servers.text:main",
    "minigent": "minigent_client.application:main",
    "minigent-client": "minigent_client.cli:main",
    "minigent-coding-workspace": "minigent_workspace.application:main",
    "minigent-mcp-stdio-bridge": "minigent_workspace.bridge.stdio:main",
    "minigent-mcp-stdio-gateway": "minigent_workspace.bridge.gateway:main",
    "minigent-shell-mcp": "minigent_workspace.servers.shell:main",
    "minigent-text-mcp": "minigent_workspace.servers.text:main",
}

MODULE_PAIRS = [
    ("mindweft_client.application", "minigent_client.application"),
    ("mindweft_config.unified_config", "minigent_config.unified_config"),
    ("mindweft_mcp.protocol", "minigent_mcp.protocol"),
    ("mindweft_workspace.application", "minigent_workspace.application"),
]

LEGACY_MODULE_COMMANDS = [
    "minigent_workspace.servers.text",
    "minigent_workspace.servers.shell",
    "minigent_workspace.bridge.stdio",
    "minigent_workspace.bridge.gateway",
]


def _validate_metadata(*, version: str) -> None:
    installed = distribution("mindweft")
    if installed.metadata["Name"] != "mindweft":
        raise RuntimeError(
            f"unexpected installed distribution name: {installed.metadata['Name']!r}"
        )
    if installed.version != version:
        raise RuntimeError(f"unexpected installed distribution version: {installed.version!r}")
    try:
        distribution("minigent")
    except PackageNotFoundError:
        pass
    else:
        raise RuntimeError("the wheel unexpectedly installed legacy minigent distribution metadata")


def _validate_modules() -> None:
    for canonical_name, legacy_name in MODULE_PAIRS:
        legacy_module = importlib.import_module(legacy_name)
        canonical_module = importlib.import_module(canonical_name)
        if legacy_module is not canonical_module:
            raise RuntimeError(f"{legacy_name} does not alias {canonical_name}")
        if canonical_module.__name__ != canonical_name:
            raise RuntimeError(f"{canonical_name} is not the implementation owner")
        module_path = canonical_module.__file__
        if module_path is None:
            raise RuntimeError(f"{canonical_name} does not have a source path")
        module_file = Path(module_path).resolve()
        if canonical_name.split(".")[0] not in module_file.parts:
            raise RuntimeError(f"{canonical_name} loaded from unexpected path: {module_file}")


def _validate_entry_points() -> None:
    scripts = {entry.name: entry for entry in entry_points(group="console_scripts")}
    for name, expected_value in EXPECTED_ENTRY_POINTS.items():
        entry = scripts.get(name)
        if entry is None:
            raise RuntimeError(f"missing console script: {name}")
        if entry.value != expected_value:
            raise RuntimeError(f"unexpected entry point for {name}: {entry.value}")
        loaded = entry.load()
        if not loaded.__module__.startswith("mindweft_"):
            raise RuntimeError(f"{name} did not load a canonical implementation: {loaded}")


def _validate_legacy_module_execution() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        for module_name in LEGACY_MODULE_COMMANDS:
            subprocess.run(
                [sys.executable, "-m", module_name, "--help"],
                cwd=temp_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    _validate_metadata(version=args.version)
    _validate_modules()
    _validate_entry_points()
    _validate_legacy_module_execution()
    print("installed Mindweft distribution passed metadata and compatibility smoke tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
