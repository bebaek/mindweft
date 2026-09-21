#!/usr/bin/env python3
"""Validate built Mindweft source and wheel artifacts without installing them."""

from __future__ import annotations

import argparse
import configparser
import email.parser
import re
import tarfile
import zipfile
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

REQUIRED_PACKAGE_FILES = {
    "app/main.py",
    "mindweft_client/application.py",
    "mindweft_config/unified_config.py",
    "mindweft_mcp/protocol.py",
    "mindweft_workspace/application.py",
    "minigent_client/application.py",
    "minigent_config/unified_config.py",
    "minigent_mcp/protocol.py",
    "minigent_workspace/application.py",
}


def _single_artifact(dist_dir: Path, pattern: str) -> Path:
    artifacts = sorted(dist_dir.glob(pattern))
    if len(artifacts) != 1:
        raise RuntimeError(
            f"expected one artifact matching {pattern!r} in {dist_dir}, found {artifacts}"
        )
    return artifacts[0]


def _validate_wheel(wheel: Path, *, version: str) -> None:
    expected_prefix = f"mindweft-{version}-"
    if not wheel.name.startswith(expected_prefix):
        raise RuntimeError(f"unexpected wheel filename: {wheel.name}")

    dist_info = f"mindweft-{version}.dist-info"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = REQUIRED_PACKAGE_FILES.difference(names)
        if missing:
            raise RuntimeError(f"wheel is missing package files: {sorted(missing)}")

        console_index = "app/static/console/index.html"
        if console_index not in names:
            raise RuntimeError("wheel is missing the built console")
        html = archive.read(console_index).decode("utf-8")
        assets = re.findall(r'(?:src|href)="/console/(assets/[^"?#]+)"', html)
        if not any(asset.endswith(".js") for asset in assets):
            raise RuntimeError("console index does not reference a JavaScript bundle")
        for asset in assets:
            if f"app/static/console/{asset}" not in names:
                raise RuntimeError(f"wheel is missing a referenced console asset: {asset}")

        metadata = email.parser.BytesParser().parsebytes(archive.read(f"{dist_info}/METADATA"))
        if metadata["Name"] != "mindweft":
            raise RuntimeError(f"unexpected distribution name: {metadata['Name']!r}")
        if metadata["Version"] != version:
            raise RuntimeError(f"unexpected distribution version: {metadata['Version']!r}")
        if metadata["Description-Content-Type"] != "text/markdown":
            raise RuntimeError("wheel metadata does not identify the README as Markdown")
        if metadata["License-Expression"] != "Apache-2.0":
            raise RuntimeError(f"unexpected license expression: {metadata['License-Expression']!r}")
        if metadata.get_all("License-File", []) != ["LICENSE"]:
            raise RuntimeError(f"unexpected license files: {metadata.get_all('License-File', [])}")
        license_path = f"{dist_info}/licenses/LICENSE"
        if license_path not in names:
            raise RuntimeError(f"wheel is missing its license file: {license_path}")
        project_urls = set(metadata.get_all("Project-URL", []))
        expected_project_urls = {
            "Repository, https://github.com/bebaek/mindweft",
            "Issues, https://github.com/bebaek/mindweft/issues",
        }
        if project_urls != expected_project_urls:
            raise RuntimeError(f"unexpected project URLs: {sorted(project_urls)}")
        payload = metadata.get_payload()
        if not isinstance(payload, str) or not payload.startswith("# Mindweft"):
            raise RuntimeError("wheel long description does not contain the Mindweft README")

        parser = configparser.ConfigParser()
        parser.read_string(archive.read(f"{dist_info}/entry_points.txt").decode("utf-8"))
        actual_entry_points = dict(parser["console_scripts"])
        if actual_entry_points != EXPECTED_ENTRY_POINTS:
            raise RuntimeError(
                "wheel console scripts do not match the canonical and compatibility contract: "
                f"{actual_entry_points}"
            )


def _validate_sdist(sdist: Path, *, version: str) -> None:
    expected_name = f"mindweft-{version}.tar.gz"
    if sdist.name != expected_name:
        raise RuntimeError(f"unexpected source distribution filename: {sdist.name}")

    prefix = f"mindweft-{version}/"
    required = {
        f"{prefix}README.md",
        f"{prefix}CHANGELOG.md",
        f"{prefix}LICENSE",
        f"{prefix}pyproject.toml",
        f"{prefix}setup.py",
        f"{prefix}_build_console.py",
        f"{prefix}web/package.json",
        f"{prefix}web/package-lock.json",
        f"{prefix}web/src/main.tsx",
        f"{prefix}web/vite.config.ts",
        *(f"{prefix}{path}" for path in REQUIRED_PACKAGE_FILES),
    }
    with tarfile.open(sdist, mode="r:gz") as archive:
        names = set(archive.getnames())
    forbidden = [
        name
        for name in names
        if any(
            part in {"node_modules", "dist", ".npmrc"} or part.startswith(".env")
            for part in name.removeprefix(prefix).split("/")
        )
    ]
    if forbidden:
        raise RuntimeError(f"source distribution contains unwanted build inputs: {forbidden}")
    missing = required.difference(names)
    if missing:
        raise RuntimeError(f"source distribution is missing files: {sorted(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    wheel = _single_artifact(args.dist_dir, "mindweft-*.whl")
    sdist = _single_artifact(args.dist_dir, "mindweft-*.tar.gz")
    _validate_wheel(wheel, version=args.version)
    _validate_sdist(sdist, version=args.version)
    print(f"validated {wheel.name} and {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
