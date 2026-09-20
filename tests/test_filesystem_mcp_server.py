from __future__ import annotations

import os

import pytest

from mindweft_workspace.servers.filesystem import FilesystemMCPServer
from mindweft_workspace.servers.readonly import MAX_FILE_BYTES, WorkspaceReadPolicy
from mindweft_workspace.servers.text import TextMCPServer


@pytest.fixture
def roots(tmp_path):
    first = tmp_path / "first project"
    second = tmp_path / "second project"
    first.mkdir()
    second.mkdir()
    (first / "source.txt").write_text("first\n", encoding="utf-8")
    (second / "source.txt").write_text("second\n", encoding="utf-8")
    return [first, second]


def test_multiple_roots_relative_paths_and_bounded_output(roots):
    server = FilesystemMCPServer([*roots, roots[0]])
    assert server.list_allowed_directories() == {"directories": [str(root) for root in roots]}
    assert server.read_file("source.txt")["content"] == "first\n"
    assert server.read_file(str(roots[1] / "source.txt"))["content"] == "second\n"
    result = server.read_file("source.txt", max_chars=3)
    assert result["content"] == "fir" and result["truncated"]
    assert server.list_directory(str(roots[0]))["entries"] == [
        {"name": "source.txt", "type": "file"}
    ]


@pytest.mark.parametrize("limit", [0, -1, 40001, True])
def test_invalid_output_limit(roots, limit):
    with pytest.raises(ValueError, match="max_chars"):
        FilesystemMCPServer(roots).read_file("source.txt", max_chars=limit)


@pytest.mark.parametrize("targeted", [False, True])
def test_path_policy_and_symlinks(roots, targeted):
    first, second = roots
    outside = first.parent / "outside.txt"
    outside.write_text("not allowed")
    hidden = first / ".git"
    hidden.mkdir()
    denied = hidden / "fixture.txt"
    denied.write_text("not allowed")
    (first / "escape").symlink_to(outside)
    (first / "alias.txt").symlink_to(denied)
    (first / "cross-root").symlink_to(second / "source.txt")
    (first / "broken").symlink_to(first / "missing")
    (first / "loop").symlink_to(first / "loop")
    if targeted:
        server = TextMCPServer(workspaces=roots, safe_reads=True)

        def read(path):
            return server.read_text_file_lines({"path": str(path), "start_line": 1, "end_line": 1})
    else:
        server = FilesystemMCPServer(roots)

        def read(path):
            return server.read_file(str(path))

    assert read(first / "cross-root")["content"] == "second\n"
    for path in (
        outside,
        first / ".." / "outside.txt",
        denied,
        first / "escape",
        first / "alias.txt",
        first / "broken",
        first / "loop",
        first / ".env-never-created",
    ):
        with pytest.raises(ValueError):
            read(path)
    listing = FilesystemMCPServer(roots).list_directory(str(first))
    assert {entry["name"] for entry in listing["entries"]} == {"source.txt", "cross-root"}


def test_both_supplied_and_resolved_names_are_checked(roots):
    # A denied alias to an ordinary source file must also remain denied.
    alias = roots[0] / ".git"
    alias.symlink_to(roots[1], target_is_directory=True)
    with pytest.raises(ValueError, match="denied"):
        FilesystemMCPServer(roots).read_file(str(alias / "source.txt"))


@pytest.mark.parametrize("targeted", [False, True])
def test_large_binary_and_nonregular_files_rejected(roots, targeted):
    first = roots[0]
    (first / "large").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    (first / "invalid").write_bytes(b"\xff")
    (first / "binary").write_bytes(b"abc\x00def")
    os.mkfifo(first / "fifo")
    server = (
        TextMCPServer(workspaces=roots, safe_reads=True) if targeted else FilesystemMCPServer(roots)
    )
    for name in ("large", "invalid", "binary", "fifo", "."):
        with pytest.raises(ValueError):
            if targeted:
                server.read_text_file_lines(
                    {"path": str(first / name), "start_line": 1, "end_line": 1}
                )
            else:
                server.read_file(str(first / name))


def test_directory_listing_has_a_scan_limit(roots, monkeypatch):
    from mindweft_workspace.servers import readonly

    monkeypatch.setattr(readonly, "MAX_DIRECTORY_ENTRIES", 2)
    for index in range(5):
        (roots[0] / f"{index}.txt").touch()
    result = FilesystemMCPServer(roots).list_directory(str(roots[0]))
    assert result["truncated"]
    assert len(result["entries"]) == 2


def test_parent_symlink_swap_between_resolve_and_open_is_rejected(roots, monkeypatch):
    parent = roots[0] / "parent"
    parent.mkdir()
    (parent / "source.txt").write_text("allowed")
    policy = WorkspaceReadPolicy(roots)
    original = policy.resolve

    def swap(raw):
        resolved = original(raw)
        parent.rename(roots[0] / "renamed")
        parent.symlink_to(roots[1], target_is_directory=True)
        return resolved

    monkeypatch.setattr(policy, "resolve", swap)
    with pytest.raises(ValueError, match="safely"):
        policy.read_text(str(parent / "source.txt"))


def test_text_safe_reads_enforces_output_cap(roots):
    (roots[0] / "source.txt").write_text("x" * 50000)
    server = TextMCPServer(workspaces=roots, safe_reads=True)
    result = server.read_text_file_lines(
        {"path": "source.txt", "start_line": 1, "end_line": 1, "max_chars": 100000}
    )
    assert len(result["content"]) <= 40000 and result["truncated"]


def test_file_growth_after_stat_is_bounded(roots, monkeypatch):
    from mindweft_workspace.servers import readonly

    monkeypatch.setattr(readonly, "MAX_FILE_BYTES", 10)
    # Simulate a file growing after fstat; one byte beyond the budget is enough to reject it.
    monkeypatch.setattr(readonly.os, "read", lambda fd, size: b"x" * size)
    with pytest.raises(ValueError, match="read limit"):
        WorkspaceReadPolicy(roots).read_text("source.txt")
