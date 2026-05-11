from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RUN_COMPOSE_INTEGRATION_ENV = "MINIGENT_RUN_COMPOSE_INTEGRATION_TESTS"


pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.getenv(RUN_COMPOSE_INTEGRATION_ENV, "").strip().lower() not in {"1", "true", "yes", "on"},
    reason=f"Set {RUN_COMPOSE_INTEGRATION_ENV}=true to run peer-agent Compose integration tests",
)
def test_peer_agent_tool_compose_demo_completes() -> None:
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "MINIGENT_PORT": os.getenv("MINIGENT_COMPOSE_TEST_PORT", "18080"),
    }
    prompt = "Reply exactly: compose-opencode-ok"

    completed = subprocess.run(
        ["./scripts/demo_peer_agent_tool_compose.sh", prompt],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "Prepared " in output
    assert "[ok] minigent config:" in completed.stdout
    assert "[ok] minigent peer agents:" in completed.stdout
    assert "local_tools:" in completed.stdout
    assert "peer_agent_task" in completed.stdout
    assert "transcript:" in completed.stdout
    assert "- assistant (peer_agent_task):" in completed.stdout
    assert "- tool (peer_agent_task):" in completed.stdout
    assert '"status": "completed"' in completed.stdout
    assert '"timed_out": false' in completed.stdout
    assert "compose-opencode-ok" in completed.stdout
