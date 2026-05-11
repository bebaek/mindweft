from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def load_check_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "check_peer_agent_demo.py"
    spec = importlib.util.spec_from_file_location("check_peer_agent_demo", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def args_for(tmp_path: Path, *, check_running: bool = False) -> argparse.Namespace:
    wrapper_dir = tmp_path / "local-agent-wrapper"
    workspace = tmp_path / "workspace"
    wrapper_dir.mkdir()
    workspace.mkdir()
    return argparse.Namespace(
        root_dir=str(tmp_path),
        wrapper_dir=str(wrapper_dir),
        workspace=str(workspace),
        agent_runtime="opencode",
        agent_command="opencode",
        agent_host="127.0.0.1",
        agent_port=8010,
        peer_name="opencode",
        minigent_host="127.0.0.1",
        minigent_port=8000,
        check_running=check_running,
        skip_wrapper_health=False,
    )


def test_check_peer_agent_demo_preflight_passes_with_free_ports(monkeypatch, tmp_path) -> None:
    checker = load_check_module()

    monkeypatch.setattr(checker.shutil, "which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr(
        checker.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(
        checker,
        "check_port_free",
        lambda name, host, port: checker.CheckResult(name, True, f"{host}:{port}"),
    )

    results = checker.run_checks(args_for(tmp_path))

    assert all(result.ok for result in results)
    assert [result.name for result in results][-2:] == ["agent wrapper port", "minigent port"]


def test_check_peer_agent_demo_reports_busy_port(monkeypatch, tmp_path) -> None:
    checker = load_check_module()

    monkeypatch.setattr(checker.shutil, "which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr(
        checker.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )

    def fake_check_port_free(name: str, host: str, port: int):
        if port == 8000:
            return checker.CheckResult(name, False, f"{host}:{port} is not available")
        return checker.CheckResult(name, True, f"{host}:{port}")

    monkeypatch.setattr(checker, "check_port_free", fake_check_port_free)

    results = checker.run_checks(args_for(tmp_path))

    assert any(not result.ok and result.name == "minigent port" for result in results)


def test_check_peer_agent_demo_running_mode_validates_config(monkeypatch, tmp_path) -> None:
    checker = load_check_module()

    monkeypatch.setattr(checker.shutil, "which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr(
        checker.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(
        checker,
        "check_url",
        lambda name, url: checker.CheckResult(name, True, url),
    )

    def fake_request_json_result(name: str, url: str):
        if url.endswith("/config"):
            return checker.CheckResult(name, True, url), {
                "local_tools": ["echo", "peer_agent_task"]
            }
        if url.endswith("/peer-agents"):
            return checker.CheckResult(name, True, url), {"agents": [{"name": "opencode"}]}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(checker, "request_json_result", fake_request_json_result)

    results = checker.run_checks(args_for(tmp_path, check_running=True))

    assert all(result.ok for result in results)
    assert any(result.name == "peer_agent_task enabled" for result in results)
    assert any(result.name == "peer configured" for result in results)


def test_check_peer_agent_demo_running_mode_can_skip_direct_wrapper_health(
    monkeypatch, tmp_path
) -> None:
    checker = load_check_module()
    args = args_for(tmp_path, check_running=True)
    args.skip_wrapper_health = True

    monkeypatch.setattr(checker.shutil, "which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr(
        checker.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(
        checker,
        "check_url",
        lambda name, url: (_ for _ in ()).throw(AssertionError("unexpected wrapper check")),
    )
    monkeypatch.setattr(
        checker,
        "request_json_result",
        lambda name, url: (
            checker.CheckResult(name, True, url),
            {"local_tools": ["peer_agent_task"]}
            if url.endswith("/config")
            else {"agents": [{"name": "opencode"}]},
        ),
    )

    results = checker.run_checks(args)

    assert all(result.ok for result in results)
    assert not any(result.name == "agent wrapper health" for result in results)
