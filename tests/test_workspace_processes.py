from __future__ import annotations

import signal
import subprocess
from unittest.mock import Mock

import pytest

from app import coding_workspace_runner as legacy_runner
from minigent_workspace import processes
from minigent_workspace.mcp_specs import CodingMCPServerSpec


def test_runner_reexports_canonical_process_helpers() -> None:
    names = [
        "start_process",
        "redacted_command_for_log",
        "wait_for_managed_http_server",
        "wait_for_processes",
        "stop_process",
    ]

    for name in names:
        assert getattr(legacy_runner, name) is getattr(processes, name)


def test_redacted_command_for_log_hides_sensitive_values() -> None:
    logged = processes.redacted_command_for_log(
        [
            "server",
            "--api-key",
            "secret-token",
            "--password=also-secret",
            "AUTHORIZATION=Bearer token",
        ]
    )

    assert "secret-token" not in logged
    assert "also-secret" not in logged
    assert "Bearer token" not in logged
    assert "<redacted>" in logged


def test_start_process_uses_isolated_text_subprocess(monkeypatch, capsys) -> None:
    popen = Mock(return_value=Mock())
    monkeypatch.setattr(processes.subprocess, "Popen", popen)
    env = {"PATH": "/bin"}

    started = processes.start_process(["server", "--api-key", "secret"], env=env, label="MCP")

    assert started is popen.return_value
    popen.assert_called_once_with(
        ["server", "--api-key", "secret"], env=env, text=True, start_new_session=True
    )
    output = capsys.readouterr().out
    assert "starting MCP:" in output
    assert "secret" not in output


def test_wait_for_managed_http_server_returns_without_health_url() -> None:
    spec = CodingMCPServerSpec(name="remote", url="https://example.com/mcp", transport="http")
    process = Mock()

    processes.wait_for_managed_http_server(spec, process)

    process.poll.assert_not_called()


def test_wait_for_managed_http_server_reports_early_exit() -> None:
    spec = CodingMCPServerSpec(
        name="managed",
        url="http://127.0.0.1:9000/mcp",
        transport="http",
        command=["server"],
        managed=True,
        health_url="http://127.0.0.1:9000/health",
    )
    process = Mock()
    process.poll.return_value = 7

    with pytest.raises(RuntimeError, match="exited before health check succeeded: code=7"):
        processes.wait_for_managed_http_server(spec, process)


def test_wait_for_processes_returns_first_exit_code(monkeypatch, capsys) -> None:
    process = Mock(pid=123)
    process.poll.return_value = 4
    monkeypatch.setattr(processes.time, "sleep", Mock())

    assert processes.wait_for_processes([process]) == 4
    assert "process exited: pid=123 code=4" in capsys.readouterr().err


def test_stop_process_terminates_then_kills_after_timeout() -> None:
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("server", 5), 0]

    processes.stop_process(process)

    process.send_signal.assert_called_once_with(signal.SIGTERM)
    process.kill.assert_called_once_with()
    assert process.wait.call_args_list[0].kwargs == {"timeout": 5}
    assert process.wait.call_args_list[1].kwargs == {"timeout": 5}
