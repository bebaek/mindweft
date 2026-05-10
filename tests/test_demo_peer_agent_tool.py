from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any


def load_demo_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "demo_peer_agent_tool.py"
    spec = importlib.util.spec_from_file_location("demo_peer_agent_tool", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_peer_agent_tool_drives_runtime_tool_path(monkeypatch, capsys) -> None:
    demo = load_demo_module()
    calls: list[tuple[str, str, dict[str, Any] | None, dict[str, str] | None]] = []
    captured_message: str | None = None

    def fake_request_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        del timeout
        nonlocal captured_message
        calls.append((method, url, payload, headers))
        if method == "GET" and url == "http://minigent.test/config":
            return {"local_tools": ["echo", "peer_agent_task"]}
        if method == "GET" and url == "http://minigent.test/peer-agents":
            return {"agents": [{"name": "codex"}]}
        if method == "POST" and url == "http://minigent.test/threads":
            return {"thread_id": "thread_123"}
        if method == "POST" and url == "http://minigent.test/threads/thread_123/messages":
            assert payload is not None
            captured_message = str(payload["content"])
            return {"id": "message_123"}
        if method == "POST" and url == "http://minigent.test/threads/thread_123/run":
            return {"reply": 'Tool result: {"status":"completed"}'}
        if method == "GET" and url == "http://minigent.test/threads/thread_123/messages":
            return [
                {"role": "user", "content": captured_message},
                {"role": "assistant", "content": "", "tool_name": "peer_agent_task"},
                {
                    "role": "tool",
                    "content": '{"status":"completed"}',
                    "tool_name": "peer_agent_task",
                },
                {"role": "assistant", "content": 'Tool result: {"status":"completed"}'},
            ]
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
            "--poll-interval",
            "0.5",
        ]
    )

    assert exit_code == 0
    assert captured_message is not None
    assert captured_message.startswith("/tool peer_agent_task ")
    tool_payload = json.loads(captured_message.removeprefix("/tool peer_agent_task "))
    assert tool_payload == {
        "peer": "codex",
        "cwd": "/workspace/project",
        "prompt": "summarize",
        "poll": True,
        "timeout_seconds": 180.0,
        "poll_interval_seconds": 0.5,
    }
    assert calls[2] == (
        "POST",
        "http://minigent.test/threads",
        None,
        {
            "X-Minigent-User-Id": "demo-user",
            "X-Minigent-Tenant-Id": "demo-tenant",
            "X-Minigent-Admin": "false",
        },
    )
    output = capsys.readouterr().out
    assert "local_tools: ['echo', 'peer_agent_task']" in output
    assert "assistant: Tool result:" in output


def test_demo_peer_agent_tool_reports_disabled_tool(monkeypatch, capsys) -> None:
    demo = load_demo_module()

    def fake_request_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        del payload, headers, timeout
        if method == "GET" and url == "http://minigent.test/config":
            return {"local_tools": ["echo"]}
        if method == "GET" and url == "http://minigent.test/peer-agents":
            return {"agents": [{"name": "codex"}]}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)

    exit_code = demo.main(["--base-url", "http://minigent.test"])

    assert exit_code == 2
    assert "peer_agent_task is not enabled" in capsys.readouterr().err


def test_demo_peer_agent_tool_fails_when_peer_task_fails(monkeypatch, capsys) -> None:
    demo = load_demo_module()

    def fake_request_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        del payload, headers, timeout
        if method == "GET" and url == "http://minigent.test/config":
            return {"local_tools": ["peer_agent_task"]}
        if method == "GET" and url == "http://minigent.test/peer-agents":
            return {"agents": [{"name": "codex"}]}
        if method == "POST" and url == "http://minigent.test/threads":
            return {"thread_id": "thread_123"}
        if method == "POST" and url == "http://minigent.test/threads/thread_123/messages":
            return {"id": "message_123"}
        if method == "POST" and url == "http://minigent.test/threads/thread_123/run":
            return {"reply": 'Tool result: {"status":"failed"}'}
        if method == "GET" and url == "http://minigent.test/threads/thread_123/messages":
            return [
                {"role": "user", "content": "/tool peer_agent_task {}"},
                {
                    "role": "tool",
                    "content": '{"status":"failed","exit_code":1}',
                    "tool_name": "peer_agent_task",
                },
                {"role": "assistant", "content": 'Tool result: {"status":"failed"}'},
            ]
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)

    exit_code = demo.main(["--base-url", "http://minigent.test"])

    assert exit_code == 1
    assert "peer_agent_task did not complete successfully" in capsys.readouterr().err
