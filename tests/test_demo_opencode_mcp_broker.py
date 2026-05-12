from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def load_demo_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "demo_opencode_mcp_broker.py"
    spec = importlib.util.spec_from_file_location("demo_opencode_mcp_broker", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_opencode_mcp_broker_builds_tool_smoke_prompt(monkeypatch) -> None:
    demo = load_demo_module()
    calls: list[list[str]] = []

    def fake_run_backend_demo(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(demo, "run_backend_demo", fake_run_backend_demo)

    exit_code = demo.main(["--base-url", "http://minigent.test", "--smoke-text", "marker"])

    assert exit_code == 0
    assert calls == [
        [
            "--base-url",
            "http://minigent.test",
            "--user-id",
            "demo-user",
            "--tenant-id",
            "demo-tenant",
            "--message",
            "Use the Minigent MCP broker echo tool with text 'marker', then reply with only the echoed text. Do not edit files.",
            "--expect-reply-contains",
            "marker",
        ]
    ]
