from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from local_agent_wrapper.app import Settings, _pi_mcp_broker_tool_names, create_app


def test_agent_card() -> None:
    with TestClient(create_app(Settings(allowed_workspaces=(Path.cwd(),)))) as client:
        response = client.get("/agent-card")

        assert response.status_code == 200
        assert response.json()["name"] == "pi-coding-agent"


def test_task_captures_stdout_and_stderr(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        "import sys\nprint('stdout: ' + sys.argv[-1])\nprint('stderr: warning', file=sys.stderr)\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]
        result = _wait_for_terminal_task(client, task_id)
        assert result["status"] == "completed"
        assert result["exit_code"] == 0
        assert result["links"] == {
            "self": f"/tasks/{task_id}",
            "events": f"/tasks/{task_id}/events",
            "cancel": f"/tasks/{task_id}/cancel",
        }
        assert result["artifacts"] == {
            "final_output": f"/tasks/{task_id}/artifacts/final-output",
            "stdout_tail": f"/tasks/{task_id}/artifacts/stdout-tail",
            "stderr_tail": f"/tasks/{task_id}/artifacts/stderr-tail",
            "events": f"/tasks/{task_id}/artifacts/events",
        }
        assert "stdout: hello" in result["stdout_tail"]
        assert "stderr: warning" in result["stderr_tail"]


def test_create_task_response_includes_links_and_artifacts(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text("print('ok')\n", encoding="utf-8")
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        body = create_response.json()
        task_id = body["task_id"]
        assert body["links"]["self"] == f"/tasks/{task_id}"
        assert body["links"]["events"] == f"/tasks/{task_id}/events"
        assert body["links"]["cancel"] == f"/tasks/{task_id}/cancel"
        assert body["artifacts"]["final_output"] == f"/tasks/{task_id}/artifacts/final-output"
        assert body["artifacts"]["events"] == f"/tasks/{task_id}/artifacts/events"


def test_opencode_runtime_uses_run_prompt_argv(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    argv_file = tmp_path / "argv.json"
    fake_agent.write_text(
        "import json\n"
        "import sys\n"
        f"open({str(argv_file)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "print('done')\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
        agent_runtime="opencode",
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert json.loads(argv_file.read_text(encoding="utf-8")) == [
            "run",
            "--format",
            "json",
            "--dir",
            str(tmp_path),
            "hello",
        ]


def test_pi_runtime_uses_json_mode_argv(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    argv_file = tmp_path / "argv.json"
    fake_agent.write_text(
        "import json\n"
        "import sys\n"
        f"open({str(argv_file)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "print('done')\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
        agent_runtime="pi",
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert json.loads(argv_file.read_text(encoding="utf-8")) == [
            "--mode",
            "json",
            "--no-session",
            "--tools",
            "read,grep,find,ls",
            "hello",
        ]


def test_pi_runtime_uses_configured_local_tools(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    argv_file = tmp_path / "argv.json"
    fake_agent.write_text(
        "import json\n"
        "import sys\n"
        f"open({str(argv_file)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "print('done')\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
        agent_runtime="pi",
        pi_tools=("read", "grep", "write", "edit", "bash"),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert json.loads(argv_file.read_text(encoding="utf-8")) == [
            "--mode",
            "json",
            "--no-session",
            "--tools",
            "read,grep,write,edit,bash",
            "hello",
        ]


def test_pi_runtime_adds_mcp_broker_extension_when_env_is_present(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    argv_file = tmp_path / "argv.json"
    fake_agent.write_text(
        "import json\n"
        "import sys\n"
        f"open({str(argv_file)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "print('done')\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
        agent_runtime="pi",
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post(
            "/tasks",
            json={
                "cwd": str(tmp_path),
                "prompt": "hello",
                "env": {
                    "MINIGENT_MCP_BROKER_URL": "http://127.0.0.1:8000/mcp/peer/session",
                    "MINIGENT_MCP_BROKER_TOKEN": "token-123",
                },
            },
        )

        assert create_response.status_code == 200
        _wait_for_terminal_task(client, create_response.json()["task_id"])
        argv = json.loads(argv_file.read_text(encoding="utf-8"))
        assert argv[:3] == ["--mode", "json", "--no-session"]
        assert argv[3] == "--extension"
        extension_path = Path(argv[4])
        assert extension_path.read_text(encoding="utf-8").startswith(
            'import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";'
        )
        assert argv[5:] == ["--tools", "read,grep,find,ls", "hello"]


def test_pi_mcp_broker_tool_names_lists_remote_tools() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            assert self.headers["authorization"] == "Bearer token-123"
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "minigent-tool-list",
                        "result": {
                            "tools": [
                                {"name": "calculator"},
                                {"name": "echo"},
                            ]
                        },
                    }
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _pi_mcp_broker_tool_names(
            {
                "MINIGENT_MCP_BROKER_URL": f"http://127.0.0.1:{server.server_port}/mcp/peer/session",
                "MINIGENT_MCP_BROKER_TOKEN": "token-123",
            }
        ) == ["minigent_calculator", "minigent_echo"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_custom_args_template_overrides_runtime_argv(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    argv_file = tmp_path / "argv.json"
    fake_agent.write_text(
        "import json\n"
        "import sys\n"
        f"open({str(argv_file)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n"
        "print('done')\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
        agent_args_template=("--workspace", "{cwd}", "--message", "{prompt}"),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert json.loads(argv_file.read_text(encoding="utf-8")) == [
            "--workspace",
            str(tmp_path),
            "--message",
            "hello",
        ]


def test_task_env_allows_only_configured_prefixes(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    env_file = tmp_path / "env.json"
    fake_agent.write_text(
        "import json\n"
        "import os\n"
        f"open({str(env_file)!r}, 'w', encoding='utf-8').write(json.dumps({{\n"
        "    'broker_url': os.getenv('MINIGENT_MCP_BROKER_URL'),\n"
        "    'secret': os.getenv('SECRET_VALUE'),\n"
        "}))\n"
        "print('done')\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post(
            "/tasks",
            json={
                "cwd": str(tmp_path),
                "prompt": "hello",
                "env": {
                    "MINIGENT_MCP_BROKER_URL": "http://127.0.0.1:8000/mcp/peer/session",
                    "SECRET_VALUE": "must-not-pass",
                },
            },
        )

        assert create_response.status_code == 200
        _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert json.loads(env_file.read_text(encoding="utf-8")) == {
            "broker_url": "http://127.0.0.1:8000/mcp/peer/session",
            "secret": None,
        }


def test_task_env_generates_opencode_mcp_config(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    env_file = tmp_path / "env.json"
    fake_agent.write_text(
        "import json\n"
        "import os\n"
        f"open({str(env_file)!r}, 'w', encoding='utf-8').write(os.environ['OPENCODE_CONFIG_CONTENT'])\n"
        "print('done')\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post(
            "/tasks",
            json={
                "cwd": str(tmp_path),
                "prompt": "hello",
                "env": {
                    "MINIGENT_MCP_BROKER_URL": "http://127.0.0.1:8000/mcp/peer/session",
                    "MINIGENT_MCP_BROKER_TOKEN": "token-123",
                },
            },
        )

        assert create_response.status_code == 200
        _wait_for_terminal_task(client, create_response.json()["task_id"])
        config = json.loads(env_file.read_text(encoding="utf-8"))

    assert config["mcp"]["minigent"] == {
        "type": "remote",
        "url": "{env:MINIGENT_MCP_BROKER_URL}",
        "enabled": True,
        "oauth": False,
        "headers": {"Authorization": "Bearer {env:MINIGENT_MCP_BROKER_TOKEN}"},
    }


def test_task_env_preserves_existing_opencode_config_content(tmp_path: Path, monkeypatch) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    env_file = tmp_path / "env.json"
    fake_agent.write_text(
        "import os\n"
        f"open({str(env_file)!r}, 'w', encoding='utf-8').write(os.environ['OPENCODE_CONFIG_CONTENT'])\n"
        "print('done')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        json.dumps(
            {
                "model": "test/model",
                "mcp": {"other": {"type": "remote", "url": "https://example.com"}},
            }
        ),
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post(
            "/tasks",
            json={
                "cwd": str(tmp_path),
                "prompt": "hello",
                "env": {
                    "MINIGENT_MCP_BROKER_URL": "http://127.0.0.1:8000/mcp/peer/session",
                    "MINIGENT_MCP_BROKER_TOKEN": "token-123",
                },
            },
        )

        assert create_response.status_code == 200
        _wait_for_terminal_task(client, create_response.json()["task_id"])
        config = json.loads(env_file.read_text(encoding="utf-8"))

    assert config["model"] == "test/model"
    assert config["mcp"]["other"] == {"type": "remote", "url": "https://example.com"}
    assert config["mcp"]["minigent"]["type"] == "remote"


def test_task_parses_jsonl_events_and_final_output(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        "import json\n"
        "import sys\n"
        "print(json.dumps({'type': 'task.started', 'message': {'role': 'system', 'content': 'started'}}))\n"
        "print(json.dumps({'type': 'message.completed', 'message': {'role': 'assistant', 'content': 'final answer'}}))\n"
        "print('debug log', file=sys.stderr)\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
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


def test_task_extracts_pi_message_end_events_as_final_output(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        "import json\n"
        "print(json.dumps({'type': 'session', 'version': 3}))\n"
        "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'pi final'}], 'usage': {'input': 10, 'output': 4, 'cacheRead': 3, 'cacheWrite': 2, 'totalTokens': 14, 'cost': 0.001}}}))\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
        agent_runtime="pi",
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        result = _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert result["status"] == "completed"
        assert result["final_output"] == "pi final"
        assert result["usage"] == {
            "prompt_tokens": 10,
            "input_tokens": 10,
            "completion_tokens": 4,
            "output_tokens": 4,
            "total_tokens": 14,
            "cache_read_tokens": 3,
            "cache_write_tokens": 2,
            "cost": 0.001,
        }


def test_task_extracts_pi_turn_usage_as_total_usage(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        "import json\n"
        "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'content': 'pi final', 'usage': {'input': 10, 'output': 4, 'totalTokens': 14}}}))\n"
        "print(json.dumps({'type': 'turn_end', 'usage': {'input': 12, 'output': 5, 'totalTokens': 17}}))\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
        agent_runtime="pi",
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        result = _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert result["status"] == "completed"
        assert result["final_output"] == "pi final"
        assert result["usage"]["total_tokens"] == 17


def test_task_extracts_opencode_text_events_as_final_output(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        "import json\n"
        "print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': 'working'}}))\n"
        "print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': 'final answer'}}))\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        result = _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert result["status"] == "completed"
        assert result["final_output"] == "final answer"


def test_task_parses_jsonl_events_longer_than_stream_reader_line_limit(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    long_final = "x" * 70000
    fake_agent.write_text(
        "import json\n"
        f"print(json.dumps({{'type': 'message.completed', 'message': {{'role': 'assistant', 'content': {long_final!r}}}}}))\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        result = _wait_for_terminal_task(client, create_response.json()["task_id"])
        assert result["status"] == "completed"
        assert result["final_output"] == long_final


def test_task_events_endpoint_supports_incremental_polling(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        "import json\n"
        "print(json.dumps({'type': 'task.started'}))\n"
        "print(json.dumps({'type': 'message.completed', 'message': {'role': 'assistant', 'content': 'done'}}))\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
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


def test_task_artifact_endpoints_return_outputs_and_events(tmp_path: Path) -> None:
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        "import json\n"
        "import sys\n"
        "print(json.dumps({'type': 'message.completed', 'message': {'role': 'assistant', 'content': 'artifact final'}}))\n"
        "print('artifact stderr', file=sys.stderr)\n",
        encoding="utf-8",
    )
    settings = Settings(
        agent_command=(sys.executable, str(fake_agent)),
        allowed_workspaces=(tmp_path,),
    )
    with TestClient(create_app(settings)) as client:
        create_response = client.post("/tasks", json={"cwd": str(tmp_path), "prompt": "hello"})

        assert create_response.status_code == 200
        task_id = create_response.json()["task_id"]
        _wait_for_terminal_task(client, task_id)

        final_output = client.get(f"/tasks/{task_id}/artifacts/final-output")
        assert final_output.status_code == 200
        assert final_output.text == "artifact final"
        assert final_output.headers["content-type"].startswith("text/plain")

        stdout_tail = client.get(f"/tasks/{task_id}/artifacts/stdout-tail")
        assert stdout_tail.status_code == 200
        assert "message.completed" in stdout_tail.text

        stderr_tail = client.get(f"/tasks/{task_id}/artifacts/stderr-tail")
        assert stderr_tail.status_code == 200
        assert stderr_tail.text.strip() == "artifact stderr"

        events = client.get(f"/tasks/{task_id}/artifacts/events")
        assert events.status_code == 200
        assert events.json()["task_id"] == task_id
        assert events.json()["events"][0]["index"] == 0


def test_task_artifact_endpoint_returns_404_for_unknown_task() -> None:
    with TestClient(create_app(Settings(allowed_workspaces=(Path.cwd(),)))) as client:
        response = client.get("/tasks/missing/artifacts/final-output")

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
    fake_agent = tmp_path / "fake_agent.py"
    marker = tmp_path / "interrupted"
    ready = tmp_path / "ready"
    fake_agent.write_text(
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
        agent_command=(sys.executable, str(fake_agent)),
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
