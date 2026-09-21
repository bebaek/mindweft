"""OS-user-owned workspace trust, independent of agent configuration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from mindweft_workspace.instances import registry_dir
from mindweft_workspace.local_credentials import _directory


def choose_coding_access(args, roots: list[Path], env: dict[str, str]) -> bool:
    """Return True for trusted editing/execution, False for explicit inspect mode."""
    if args.read_only:
        return False
    directory = registry_dir(env).parent.resolve() / "workspace-trust"
    missing = [root for root in roots if not _trusted(directory, root)]
    if not missing:
        return True
    if not args.trust_workspace:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Workspace trust required. Use --trust-workspace to allow edits and shell execution, or --read-only to inspect."
            )
        print("Trust these workspaces for file editing and shell execution?", file=sys.stderr)
        for root in missing:
            print(f"  {root}", file=sys.stderr)
        print(
            "Commands run as your OS user and can access files/network outside these roots. This is not a sandbox.",
            file=sys.stderr,
        )
        try:
            answer = input("[trust / inspect / cancel]: ").strip().lower()
        except EOFError:
            raise RuntimeError("Workspace trust was not granted") from None
        if answer == "inspect":
            return False
        if answer != "trust":
            raise RuntimeError("Workspace trust was not granted")
    for root in missing:
        _remember(directory, root)
    return True


def _name(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest() + ".json"


def _trusted(directory: Path, root: Path) -> bool:
    try:
        with _directory(directory, create=False) as parent:
            fd = os.open(_name(root), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                ):
                    raise RuntimeError("Unsafe workspace trust record")
                record = json.loads(os.read(fd, 16384))
            finally:
                os.close(fd)
        return record == {"version": 1, "root": str(root)}
    except FileNotFoundError:
        return False
    except (ValueError, UnicodeError):
        raise RuntimeError("Invalid workspace trust record") from None


def _remember(directory: Path, root: Path) -> None:
    with _directory(directory, create=True) as parent:
        fd = os.open(
            _name(root), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "root": str(root)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
