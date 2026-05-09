from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit and poll a simple Codex agent task.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--cwd", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument(
        "--prompt",
        default="Summarize this repository in one paragraph. Do not edit files.",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    try:
        health = _request_json("GET", f"{base_url}/health")
    except urllib.error.URLError as exc:
        print(f"Wrapper is not reachable at {base_url}: {exc}", file=sys.stderr)
        return 2
    print(f"health: {health}")

    task = _request_json(
        "POST",
        f"{base_url}/tasks",
        {"cwd": args.cwd, "prompt": args.prompt},
    )
    task_id = str(task["task_id"])
    print(f"submitted: {task_id}")

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        task = _request_json("GET", f"{base_url}/tasks/{task_id}")
        status = str(task["status"])
        print(f"status: {status}")
        if status in TERMINAL_STATUSES:
            _print_result(task)
            return 0 if status == "completed" else 1
        time.sleep(args.poll_interval)

    print(f"timed out waiting for {task_id}", file=sys.stderr)
    return 1


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _print_result(task: dict[str, Any]) -> None:
    print(f"exit_code: {task.get('exit_code')}")
    stdout_tail = str(task.get("stdout_tail") or "").strip()
    stderr_tail = str(task.get("stderr_tail") or "").strip()
    if stdout_tail:
        print("\nstdout_tail:")
        print(stdout_tail)
    if stderr_tail:
        print("\nstderr_tail:")
        print(stderr_tail)


if __name__ == "__main__":
    raise SystemExit(main())
