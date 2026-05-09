from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from codex_agent_wrapper.app import Settings, create_app


def test_agent_card() -> None:
    with TestClient(create_app(Settings(allowed_workspaces=(Path.cwd(),)))) as client:
        response = client.get("/agent-card")

        assert response.status_code == 200
        assert response.json()["name"] == "codex-coding-agent"


def test_task_captures_stdout_and_stderr(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        "import sys\nprint('stdout: ' + sys.argv[-1])\nprint('stderr: warning', file=sys.stderr)\n",
        encoding="utf-8",
    )
    settings = Settings(
        codex_command=(sys.executable, str(fake_codex)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]
        result = _wait_for_terminal_task(client, task_id)
        assert result["status"] == "completed"
        assert result["exit_code"] == 0
        assert "stdout: hello" in result["stdout_tail"]
        assert "stderr: warning" in result["stderr_tail"]


def test_task_parses_jsonl_events_and_final_output(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        "import json\n"
        "import sys\n"
        "print(json.dumps({'type': 'task.started', 'message': {'role': 'system', 'content': 'started'}}))\n"
        "print(json.dumps({'type': 'message.completed', 'message': {'role': 'assistant', 'content': 'final answer'}}))\n"
        "print('debug log', file=sys.stderr)\n",
        encoding="utf-8",
    )
    settings = Settings(
        codex_command=(sys.executable, str(fake_codex)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]
        result = _wait_for_terminal_task(client, task_id)
        assert result["status"] == "completed"
        assert result["final_output"] == "final answer"
        assert len(result["events_tail"]) == 2
        assert result["events_tail"][0]["index"] == 0
        assert result["events_tail"][1]["index"] == 1
        assert result["events_tail"][1]["type"] == "message.completed"
        assert "message.completed" in result["stdout_tail"]
        assert "debug log" in result["stderr_tail"]


def test_task_events_endpoint_supports_incremental_polling(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    fake_codex.write_text(
        "import json\n"
        "print(json.dumps({'type': 'task.started'}))\n"
        "print(json.dumps({'type': 'message.completed', 'message': {'role': 'assistant', 'content': 'done'}}))\n",
        encoding="utf-8",
    )
    settings = Settings(
        codex_command=(sys.executable, str(fake_codex)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]
        _wait_for_terminal_task(client, task_id)

        all_events_response = client.get(f"/tasks/{task_id}/events")
        assert all_events_response.status_code == 200
        all_events = all_events_response.json()
        assert all_events["task_id"] == task_id
        assert all_events["next_index"] == 2
        assert [event["index"] for event in all_events["events"]] == [0, 1]

        incremental_response = client.get(f"/tasks/{task_id}/events?after=0")
        assert incremental_response.status_code == 200
        incremental_events = incremental_response.json()
        assert incremental_events["next_index"] == 2
        assert [event["index"] for event in incremental_events["events"]] == [1]

        empty_response = client.get(f"/tasks/{task_id}/events?after=1")
        assert empty_response.status_code == 200
        assert empty_response.json()["next_index"] == 2
        assert empty_response.json()["events"] == []


def test_task_events_endpoint_returns_404_for_unknown_task() -> None:
    with TestClient(create_app(Settings(allowed_workspaces=(Path.cwd(),)))) as client:
        response = client.get("/tasks/missing/events")

        assert response.status_code == 404


def test_task_rejects_workspace_outside_allowlist(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    with TestClient(create_app(Settings(allowed_workspaces=(allowed,)))) as client:
        response = client.post("/tasks", json={"cwd": str(denied), "prompt": "hello"})

        assert response.status_code == 403


def test_task_cancel_sends_signal(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake_codex.py"
    marker = tmp_path / "interrupted"
    ready = tmp_path / "ready"
    fake_codex.write_text(
        "import signal\n"
        "import sys\n"
        "import time\n"
        f"marker = {str(marker)!r}\n"
        f"ready = {str(ready)!r}\n"
        "def handle_sigint(signum, frame):\n"
        "    open(marker, 'w', encoding='utf-8').write('interrupted')\n"
        "    sys.exit(130)\n"
        "signal.signal(signal.SIGINT, handle_sigint)\n"
        "open(ready, 'w', encoding='utf-8').write('ready')\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    settings = Settings(
        codex_command=(sys.executable, str(fake_codex)),
        allowed_workspaces=(tmp_path,),
        cancel_grace_seconds=0.1,
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "sleep"})

        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]
        _wait_for_status(client, task_id, "running")
        _wait_for_file(ready)
        cancel_response = client.post(f"/tasks/{task_id}/cancel")

        assert cancel_response.status_code == 200
        assert cancel_response.json()["status"] == "canceled"
        assert marker.read_text(encoding="utf-8") == "interrupted"


def _wait_for_status(client: TestClient, task_id: str, status: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/tasks/{task_id}").json()
        if result["status"] == status:
            return result
        time.sleep(0.05)
    raise AssertionError(f"Task {task_id} did not reach {status}")


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} was not created")


def _wait_for_terminal_task(client: TestClient, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    terminal = {"completed", "failed", "canceled"}
    while time.monotonic() < deadline:
        result = client.get(f"/tasks/{task_id}").json()
        if result["status"] in terminal:
            return result
        time.sleep(0.05)
    raise AssertionError(f"Task {task_id} did not finish")
