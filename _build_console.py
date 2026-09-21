"""Build-time frontend compilation; deliberately independent of runtime packages."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

WEB_CONFIG = (
    "package.json",
    "package-lock.json",
    "index.html",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "vite.config.ts",
    "playwright.config.ts",
    "eslint.config.js",
)


def build_console(root: Path, build_lib: Path) -> None:
    if shutil.which("npm") is None:
        raise RuntimeError(
            "Building Mindweft from source requires Node.js/npm to compile the console. "
            "Install Node.js/npm and retry, or install a prebuilt Mindweft wheel. "
            "Existing console assets will not be reused."
        )
    build_lib.mkdir(parents=True, exist_ok=True)
    # Build outside web/ so untracked dotenv files, npm config, stale dist assets,
    # and node_modules in the checkout cannot enter the build input directory.
    with tempfile.TemporaryDirectory(prefix=".console-source-", dir=build_lib.parent) as work:
        source = Path(work) / "web"
        source.mkdir()
        for name in WEB_CONFIG:
            path = root / "web" / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"Missing or symlinked frontend build input: web/{name}")
            shutil.copyfile(path, source / name)
        for folder in ("src", "e2e"):
            directory = root / "web" / folder
            if not directory.is_dir() or directory.is_symlink():
                raise RuntimeError(f"Missing or symlinked frontend source directory: web/{folder}")
            for path in directory.rglob("*"):
                relative = path.relative_to(root / "web")
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if path.is_symlink():
                    raise RuntimeError(f"Symlinked frontend source: {relative}")
                if path.is_file() and path.suffix in {".ts", ".tsx", ".css"}:
                    destination = source / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
        try:
            subprocess.run(
                ["npm", "ci", "--include=dev", "--no-audit", "--no-fund"], cwd=source, check=True
            )
            subprocess.run(["npm", "run", "build"], cwd=source, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "Console compilation failed; refusing to build a wheel with stale assets. "
                "Check the npm output above."
            ) from exc
        dist = source / "dist"
        if not (dist / "index.html").is_file() or not list((dist / "assets").glob("*.js")):
            raise RuntimeError(
                "Console compilation produced no usable index.html/JavaScript assets"
            )
        destination = build_lib / "app" / "static" / "console"
        if destination.is_symlink():
            raise RuntimeError("Refusing a symlinked console build destination")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(dist, destination)
