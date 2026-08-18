from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, call

import pytest

from minigent_workspace import orchestration
from minigent_workspace.mcp_specs import CodingMCPServerSpec


def run_processes(
    tmp_path: Path,
    specs: list[CodingMCPServerSpec],
    **overrides,
) -> int:
    options = {
        "env": {"PATH": "/bin"},
        "mcp_server_specs": specs,
        "skip_bridge": False,
        "gateway_enabled": False,
        "bridge_host": "127.0.0.1",
        "gateway_port": 8765,
        "skip_api": True,
        "api_host": "127.0.0.1",
        "api_port": 8000,
        "tenant_id": "demo-tenant",
        "workspace": tmp_path,
        "bridge_name": "fs-workspace",
        "text_bridge_name": None,
        "shell_bridge_name": None,
    }
    options.update(overrides)
    return orchestration.run_workspace_processes(**options)


def test_run_workspace_processes_starts_stdio_bridges_and_stops_them(
    tmp_path: Path, monkeypatch
) -> None:
    spec = CodingMCPServerSpec(
        name="fs-workspace",
        url="http://127.0.0.1:8765/mcp",
        command=["server"],
        env={"SERVER_VALUE": "enabled"},
    )
    process = Mock()
    start = Mock(return_value=process)
    wait = Mock(return_value=7)
    stop = Mock()
    monkeypatch.setattr(orchestration, "start_process", start)
    monkeypatch.setattr(orchestration, "wait_for_processes", wait)
    monkeypatch.setattr(orchestration, "stop_process", stop)
    monkeypatch.setattr(
        orchestration, "build_mcp_stdio_bridge_command", Mock(return_value=["bridge"])
    )

    assert run_processes(tmp_path, [spec]) == 7

    start.assert_called_once_with(
        ["bridge"],
        env={"PATH": "/bin", "SERVER_VALUE": "enabled"},
        label="fs-workspace MCP bridge",
    )
    wait.assert_called_once_with([process])
    stop.assert_called_once_with(process)


def test_run_workspace_processes_starts_managed_http_before_waiting(
    tmp_path: Path, monkeypatch
) -> None:
    spec = CodingMCPServerSpec(
        name="managed",
        url="http://127.0.0.1:9000/mcp",
        transport="http",
        command=["managed-server"],
        env={"TOKEN": "value"},
        managed=True,
    )
    process = Mock()
    start = Mock(return_value=process)
    health = Mock()
    monkeypatch.setattr(orchestration, "start_process", start)
    monkeypatch.setattr(orchestration, "wait_for_managed_http_server", health)
    monkeypatch.setattr(orchestration, "wait_for_processes", Mock(return_value=0))
    monkeypatch.setattr(orchestration, "stop_process", Mock())

    assert run_processes(tmp_path, [spec]) == 0

    start.assert_called_once_with(
        ["managed-server"],
        env={"PATH": "/bin", "TOKEN": "value"},
        label="managed MCP HTTP server",
    )
    health.assert_called_once_with(spec, process)


def test_run_workspace_processes_rejects_managed_http_without_command(
    tmp_path: Path, monkeypatch
) -> None:
    spec = CodingMCPServerSpec(
        name="managed",
        url="http://127.0.0.1:9000/mcp",
        transport="http",
        managed=True,
    )
    monkeypatch.setattr(orchestration, "stop_process", Mock())

    with pytest.raises(RuntimeError, match="requires a command"):
        run_processes(tmp_path, [spec])


def test_run_workspace_processes_removes_generated_gateway_config(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "gateway.json"
    config_path.write_text("{}", encoding="utf-8")
    process = Mock()
    monkeypatch.setattr(orchestration, "write_mcp_gateway_config", Mock(return_value=config_path))
    monkeypatch.setattr(orchestration, "build_mcp_gateway_command", Mock(return_value=["gateway"]))
    monkeypatch.setattr(orchestration, "start_process", Mock(return_value=process))
    monkeypatch.setattr(orchestration, "wait_for_processes", Mock(return_value=0))
    stop = Mock()
    monkeypatch.setattr(orchestration, "stop_process", stop)

    assert run_processes(tmp_path, [], gateway_enabled=True) == 0

    assert not config_path.exists()
    stop.assert_called_once_with(process)


def test_run_workspace_processes_starts_api_and_prints_demo_commands(
    tmp_path: Path, monkeypatch
) -> None:
    process = Mock()
    start = Mock(return_value=process)
    demo = Mock()
    monkeypatch.setattr(orchestration, "start_process", start)
    monkeypatch.setattr(orchestration, "print_demo_commands", demo)
    monkeypatch.setattr(orchestration, "wait_for_processes", Mock(return_value=0))
    monkeypatch.setattr(orchestration, "stop_process", Mock())

    assert (
        run_processes(
            tmp_path,
            [],
            skip_bridge=True,
            skip_api=False,
            text_bridge_name="text-workspace",
            shell_bridge_name="shell-workspace",
        )
        == 0
    )

    start.assert_called_once_with(
        [
            orchestration.sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        env={"PATH": "/bin"},
        label="Mindweft API",
    )
    demo.assert_called_once_with(
        "127.0.0.1",
        8000,
        "demo-tenant",
        tmp_path,
        "fs-workspace",
        "text-workspace",
        "shell-workspace",
    )


def test_run_workspace_processes_handles_interrupt_and_stops_reverse_order(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    first = Mock()
    second = Mock()
    monkeypatch.setattr(orchestration, "start_process", Mock(side_effect=[first, second]))
    monkeypatch.setattr(
        orchestration,
        "build_mcp_stdio_bridge_command",
        Mock(side_effect=[["first"], ["second"]]),
    )
    monkeypatch.setattr(orchestration, "wait_for_processes", Mock(side_effect=KeyboardInterrupt))
    stop = Mock()
    monkeypatch.setattr(orchestration, "stop_process", stop)
    specs = [
        CodingMCPServerSpec(name="first", url="http://first/mcp", command=["first"]),
        CodingMCPServerSpec(name="second", url="http://second/mcp", command=["second"]),
    ]

    assert run_processes(tmp_path, specs) == 0

    assert stop.call_args_list == [call(second), call(first)]
    assert "Stopping coding workspace processes" in capsys.readouterr().out
