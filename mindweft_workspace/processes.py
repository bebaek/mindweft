from __future__ import annotations

import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from mindweft_workspace.mcp_specs import CodingMCPServerSpec

_SENSITIVE_ARG_MARKERS = ("key", "token", "secret", "password", "authorization", "credential")


def start_process(command: list[str], *, env: dict[str, str], label: str) -> subprocess.Popen[str]:
    print(f"starting {label}: {redacted_command_for_log(command)}")
    return subprocess.Popen(command, env=env, text=True, start_new_session=True)


def redacted_command_for_log(command: list[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for part in command:
        lower_part = part.lower()
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if lower_part.startswith("--") and any(
            marker in lower_part for marker in _SENSITIVE_ARG_MARKERS
        ):
            if "=" in part:
                option, _value = part.split("=", 1)
                redacted.append(f"{option}=<redacted>")
            else:
                redacted.append(part)
                redact_next = True
            continue
        if "=" in part:
            key, _value = part.split("=", 1)
            if any(marker in key.lower() for marker in _SENSITIVE_ARG_MARKERS):
                redacted.append(f"{key}=<redacted>")
                continue
        redacted.append(part)
    return " ".join(shlex.quote(part) for part in redacted)


def wait_for_managed_http_server(spec: CodingMCPServerSpec, process: subprocess.Popen[str]) -> None:
    if not spec.health_url:
        return
    deadline = time.monotonic() + spec.startup_timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() <= deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"managed HTTP MCP server '{spec.name}' exited before health check succeeded: "
                f"code={return_code}"
            )
        try:
            with urllib.request.urlopen(spec.health_url, timeout=1.0) as response:
                if 200 <= response.status < 400:
                    print(f"healthy {spec.name} MCP HTTP server: {spec.health_url}")
                    return
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
        time.sleep(0.2)
    detail = f": {last_error}" if last_error is not None else ""
    raise RuntimeError(
        f"managed HTTP MCP server '{spec.name}' did not become healthy within "
        f"{spec.startup_timeout_seconds:g}s at {spec.health_url}{detail}"
    )


def wait_for_processes(processes: list[subprocess.Popen[str]]) -> int:
    while processes:
        time.sleep(0.5)
        for process in list(processes):
            return_code = process.poll()
            if return_code is not None:
                print(f"process exited: pid={process.pid} code={return_code}", file=sys.stderr)
                return return_code or 0
    return 0


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
