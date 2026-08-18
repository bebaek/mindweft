from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


def load_demo_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "demo_pi_backend.py"
    spec = importlib.util.spec_from_file_location("demo_pi_backend", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_pi_backend_drives_run_endpoint(monkeypatch, capsys) -> None:
    demo = load_demo_module()
    calls: list[tuple[str, str, dict[str, Any] | None, dict[str, str] | None]] = []

    def fake_request_json(
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        del timeout
        calls.append((method, url, payload, headers))
        if method == "GET" and url == "http://minigent.test/config":
            return {
                "agent_backend": {
                    "type": "peer_agent",
                    "peer": "pi",
                    "cwd": "/workspace/project",
                    "timeout_seconds": 180.0,
                }
            }
        if method == "POST" and url == "http://minigent.test/threads":
            return {"thread_id": "thread_123"}
        if method == "POST" and url == "http://minigent.test/threads/thread_123/messages":
            assert payload == {"content": "summarize"}
            return {"id": "message_123"}
        if method == "POST" and url == "http://minigent.test/threads/thread_123/run":
            return {"reply": "Pi summary"}
        if method == "GET" and url == "http://minigent.test/threads/thread_123/messages":
            raise AssertionError("Transcript should not be fetched unless --show-content is set")
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)

    exit_code = demo.main(
        [
            "--base-url",
            "http://minigent.test",
            "--message",
            "summarize",
        ]
    )

    assert exit_code == 0
    assert calls[1] == (
        "POST",
        "http://minigent.test/threads",
        None,
        {
            "X-Mindweft-User-Id": "demo-user",
            "X-Mindweft-Tenant-Id": "demo-tenant",
            "X-Mindweft-Admin": "false",
        },
    )
    output = capsys.readouterr().out
    assert "agent_backend: {'type': 'peer_agent'" in output
    assert "assistant: <redacted length=10>" in output
    assert "Pi summary" not in output


def test_demo_pi_backend_show_content_prints_transcript(monkeypatch, capsys) -> None:
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
            return {"agent_backend": {"type": "peer_agent", "peer": "pi", "timeout_seconds": 180.0}}
        if method == "POST" and url == "http://minigent.test/threads":
            return {"thread_id": "thread_123"}
        if method == "POST" and url.endswith("/messages"):
            return {"id": "message_123"}
        if method == "POST" and url.endswith("/run"):
            return {"reply": "Pi summary"}
        if method == "GET" and url.endswith("/messages"):
            return [
                {"role": "user", "content": "summarize"},
                {"role": "assistant", "content": "Pi summary"},
            ]
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)

    exit_code = demo.main(["--base-url", "http://minigent.test", "--show-content"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "assistant: Pi summary" in output
    assert "- user: summarize" in output


def test_demo_pi_backend_reports_native_backend(monkeypatch, capsys) -> None:
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
            return {"agent_backend": {"type": "native"}}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)

    exit_code = demo.main(["--base-url", "http://minigent.test"])

    assert exit_code == 2
    assert "Mindweft is not configured for the peer_agent backend" in capsys.readouterr().err


def test_demo_pi_backend_warns_for_non_pi_peer(monkeypatch, capsys) -> None:
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
            return {"agent_backend": {"type": "peer_agent", "peer": "opencode"}}
        if method == "POST" and url == "http://minigent.test/threads":
            return {"thread_id": "thread_123"}
        if method == "POST" and url.endswith("/messages"):
            return {"id": "message_123"}
        if method == "POST" and url.endswith("/run"):
            return {"reply": "reply"}
        if method == "GET" and url.endswith("/messages"):
            return []
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(demo, "request_json", fake_request_json)

    exit_code = demo.main(["--base-url", "http://minigent.test"])

    assert exit_code == 0
    assert "warning: configured peer is 'opencode', not 'pi'" in capsys.readouterr().err
