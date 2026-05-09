from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


def load_demo_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "demo_peer_agent.py"
    spec = importlib.util.spec_from_file_location("demo_peer_agent", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_peer_agent_submits_and_polls_through_minigent(
    monkeypatch,
    capsys,
) -> None:
    demo = load_demo_module()
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append((method, url, payload))
        if method == "GET" and url == "http://minigent.test/health":
            return {"status": "ok"}
        if method == "GET" and url == "http://minigent.test/peer-agents":
            return {"agents": [{"name": "codex"}]}
        if method == "GET" and url == "http://minigent.test/peer-agents/codex/agent-card":
            return {"name": "codex-coding-agent"}
        if method == "POST" and url == "http://minigent.test/peer-agents/codex/tasks":
            assert payload == {"cwd": "/workspace/project", "prompt": "summarize"}
            return {"task_id": "task_123", "status": "running"}
        if method == "GET" and url == "http://minigent.test/peer-agents/codex/tasks/task_123":
            return {
                "task_id": "task_123",
                "status": "completed",
                "exit_code": 0,
                "final_output": "summary",
            }
        if (
            method == "GET"
            and url == "http://minigent.test/peer-agents/codex/tasks/task_123/events"
        ):
            return {
                "task_id": "task_123",
                "next_index": 1,
                "events": [{"index": 0, "type": "message.completed"}],
            }
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)

    exit_code = demo.main(
        [
            "--base-url",
            "http://minigent.test",
            "--cwd",
            "/workspace/project",
            "--prompt",
            "summarize",
            "--show-events",
        ]
    )

    assert exit_code == 0
    assert calls == [
        ("GET", "http://minigent.test/health", None),
        ("GET", "http://minigent.test/peer-agents", None),
        ("GET", "http://minigent.test/peer-agents/codex/agent-card", None),
        (
            "POST",
            "http://minigent.test/peer-agents/codex/tasks",
            {"cwd": "/workspace/project", "prompt": "summarize"},
        ),
        ("GET", "http://minigent.test/peer-agents/codex/tasks/task_123", None),
        ("GET", "http://minigent.test/peer-agents/codex/tasks/task_123/events", None),
    ]
    output = capsys.readouterr().out
    assert "submitted: task_123" in output
    assert '"type": "message.completed"' in output


def test_demo_peer_agent_returns_failure_for_failed_task(monkeypatch, capsys) -> None:
    demo = load_demo_module()

    def fake_request_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del payload
        if method == "GET" and url.endswith("/health"):
            return {"status": "ok"}
        if method == "GET" and url.endswith("/peer-agents"):
            return {"agents": [{"name": "codex"}]}
        if method == "GET" and url.endswith("/agent-card"):
            return {"name": "codex-coding-agent"}
        if method == "POST" and url.endswith("/tasks"):
            return {"task_id": "task_123", "status": "running"}
        if method == "GET" and url.endswith("/tasks/task_123"):
            return {"task_id": "task_123", "status": "failed", "exit_code": 2}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)

    exit_code = demo.main(["--base-url", "http://minigent.test"])

    assert exit_code == 1
    assert "status: failed" in capsys.readouterr().out


def test_demo_peer_agent_can_cancel_after_delay(monkeypatch, capsys) -> None:
    demo = load_demo_module()
    task_polls = 0
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal task_polls
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/health"):
            return {"status": "ok"}
        if method == "GET" and url.endswith("/peer-agents"):
            return {"agents": [{"name": "codex"}]}
        if method == "GET" and url.endswith("/agent-card"):
            return {"name": "codex-coding-agent"}
        if method == "POST" and url.endswith("/tasks"):
            return {"task_id": "task_123", "status": "running"}
        if method == "GET" and url.endswith("/tasks/task_123"):
            task_polls += 1
            if task_polls == 1:
                return {"task_id": "task_123", "status": "running"}
            return {"task_id": "task_123", "status": "canceled", "exit_code": -2}
        if method == "POST" and url.endswith("/tasks/task_123/cancel"):
            return {"task_id": "task_123", "status": "canceling"}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)
    monkeypatch.setattr(demo.time, "sleep", lambda seconds: None)

    exit_code = demo.main(
        [
            "--base-url",
            "http://minigent.test",
            "--cancel-after",
            "0",
        ]
    )

    assert exit_code == 1
    assert (
        "POST",
        "http://minigent.test/peer-agents/codex/tasks/task_123/cancel",
        None,
    ) in calls
    output = capsys.readouterr().out
    assert "cancel_requested: task_123" in output
    assert "status: canceled" in output
