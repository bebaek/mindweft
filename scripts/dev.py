#!/usr/bin/env python3
"""Build and stage the console before running or installing this checkout."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_console(root: Path) -> None:
    web = root / "web"
    subprocess.run(["npm", "ci", "--prefix", str(web)], cwd=root, check=True)
    subprocess.run(["npm", "run", "build", "--prefix", str(web)], cwd=root, check=True)
    dist = web / "dist"
    if not (dist / "index.html").is_file():
        raise RuntimeError("Console build did not produce web/dist/index.html")
    destination = root / "app" / "static" / "console"
    if destination.is_symlink():
        raise RuntimeError("Refusing to replace a symlinked console directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Copy completely before replacing the packaged assets. Do not accumulate old
    # hashed bundles, and restore the previous console if publication fails.
    with tempfile.TemporaryDirectory(prefix=".console-build-", dir=destination.parent) as work:
        staging = Path(work) / "new"
        backup = Path(work) / "previous"
        shutil.copytree(dist, staging)
        if destination.exists():
            destination.rename(backup)
        try:
            staging.rename(destination)
        except OSError:
            if backup.exists():
                backup.rename(destination)
            raise
    print("Console built and staged for this checkout.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build/stage the console, then optionally run or install this checkout.",
        epilog="Examples: ./scripts/dev.py code . --instance preview; ./scripts/dev.py install",
    )
    parser.add_argument("command", choices=("build", "code", "install"))
    args, remainder = parser.parse_known_args(argv)
    if args.command == "install":
        install_parser = argparse.ArgumentParser(prog="dev.py install")
        install_parser.add_argument("--python", default="3.12")
        install_args = install_parser.parse_args(remainder)
    elif args.command == "build" and remainder:
        parser.error("build takes no additional arguments")
    try:
        if args.command != "install":
            build_console(ROOT)
        if args.command == "code":
            # Preserve the caller's cwd: `.` is their workspace, not this repo.
            # --project selects the checkout's Python environment without installing
            # over the user's daily-use uv tool environment.
            if remainder[:1] == ["--"]:
                remainder = remainder[1:]
            return subprocess.run(
                ["uv", "run", "--project", str(ROOT), "mindweft", "code", *remainder],
                check=False,
            ).returncode
        if args.command == "install":
            return subprocess.run(
                [
                    "uv",
                    "tool",
                    "install",
                    "--reinstall",
                    "--python",
                    install_args.python,
                    str(ROOT),
                ],
                cwd=ROOT,
                check=False,
            ).returncode
        return 0
    except subprocess.CalledProcessError as exc:
        print("Build failed; launch/install was not attempted.", file=sys.stderr)
        return exc.returncode
    except (OSError, RuntimeError) as exc:
        print(f"Development setup failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
