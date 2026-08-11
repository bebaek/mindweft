from __future__ import annotations

from pathlib import Path

import pytest

from minigent_workspace.servers.text import TextMCPServer


def test_read_text_file_lines_reads_inclusive_line_range(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.py"
    file_path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    server = TextMCPServer(workspace=tmp_path)

    result = server.read_text_file_lines({"path": str(file_path), "start_line": 2, "end_line": 3})

    assert result["path"] == str(file_path)
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["line_count"] == 4
    assert result["content"] == "two\nthree\n"
    assert result["truncated"] is False


def test_read_text_file_lines_allows_workspace_relative_paths(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    server = TextMCPServer(workspace=tmp_path)

    result = server.read_text_file_lines({"path": "sample.txt", "start_line": 1, "end_line": 1})

    assert result["path"] == str(file_path)
    assert result["content"] == "alpha\n"


def test_read_text_file_lines_allows_absolute_paths_in_any_workspace_root(tmp_path: Path) -> None:
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    workspace_one.mkdir()
    workspace_two.mkdir()
    file_path = workspace_two / "sample.txt"
    file_path.write_text("from second root\n", encoding="utf-8")
    server = TextMCPServer(workspaces=[workspace_one, workspace_two])

    result = server.read_text_file_lines({"path": str(file_path), "start_line": 1, "end_line": 1})

    assert result["path"] == str(file_path)
    assert result["content"] == "from second root\n"


def test_read_text_file_lines_resolves_relative_paths_against_first_workspace_root(
    tmp_path: Path,
) -> None:
    workspace_one = tmp_path / "one"
    workspace_two = tmp_path / "two"
    workspace_one.mkdir()
    workspace_two.mkdir()
    file_path = workspace_one / "sample.txt"
    file_path.write_text("from first root\n", encoding="utf-8")
    server = TextMCPServer(workspaces=[workspace_one, workspace_two])

    result = server.read_text_file_lines({"path": "sample.txt", "start_line": 1, "end_line": 1})

    assert result["path"] == str(file_path)
    assert result["content"] == "from first root\n"


def test_read_text_file_around_adds_context(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    server = TextMCPServer(workspace=tmp_path)

    result = server.read_text_file_around(
        {"path": str(file_path), "line": 3, "before": 1, "after": 2}
    )

    assert result["start_line"] == 2
    assert result["end_line"] == 5
    assert result["content"] == "two\nthree\nfour\nfive\n"


def test_search_text_file_returns_match_contexts(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\ngamma\nbeta two\n", encoding="utf-8")
    server = TextMCPServer(workspace=tmp_path)

    result = server.search_text_file(
        {"path": str(file_path), "pattern": "beta", "before": 1, "after": 1}
    )

    assert result["match_count"] == 2
    assert result["matches"][0] == {
        "line": 2,
        "start_line": 1,
        "end_line": 3,
        "content": "alpha\nbeta\ngamma\n",
        "truncated": False,
    }
    assert result["matches"][1]["line"] == 4
    assert result["matches"][1]["content"] == "gamma\nbeta two\n"


def test_text_mcp_server_rejects_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    server = TextMCPServer(workspace=tmp_path)

    with pytest.raises(ValueError, match="path must be inside a workspace root"):
        server.read_text_file_lines({"path": str(outside), "start_line": 1, "end_line": 1})


def test_text_mcp_server_rejects_invalid_line_range(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    server = TextMCPServer(workspace=tmp_path)

    with pytest.raises(ValueError, match="end_line must be >= start_line"):
        server.read_text_file_lines({"path": str(file_path), "start_line": 2, "end_line": 1})
