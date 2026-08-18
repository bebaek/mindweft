from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_agent_wrapper.app import Settings, create_app

RUN_PI_INTEGRATION_ENV = "MINDWEFT_RUN_PI_INTEGRATION_TESTS"
TERMINAL_STATUSES = {"completed", "failed", "canceled"}


@pytest.mark.skipif(
    (
        os.getenv(RUN_PI_INTEGRATION_ENV) or os.getenv("MINIGENT_RUN_PI_INTEGRATION_TESTS", "")
    ).lower()
    not in {"1", "true", "yes"},
    reason=f"set {RUN_PI_INTEGRATION_ENV}=true to run real Pi integration tests",
)
def test_real_pi_task_extracts_final_output() -> None:
    if shutil.which("pi") is None:
        pytest.skip("pi executable not found on PATH")

    workspace = Path(__file__).resolve().parents[2]
    settings = Settings(
        agent_runtime="pi",
        agent_command=("pi",),
        allowed_workspaces=(workspace,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post(
            "/tasks",
            json={
                "cwd": str(workspace),
                "prompt": (
                    "In one short sentence, say what this repository appears to be. "
                    "Do not edit files."
                ),
            },
        )

        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]
        task = _wait_for_terminal_task(client, task_id)
        assert task["status"] == "completed"
        assert task["exit_code"] == 0
        assert isinstance(task["final_output"], str)
        assert task["final_output"].strip()

        events_response = client.get(f"/tasks/{task_id}/events")
        assert events_response.status_code == 200
        assert events_response.json()["events"]


def _wait_for_terminal_task(client: TestClient, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = client.get(f"/tasks/{task_id}").json()
        if result["status"] in TERMINAL_STATUSES:
            return result
        time.sleep(1)
    raise AssertionError(f"Task {task_id} did not finish")
