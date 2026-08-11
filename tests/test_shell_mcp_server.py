from __future__ import annotations

from pathlib import Path

import pytest

from minigent_workspace.servers.shell import ShellMCPServer


def test_run_command_executes_inside_workspace(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh")

    result = server.run_command({"command": "pwd", "cwd": str(tmp_path)})

    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["stdout"].strip() == str(tmp_path)
    assert result["cwd"] == str(tmp_path)


def test_run_command_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh")

    with pytest.raises(ValueError, match="cwd must be inside a workspace root"):
        server.run_command({"command": "pwd", "cwd": "/tmp"})


def test_run_command_allows_cwd_inside_any_configured_workspace(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    server = ShellMCPServer(workspaces=[tmp_path, other_workspace], shell="/bin/sh")

    result = server.run_command({"command": "pwd", "cwd": str(other_workspace)})

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == str(other_workspace)


def test_run_command_allows_configured_command_prefix(tmp_path: Path) -> None:
    server = ShellMCPServer(
        workspace=tmp_path,
        shell="/bin/sh",
        allowed_command_prefixes=["printf", "git status"],
    )

    result = server.run_command({"command": "printf ok"})

    assert result["exit_code"] == 0
    assert result["stdout"] == "ok"


def test_run_command_rejects_command_outside_prefix_allowlist(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh", allowed_command_prefixes=["git"])

    with pytest.raises(ValueError, match="command is not allowed"):
        server.run_command({"command": "cat .env"})


def test_run_command_truncates_output(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh", max_output_chars=20)

    result = server.run_command({"command": "printf 1234567890abcdef", "max_output_chars": 8})

    assert result["stdout"] == "12345678"
    assert result["stdout_truncated"] is True


def test_run_command_reports_timeout(tmp_path: Path) -> None:
    server = ShellMCPServer(workspace=tmp_path, shell="/bin/sh")

    result = server.run_command({"command": "sleep 2", "timeout_seconds": 0.1})

    assert result["timed_out"] is True
    assert result["exit_code"] is not None
