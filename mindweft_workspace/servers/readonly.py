"""Bounded, read-only file access for the packaged inspect tools.

POSIX descriptor traversal refuses symlinks after canonicalization, including parent
components swapped between validation and open. This is not an OS sandbox: hard links,
concurrent renames of already-open directories, and same-user processes remain trusted.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from mindweft_mcp.path_policy import MCPPathPolicy, path_denied
from mindweft_workspace.tenant_config import DEFAULT_BRIDGE_ALLOW_GLOBS, DEFAULT_BRIDGE_DENY_GLOBS

MAX_FILE_BYTES = 1_048_576
MAX_DIRECTORY_ENTRIES = 1000


class WorkspaceReadPolicy:
    def __init__(self, workspaces: Sequence[Path]) -> None:
        self.roots = tuple(dict.fromkeys(root.expanduser().resolve() for root in workspaces))
        if not self.roots or any(not root.is_dir() for root in self.roots):
            raise ValueError("at least one existing workspace directory is required")
        self.policy = MCPPathPolicy(
            deny_globs=list(DEFAULT_BRIDGE_DENY_GLOBS),
            allow_globs=list(DEFAULT_BRIDGE_ALLOW_GLOBS),
        )

    def resolve(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty string")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        if path_denied(str(candidate), self.policy):
            raise ValueError("path denied by workspace policy")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("path does not exist or cannot be resolved") from exc
        if not any(resolved.is_relative_to(root) for root in self.roots):
            raise ValueError("path must be inside a workspace root")
        if path_denied(str(resolved), self.policy):
            raise ValueError("resolved path denied by workspace policy")
        return resolved

    @contextmanager
    def open(self, raw_path: str, *, directory: bool = False) -> Iterator[tuple[Path, int]]:
        path = self.resolve(raw_path)
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("packaged workspace reads currently require POSIX no-follow support")
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            parts = path.parts[1:]
            for index, part in enumerate(parts):
                is_directory = index < len(parts) - 1 or directory
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if is_directory:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            mode = os.fstat(fd).st_mode
            if directory and not stat.S_ISDIR(mode):
                raise ValueError("path is not a directory")
            if not directory and not stat.S_ISREG(mode):
                raise ValueError("path is not a regular file")
            yield path, fd
        except OSError as exc:
            raise ValueError("path could not be accessed safely") from exc
        finally:
            os.close(fd)

    def read_text(self, raw_path: str) -> tuple[Path, str]:
        with self.open(raw_path) as (path, fd):
            if os.fstat(fd).st_size > MAX_FILE_BYTES:
                raise ValueError(f"file exceeds the {MAX_FILE_BYTES}-byte read limit")
            chunks = []
            size = 0
            while size <= MAX_FILE_BYTES:
                chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise ValueError(f"file exceeds the {MAX_FILE_BYTES}-byte read limit")
        try:
            content = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("file is not valid UTF-8 text") from exc
        if "\x00" in content:
            raise ValueError("file contains binary data")
        return path, content

    def list_directory(self, raw_path: str) -> dict[str, object]:
        entries: list[dict[str, str]] = []
        truncated = False
        with self.open(raw_path, directory=True) as (path, fd), os.scandir(fd) as iterator:
            for index, entry in enumerate(iterator):
                if index >= MAX_DIRECTORY_ENTRIES:
                    truncated = True
                    break
                try:
                    self.resolve(str(path / entry.name))
                    if entry.is_dir():
                        kind = "directory"
                    elif entry.is_file():
                        kind = "file"
                    else:
                        continue
                except (ValueError, OSError):
                    continue
                entries.append({"name": entry.name, "type": kind})
        return {
            "path": str(path),
            "entries": sorted(entries, key=lambda item: item["name"]),
            "truncated": truncated,
        }
