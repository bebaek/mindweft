"""POSIX credential primitives for the credential-backed local launcher.

A credential directory must be dedicated to one instance and service role. Callers must hold that instance's lifetime lock when issuing/rotating.
Never put these values in discovery metadata, logs, argv, or browser storage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

_FILE_NAME = "credential"
_MAX_BYTES = 1024


class CredentialError(RuntimeError):
    """Fail-closed credential error whose message contains no credential contents."""


def _launch_id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise CredentialError("Invalid local launch identity")


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None


@dataclass(frozen=True)
class LocalCredential:
    launch_id: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        _launch_id(self.launch_id)
        if not _valid_token(self.token):
            raise CredentialError("Invalid local credential")

    def verifier(self) -> CredentialVerifier:
        return CredentialVerifier(
            self.launch_id, hashlib.sha256(self.token.encode("ascii")).digest()
        )


@dataclass(frozen=True)
class CredentialVerifier:
    """Server-side snapshot: retain a digest, not the reusable bearer value.

    Rotation requires replacing this snapshot (or restarting the service). Merely
    replacing the on-disk credential does not revoke an existing verifier.
    """

    launch_id: str
    digest: bytes = field(repr=False)

    def accepts(self, token: str, *, launch_id: str) -> bool:
        if launch_id != self.launch_id or not _valid_token(token):
            return False
        return hmac.compare_digest(self.digest, hashlib.sha256(token.encode("ascii")).digest())


def _check_directory(fd: int, *, private: bool) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise CredentialError("Local credential path is not a directory")
    if private:
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CredentialError(
                "Local credential directory must be owned by this user with mode 0700"
            )
    else:
        # Permit root-owned sticky temporary roots, but not user-controlled or
        # generally writable ancestors. Sticky roots prohibit replacing another
        # user's entry; the next component is opened without following symlinks.
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if info.st_uid not in {0, os.getuid()} or (info.st_mode & 0o022 and not sticky_root):
            raise CredentialError("Unsafe ancestor of local credential directory")


@contextmanager
def _directory(path: Path, *, create: bool) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise CredentialError("Local credential directory must be an absolute non-root path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        _check_directory(fd, private=False)
        for index, part in enumerate(path.parts[1:]):
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            _check_directory(fd, private=index == len(path.parts) - 2)
        yield fd
    finally:
        os.close(fd)


def _read(fd: int) -> LocalCredential:
    descriptor = os.open(
        _FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            # An atomically replaced file can have zero links after open.
            or info.st_nlink not in {0, 1}
            or info.st_size > _MAX_BYTES
        ):
            raise CredentialError(
                "Local credential must be an owner-only regular file without hardlinks, mode 0600"
            )
        raw = os.read(descriptor, _MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_BYTES:
        raise CredentialError("Invalid local credential file")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"version", "launch_id", "token"}:
            raise ValueError
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError
        return LocalCredential(launch_id=data["launch_id"], token=data["token"])
    except (ValueError, TypeError, KeyError):
        raise CredentialError("Invalid local credential file") from None


def read_credential(directory: Path, *, launch_id: str) -> LocalCredential:
    """Read only from a safe path and require the caller's discovered launch ID.

    No HTTP requests are performed here. Clients must separately validate their
    destination as loopback, disable proxies/redirects, and verify instance identity.
    """
    _launch_id(launch_id)
    try:
        with _directory(directory, create=False) as fd:
            credential = _read(fd)
    except OSError:
        raise CredentialError("Cannot safely read local credential") from None
    if credential.launch_id != launch_id:
        raise CredentialError("Local credential belongs to a different launch; reconnect")
    return credential


def issue_credential(directory: Path, *, launch_id: str) -> LocalCredential:
    """Atomically issue/rotate under the instance lifetime lock; never chmod unsafe files.

    Reject symlinks (including ancestor symlinks), hardlinks, foreign ownership,
    unsafe permissions, and malformed existing files instead of repairing them.
    The caller must provide a canonical, trusted directory path, e.g. /private/tmp
    rather than macOS's /tmp symlink. Existing secrets are never logged in errors.
    """
    _launch_id(launch_id)
    credential = LocalCredential(launch_id=launch_id, token=secrets.token_urlsafe(32))
    try:
        with _directory(directory, create=True) as fd:
            try:
                _read(fd)
            except FileNotFoundError:
                pass
            temporary = f".credential-{secrets.token_hex(16)}"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=fd,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    # Set exact owner permissions even under a restrictive umask.
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(
                        {"version": 1, "launch_id": launch_id, "token": credential.token}, stream
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, _FILE_NAME, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
    except OSError:
        raise CredentialError("Cannot safely issue local credential") from None
    return credential
