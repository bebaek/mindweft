from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import tomllib
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from minigent_client.api_client import MinigentAPIClient
from minigent_client.config import ClientConfig, PrincipalConfig

RUN_E2E_ENV = "MINIGENT_RUN_E2E_TESTS"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv(RUN_E2E_ENV, "").strip().lower() not in {"1", "true", "yes", "on"},
        reason=f"Set {RUN_E2E_ENV}=true to run Minigent e2e tests",
    ),
]

AUTH_HEADERS = {
    "X-Minigent-User-Id": "e2e-user",
    "X-Minigent-Tenant-Id": "e2e-tenant",
}


def test_exported_config_can_boot_replacement_server(tmp_path: Path, repo_root: Path) -> None:
    source_config_path = tmp_path / "source.minigent.toml"
    source_db_path = tmp_path / "source.db"
    source_config_path.write_text(
        f"""
profile = "e2e-export-source"

[app]
thread_db_path = "{source_db_path}"

[auth]
mode = "dev-headers"

[llm]
provider = "mock"

[tenant_execution_configs."e2e-tenant"]

[tenant_execution_configs."e2e-tenant".llm]
provider = "mock"

[tenant_execution_configs."e2e-tenant".tools]
allowed_local_tools = ["current_time", "calculator"]

[tenant_execution_configs."e2e-tenant".skills]
default_skill = "math-helper"

[[tenant_execution_configs."e2e-tenant".skills.items]]
name = "math-helper"
description = "Math helper"
system_prompt = "Use calculator for arithmetic."
allowed_local_tools = ["calculator"]
""".strip(),
        encoding="utf-8",
    )

    source_dotenv_path = tmp_path / "source.env"
    source_dotenv_path.write_text(
        "\n".join(
            [
                "MINIGENT_AUTH_MODE=dev-headers",
                "MINIGENT_LLM_PROVIDER=mock",
                f"MINIGENT_THREAD_DB_PATH={source_db_path}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    exported_config_path = tmp_path / "exported.minigent.toml"
    replacement_dotenv_path = tmp_path / "replacement.env"
    replacement_dotenv_path.write_text(
        "\n".join(
            [
                "MINIGENT_AUTH_MODE=dev-headers",
                "MINIGENT_LLM_PROVIDER=mock",
                f"MINIGENT_THREAD_DB_PATH={tmp_path / 'replacement.db'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with _started_minigent_server(
        repo_root,
        {
            "MINIGENT_CONFIG_FILE": str(source_config_path),
            "MINIGENT_DOTENV_FILE": str(source_dotenv_path),
        },
        cwd=tmp_path,
    ) as source_base_url:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from minigent_client.one_shot_cli import main; raise SystemExit(main())",
                "--base-url",
                source_base_url,
                "config",
                "export",
                "--output",
                str(exported_config_path),
            ],
            cwd=tmp_path,
            env={
                **os.environ,
                "MINIGENT_BASE_URL": source_base_url,
                "MINIGENT_DOTENV_FILE": str(source_dotenv_path),
                "PYTHONPATH": str(repo_root),
            },
            text=True,
            capture_output=True,
            check=True,
        )

    exported_text = exported_config_path.read_text(encoding="utf-8")
    exported = tomllib.loads(exported_text)
    assert exported["llm"]["provider"] == "mock"
    assert exported["tenant_execution_configs"]["e2e-tenant"]["skills"]["default_skill"] == "math-helper"
    assert exported["tenant_execution_configs"]["e2e-tenant"]["tools"]["allowed_local_tools"] == [
        "current_time",
        "calculator",
    ]
    assert "runtime" not in exported

    with _started_minigent_server(
        repo_root,
        {
            "MINIGENT_CONFIG_FILE": str(exported_config_path),
            "MINIGENT_DOTENV_FILE": str(replacement_dotenv_path),
        },
        cwd=tmp_path,
    ) as replacement_base_url:
        replacement_config = _request_json("GET", f"{replacement_base_url}/config?export=true")
        assert replacement_config["llm"]["provider"] == "mock"
        replacement_export = replacement_config["unified_config_export"]
        assert (
            replacement_export["tenant_execution_configs"]["e2e-tenant"]["skills"]["default_skill"]
            == "math-helper"
        )
        assert set(replacement_export["runtime"]["tools"]) == {"calculator", "current_time"}

        thread = _request_json("POST", f"{replacement_base_url}/threads", headers=AUTH_HEADERS)
        thread_id = thread["thread_id"]
        _request_json(
            "POST",
            f"{replacement_base_url}/threads/{thread_id}/messages",
            headers=AUTH_HEADERS,
            payload={"content": "hello after config export/import", "skill": "math-helper"},
        )
        run = _request_json(
            "POST",
            f"{replacement_base_url}/threads/{thread_id}/run",
            headers=AUTH_HEADERS,
        )
        assert run == {"reply": "Mock reply: hello after config export/import"}


def test_live_server_thread_lifecycle(live_minigent_server: str) -> None:
    health = _request_json("GET", f"{live_minigent_server}/health")
    assert health == {"status": "ok"}

    config = _request_json("GET", f"{live_minigent_server}/config")
    assert config["llm"]["provider"] == "mock"

    thread = _request_json("POST", f"{live_minigent_server}/threads", headers=AUTH_HEADERS)
    thread_id = thread["thread_id"]
    assert isinstance(thread_id, str)
    assert thread_id

    message = _request_json(
        "POST",
        f"{live_minigent_server}/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
        payload={"content": "hello from raw http e2e"},
    )
    assert message["role"] == "user"
    assert message["content"] == "hello from raw http e2e"

    run = _request_json(
        "POST",
        f"{live_minigent_server}/threads/{thread_id}/run",
        headers=AUTH_HEADERS,
    )
    assert run == {"reply": "Mock reply: hello from raw http e2e"}

    messages = _request_json(
        "GET",
        f"{live_minigent_server}/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
    )
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "Mock reply: hello from raw http e2e"


def test_python_client_against_live_server(live_minigent_server: str) -> None:
    client = MinigentAPIClient(_client_config(live_minigent_server, stream_runs=False))

    response = client.send_user_message("hello from python client e2e")
    assert response["role"] == "user"

    reply, metadata = client.run_thread()

    assert reply == "Mock reply: hello from python client e2e"
    assert metadata is None
    assert isinstance(client.thread_id, str)


def test_python_client_streaming_against_live_server(live_minigent_server: str) -> None:
    progress_stream = StringIO()
    client = MinigentAPIClient(
        _client_config(live_minigent_server, stream_runs=True),
        progress_stream=progress_stream,
    )

    client.send_user_message("hello from streaming client e2e")
    reply, metadata = client.run_thread()

    assert reply == "Mock reply: hello from streaming client e2e"
    assert metadata is None
    client.flush_pending_token_summary()
    progress = progress_stream.getvalue()
    assert "● preparing" in progress
    assert "● sending" in progress
    assert "● done" in progress


def test_live_server_run_stream_ndjson(live_minigent_server: str) -> None:
    thread = _request_json("POST", f"{live_minigent_server}/threads", headers=AUTH_HEADERS)
    thread_id = thread["thread_id"]
    _request_json(
        "POST",
        f"{live_minigent_server}/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
        payload={"content": "hello from ndjson e2e"},
    )

    events = list(
        _request_ndjson(
            "POST",
            f"{live_minigent_server}/threads/{thread_id}/run/stream",
            headers=AUTH_HEADERS,
        )
    )

    event_types = [event["type"] for event in events]
    assert event_types == [
        "run.started",
        "llm.request",
        "assistant.message",
        "run.completed",
    ]
    assert (
        events[event_types.index("assistant.message")]["content"]
        == "Mock reply: hello from ndjson e2e"
    )


@contextmanager
def _started_minigent_server(
    repo_root: Path,
    env_overrides: dict[str, str],
    *,
    cwd: Path | None = None,
) -> Iterator[str]:
    port = _unused_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "MINIGENT_BASE_URL": base_url,
        "PYTHONPATH": str(repo_root),
        **env_overrides,
    }
    for key in (
        "MINIGENT_AUTH_MODE",
        "MINIGENT_LLM_PROVIDER",
        "MINIGENT_LLM_MODEL",
        "MINIGENT_LLM_URL",
        "MINIGENT_THREAD_DB_PATH",
        "MINIGENT_TENANT_EXECUTION_CONFIGS",
    ):
        env.pop(key, None)
    env.update(env_overrides)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=cwd or repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_health(base_url, process)
        yield base_url
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
        if process.returncode not in {0, -15}:
            print(stdout, file=sys.stdout)
            print(stderr, file=sys.stderr)


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise RuntimeError(
                "Minigent e2e server exited before becoming healthy\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=0.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ok":
                return
        except Exception as exc:  # noqa: BLE001 - preserve last startup failure detail.
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"Minigent e2e server did not become healthy: {last_error!r}")


def _client_config(base_url: str, *, stream_runs: bool) -> ClientConfig:
    return ClientConfig(
        base_url=base_url,
        wake_phrase="hey minigent",
        stream_runs=stream_runs,
        principal=PrincipalConfig(user_id="e2e-user", tenant_id="e2e-tenant"),
    )


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, method=method, headers=request_headers, data=data)
    with urllib.request.urlopen(request, timeout=5) as response:
        raw_body = response.read().decode("utf-8")
    if not raw_body:
        return None
    return json.loads(raw_body)


def _request_ndjson(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    request_headers = {"Accept": "application/x-ndjson", **(headers or {})}
    request = urllib.request.Request(url, method=method, headers=request_headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if line:
                event = json.loads(line)
                assert isinstance(event, dict)
                yield event
