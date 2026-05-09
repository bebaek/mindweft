from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RUN_INTEGRATION_ENV = "MINIGENT_RUN_INTEGRATION_TESTS"


pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.getenv(RUN_INTEGRATION_ENV, "").strip().lower() not in {"1", "true", "yes", "on"},
    reason=f"Set {RUN_INTEGRATION_ENV}=true to run peer-agent integration tests",
)
def test_peer_agent_tool_stack_demo_completes() -> None:
    root = Path(__file__).resolve().parents[1]
    prompt = "Summarize this repository in one sentence. Do not edit files."

    completed = subprocess.run(
        ["./scripts/demo_peer_agent_tool_stack.sh", prompt],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "[ok] codex wrapper port:" in completed.stdout
    assert "[ok] minigent port:" in completed.stdout
    assert "local_tools:" in completed.stdout
    assert "peer_agent_task" in completed.stdout
    assert "transcript:" in completed.stdout
    assert "- assistant (peer_agent_task):" in completed.stdout
    assert "- tool (peer_agent_task):" in completed.stdout
    assert '"status": "completed"' in completed.stdout
    assert '"timed_out": false' in completed.stdout
