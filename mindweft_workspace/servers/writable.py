"""Bounded descriptor-relative file edits for the bundled filesystem server."""

from __future__ import annotations

import difflib
import os
import secrets
import stat
from pathlib import Path

from mindweft_mcp.path_policy import path_denied
from mindweft_workspace.servers.readonly import MAX_FILE_BYTES, WorkspaceReadPolicy


class WorkspaceWritePolicy(WorkspaceReadPolicy):
    def write_text(self, raw_path: str, content: str) -> dict[str, object]:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("content must be UTF-8 text of at most 1 MiB")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        if path_denied(str(candidate), self.policy):
            raise ValueError("path denied by workspace policy")
        with self.open(str(candidate.parent), directory=True) as (parent, fd):
            target = parent / candidate.name
            if path_denied(str(target), self.policy):
                raise ValueError("path denied by workspace policy")
            mode = 0o600
            try:
                info = os.stat(candidate.name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("refusing to replace a symlink, hardlink, or nonregular file")
                mode = stat.S_IMODE(info.st_mode) & 0o777
            except FileNotFoundError:
                pass
            temporary = ".mindweft-edit-" + secrets.token_hex(16)
            out = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            try:
                with os.fdopen(out, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                os.replace(temporary, candidate.name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return {"path": str(target), "bytes_written": len(content.encode("utf-8"))}

    def edit_text(
        self, path: str, old_text: str, new_text: str, *, dry_run: bool = False
    ) -> dict[str, object]:
        if not old_text:
            raise ValueError("old_text must not be empty")
        resolved, original = self.read_text(path)
        if original.count(old_text) != 1:
            raise ValueError("old_text must match exactly once; read the file and retry")
        updated = original.replace(old_text, new_text, 1)
        if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("edited file exceeds 1 MiB")
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(True),
                updated.splitlines(True),
                fromfile=str(resolved),
                tofile=str(resolved),
            )
        )
        if not dry_run:
            self.write_text(path, updated)
        return {
            "path": str(resolved),
            "diff": diff[:40000],
            "truncated": len(diff) > 40000,
            "dry_run": dry_run,
        }
