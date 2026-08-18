from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def load_demo_module() -> ModuleType:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "demo_pi_mcp_broker.py"
    spec = importlib.util.spec_from_file_location("demo_pi_mcp_broker", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_pi_mcp_broker_builds_tool_smoke_prompt(monkeypatch) -> None:
    demo = load_demo_module()
    captured: list[list[str]] = []

    def fake_run_backend_demo(argv: list[str]) -> int:
        captured.append(argv)
        return 0

    monkeypatch.setattr(demo, "run_backend_demo", fake_run_backend_demo)

    exit_code = demo.main(
        [
            "--base-url",
            "http://minigent.test",
            "--expression",
            "2 * 3",
            "--expected-result",
            "6",
        ]
    )

    assert exit_code == 0
    assert captured == [
        [
            "--base-url",
            "http://minigent.test",
            "--user-id",
            "demo-user",
            "--tenant-id",
            "demo-tenant",
            "--message",
            "Use the Mindweft MCP broker calculator tool to calculate 2 * 3, then reply with only the numeric result. Do not edit files.",
            "--expect-reply-contains",
            "6",
        ]
    ]
