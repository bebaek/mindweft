from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Preflight checks for the peer-agent demo stack.")
    parser.add_argument("--root-dir", default=str(root_dir))
    parser.add_argument("--wrapper-dir", default=str(root_dir / "codex-agent-wrapper"))
    parser.add_argument(
        "--workspace",
        default=os.getenv("CODEX_AGENT_ALLOWED_WORKSPACES", str(root_dir)),
    )
    parser.add_argument("--codex-command", default=os.getenv("CODEX_AGENT_COMMAND", "codex"))
    parser.add_argument("--codex-agent-host", default=os.getenv("CODEX_AGENT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--codex-agent-port", type=int, default=int(os.getenv("CODEX_AGENT_PORT", "8010"))
    )
    parser.add_argument("--minigent-host", default=os.getenv("MINIGENT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--minigent-port", type=int, default=int(os.getenv("MINIGENT_PORT", "8000"))
    )
    parser.add_argument(
        "--check-running",
        action="store_true",
        help="Check already-running services instead of checking that demo ports are free.",
    )
    parser.add_argument(
        "--skip-codex-wrapper-health",
        action="store_true",
        help="In --check-running mode, skip direct wrapper health checks for internal-only sidecars.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = run_checks(args)
    for result in results:
        status = "ok" if result.ok else "fail"
        print(f"[{status}] {result.name}: {result.detail}")
    return 0 if all(result.ok for result in results) else 1


def run_checks(args: argparse.Namespace) -> list[CheckResult]:
    root_dir = Path(args.root_dir).expanduser().resolve()
    wrapper_dir = Path(args.wrapper_dir).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()
    codex_command = shlex.split(args.codex_command)
    codex_executable = codex_command[0] if codex_command else ""

    checks = [
        check_directory("root directory", root_dir),
        check_directory("codex wrapper directory", wrapper_dir),
        check_directory("workspace allowlist path", workspace),
        check_executable("uv executable", "uv"),
        check_executable("codex executable", codex_executable),
        check_command("uv can run", ["uv", "--version"], cwd=root_dir),
        check_command("codex exec help", [*codex_command, "exec", "--help"], cwd=root_dir),
        check_command(
            "minigent imports",
            ["uv", "run", "python", "-c", "import app.main"],
            cwd=root_dir,
        ),
        check_command(
            "codex wrapper imports",
            ["uv", "run", "python", "-c", "import codex_agent_wrapper.app"],
            cwd=wrapper_dir,
        ),
    ]
    if args.check_running:
        checks.extend(check_running_services(args))
    else:
        checks.append(
            check_port_free("codex wrapper port", args.codex_agent_host, args.codex_agent_port)
        )
        checks.append(check_port_free("minigent port", args.minigent_host, args.minigent_port))
    return checks


def check_directory(name: str, path: Path) -> CheckResult:
    if path.is_dir():
        return CheckResult(name, True, str(path))
    return CheckResult(name, False, f"{path} does not exist or is not a directory")


def check_executable(name: str, executable: str) -> CheckResult:
    if executable and shutil.which(executable):
        return CheckResult(name, True, executable)
    return CheckResult(name, False, f"{executable or '<empty>'} not found on PATH")


def check_command(name: str, command: list[str], *, cwd: Path) -> CheckResult:
    if not command or not command[0]:
        return CheckResult(name, False, "empty command")
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(name, False, str(exc))
    if completed.returncode == 0:
        return CheckResult(name, True, " ".join(command))
    detail = (completed.stderr or completed.stdout or "").strip()
    return CheckResult(name, False, detail or f"exit code {completed.returncode}")


def check_port_free(name: str, host: str, port: int) -> CheckResult:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as exc:
        return CheckResult(name, False, f"{host}:{port} is not available: {exc}")
    return CheckResult(name, True, f"{host}:{port} is available")


def check_running_services(args: argparse.Namespace) -> list[CheckResult]:
    codex_url = f"http://{args.codex_agent_host}:{args.codex_agent_port}"
    minigent_url = f"http://{args.minigent_host}:{args.minigent_port}"
    results: list[CheckResult] = []
    if not args.skip_codex_wrapper_health:
        results.append(check_url("codex wrapper health", f"{codex_url}/health"))
    config = request_json_result("minigent config", f"{minigent_url}/config")
    results.append(config[0])
    peers = request_json_result("minigent peer agents", f"{minigent_url}/peer-agents")
    results.append(peers[0])
    if config[1] is not None:
        local_tools = config[1].get("local_tools", [])
        results.append(
            CheckResult(
                "peer_agent_task enabled",
                "peer_agent_task" in local_tools,
                "present in /config local_tools"
                if "peer_agent_task" in local_tools
                else "missing from /config local_tools",
            )
        )
    if peers[1] is not None:
        agents = peers[1].get("agents", [])
        has_codex = any(
            isinstance(agent, dict) and agent.get("name") == "codex" for agent in agents
        )
        results.append(
            CheckResult(
                "codex peer configured",
                has_codex,
                "codex listed in /peer-agents" if has_codex else "codex missing from /peer-agents",
            )
        )
    return results


def check_url(name: str, url: str) -> CheckResult:
    try:
        request_json(url)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return CheckResult(name, False, str(exc))
    return CheckResult(name, True, url)


def request_json_result(name: str, url: str) -> tuple[CheckResult, dict[str, Any] | None]:
    try:
        payload = request_json(url)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return CheckResult(name, False, str(exc)), None
    return CheckResult(name, True, url), payload


def request_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("response was not a JSON object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
