from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def live_minigent_server(tmp_path: Path, repo_root: Path) -> Iterator[str]:
    port = _unused_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "MINIGENT_CONFIG_DISCOVERY": "disabled",
        "MINIGENT_AUTH_MODE": "dev-headers",
        "MINIGENT_LLM_PROVIDER": "mock",
        "MINIGENT_THREAD_DB_PATH": str(tmp_path / "threads.db"),
    }
    env.pop("MINIGENT_CONFIG_FILE", None)
    env.pop("MINIGENT_DOTENV_FILE", None)
    env.pop("MINIGENT_TENANT_EXECUTION_CONFIGS", None)

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
        cwd=repo_root,
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
                "Mindweft e2e server exited before becoming healthy\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=0.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ok":
                return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"Mindweft e2e server did not become healthy: {last_error!r}")
