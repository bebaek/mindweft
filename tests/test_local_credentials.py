from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from mindweft_workspace.local_credentials import (
    CredentialError,
    issue_credential,
    read_credential,
)

LAUNCH = "a" * 32
NEXT_LAUNCH = "b" * 32


@pytest.fixture
def directory(tmp_path):
    # macOS exposes /tmp and /var through symlinks; production callers must
    # likewise select a canonical trusted state root before creating credentials.
    return tmp_path.resolve() / "preview"


def test_issue_read_and_digest_verification(directory):
    credential = issue_credential(directory, launch_id=LAUNCH)
    assert read_credential(directory, launch_id=LAUNCH) == credential
    assert len(credential.token) == 43
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "credential").stat().st_mode & 0o777 == 0o600
    assert credential.token not in repr(credential)
    verifier = credential.verifier()
    assert credential.token not in repr(verifier)
    assert not hasattr(verifier, "token")
    assert verifier.accepts(credential.token, launch_id=LAUNCH)
    assert not verifier.accepts(credential.token, launch_id=NEXT_LAUNCH)
    for invalid in ("", "x" * 43, "é" * 43, "x" * 100_000, None):
        assert not verifier.accepts(invalid, launch_id=LAUNCH)


def test_rotation_is_atomic_and_requires_new_verifier(directory):
    previous = issue_credential(directory, launch_id=LAUNCH)
    current = issue_credential(directory, launch_id=NEXT_LAUNCH)
    assert previous.token != current.token
    assert not current.verifier().accepts(previous.token, launch_id=NEXT_LAUNCH)
    with pytest.raises(CredentialError, match="different launch"):
        read_credential(directory, launch_id=LAUNCH)
    assert read_credential(directory, launch_id=NEXT_LAUNCH) == current
    # File rotation alone cannot revoke a verifier already held by a service.
    assert previous.verifier().accepts(previous.token, launch_id=LAUNCH)
    assert sorted(p.name for p in directory.iterdir()) == ["credential"]


def test_instances_have_distinct_credentials(directory):
    first = issue_credential(directory, launch_id=LAUNCH)
    other = issue_credential(directory.parent / "daily", launch_id=LAUNCH)
    assert not other.verifier().accepts(first.token, launch_id=LAUNCH)


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o400, 0o700])
def test_reject_unsafe_file_modes_without_repair(directory, mode):
    credential = issue_credential(directory, launch_id=LAUNCH)
    path = directory / "credential"
    path.chmod(mode)
    for operation in (read_credential, issue_credential):
        with pytest.raises(CredentialError) as error:
            operation(directory, launch_id=LAUNCH)
        assert credential.token not in str(error.value)
        assert path.stat().st_mode & 0o777 == mode
    assert credential.token in path.read_text()


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_reject_unsafe_directory_modes(directory, mode):
    issue_credential(directory, launch_id=LAUNCH)
    directory.chmod(mode)
    for operation in (read_credential, issue_credential):
        with pytest.raises(CredentialError):
            operation(directory, launch_id=LAUNCH)
    assert directory.stat().st_mode & 0o777 == mode


def test_reject_directory_and_ancestor_symlinks(directory):
    issue_credential(directory, launch_id=LAUNCH)
    alias = directory.parent / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    for operation in (read_credential, issue_credential):
        with pytest.raises(CredentialError):
            operation(alias, launch_id=LAUNCH)
        with pytest.raises(CredentialError):
            operation(alias / "child", launch_id=LAUNCH)
    assert not (directory / "child").exists()


@pytest.mark.parametrize("kind", ["symlink", "dangling", "hardlink", "fifo", "directory"])
def test_reject_nonregular_or_linked_files(directory, kind):
    directory.mkdir(mode=0o700)
    path = directory / "credential"
    target = directory.parent / "target"
    target.write_text("do not overwrite")
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "dangling":
        path.symlink_to(directory / "missing")
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir(mode=0o700)
    for operation in (read_credential, issue_credential):
        with pytest.raises(CredentialError):
            operation(directory, launch_id=LAUNCH)
    assert target.read_text() == "do not overwrite"


@pytest.mark.parametrize(
    "contents",
    [
        b"not json secret-value",
        b"\xff",
        b"x" * 1025,
        b"[]",
        b"{}",
        json.dumps({"version": True, "launch_id": LAUNCH, "token": "x" * 43}).encode(),
        json.dumps({"version": 2, "launch_id": LAUNCH, "token": "x" * 43}).encode(),
        json.dumps({"version": 1, "launch_id": LAUNCH, "token": "secret-value"}).encode(),
        json.dumps({"version": 1, "launch_id": [], "token": "x" * 43}).encode(),
    ],
)
def test_malformed_contents_fail_closed_without_disclosure(directory, contents):
    directory.mkdir(mode=0o700)
    path = directory / "credential"
    path.write_bytes(contents)
    path.chmod(0o600)
    for operation in (read_credential, issue_credential):
        with pytest.raises(CredentialError) as error:
            operation(directory, launch_id=LAUNCH)
        assert "secret-value" not in str(error.value)
        assert path.read_bytes() == contents


@pytest.mark.parametrize("launch", ["", "../preview", "A" * 32, "a" * 31, None])
def test_invalid_launch_id_does_not_create_files(directory, launch):
    with pytest.raises(CredentialError):
        issue_credential(directory, launch_id=launch)
    assert not directory.exists()


@pytest.mark.parametrize("path", [Path("relative"), Path("/"), Path("/tmp/../unsafe")])
def test_invalid_paths(path):
    with pytest.raises(CredentialError):
        issue_credential(path, launch_id=LAUNCH)


def test_foreign_ownership_rejected(directory, monkeypatch):
    credential = issue_credential(directory, launch_id=LAUNCH)
    real_fstat = os.fstat
    inode = (directory / "credential").stat().st_ino

    def foreign_stat(fd):
        info = real_fstat(fd)
        if info.st_ino == inode:
            values = list(info)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", foreign_stat)
    for operation in (read_credential, issue_credential):
        with pytest.raises(CredentialError):
            operation(directory, launch_id=LAUNCH)
    assert credential.token in (directory / "credential").read_text()


def test_unsafe_ancestor_rejected(directory):
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    with pytest.raises(CredentialError, match="ancestor"):
        issue_credential(directory / "child", launch_id=LAUNCH)
    assert not (directory / "child").exists()


def test_failed_replace_preserves_old_credential_and_cleans_temporary(directory, monkeypatch):
    previous = issue_credential(directory, launch_id=LAUNCH)

    def fail(*args, **kwargs):
        raise OSError("sensitive operating system details")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(CredentialError) as error:
        issue_credential(directory, launch_id=NEXT_LAUNCH)
    assert "sensitive" not in str(error.value)
    assert read_credential(directory, launch_id=LAUNCH) == previous
    assert sorted(p.name for p in directory.iterdir()) == ["credential"]


def test_concurrent_readers_never_see_partial_rotation(directory):
    issue_credential(directory, launch_id=LAUNCH)

    def read_many():
        return [read_credential(directory, launch_id=LAUNCH) for _ in range(30)]

    with ThreadPoolExecutor(max_workers=4) as executor:
        readers = [executor.submit(read_many) for _ in range(3)]
        for _ in range(10):
            issue_credential(directory, launch_id=LAUNCH)
        for reader in readers:
            assert len(reader.result()) == 30


def test_credential_construction_rejects_invalid_values(directory):
    credential = issue_credential(directory, launch_id=LAUNCH)
    with pytest.raises(CredentialError):
        replace(credential, token="bad")
