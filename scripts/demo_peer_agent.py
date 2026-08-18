from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mindweft_config.unified_config import preferred_mindweft_env

TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit and poll a peer-agent task through a running Mindweft server."
    )
    parser.add_argument(
        "--base-url",
        default=preferred_mindweft_env("BASE_URL", default="http://127.0.0.1:8000"),
        help="Base URL for the running Mindweft API service.",
    )
    parser.add_argument(
        "--peer",
        default="pi",
        help="Configured peer name from MINDWEFT_PEER_AGENTS.",
    )
    parser.add_argument(
        "--cwd",
        default=str(Path(__file__).resolve().parents[1]),
        help="Workspace path to send to the peer task.",
    )
    parser.add_argument(
        "--prompt",
        default="Summarize this repository in one paragraph. Do not edit files.",
        help="Prompt to send to the peer task.",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--cancel-after",
        type=float,
        default=None,
        help="Request peer task cancellation after this many seconds.",
    )
    parser.add_argument(
        "--show-log",
        action="store_true",
        help="Print peer stderr/progress log tail in addition to the final output.",
    )
    parser.add_argument(
        "--show-events",
        action="store_true",
        help="Fetch and print peer task events through Mindweft when the task finishes.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_url = args.base_url.rstrip("/")

    try:
        health = request_json("GET", f"{base_url}/health")
        peers = request_json("GET", f"{base_url}/peer-agents")
        card = request_json("GET", f"{base_url}/peer-agents/{args.peer}/agent-card")
        task = request_json(
            "POST",
            f"{base_url}/peer-agents/{args.peer}/tasks",
            {"cwd": args.cwd, "prompt": args.prompt},
        )
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 2
    except urllib.error.URLError as exc:
        print(f"Mindweft is not reachable at {base_url}: {exc}", file=sys.stderr)
        return 2

    print(f"health: {health}")
    print(f"peers: {peers}")
    print(f"agent_card: {card}")

    task_id = str(task["task_id"])
    print(f"submitted: {task_id}")

    deadline = time.monotonic() + args.timeout
    cancel_deadline = (
        time.monotonic() + args.cancel_after if args.cancel_after is not None else None
    )
    cancel_requested = False
    while time.monotonic() < deadline:
        try:
            task = request_json("GET", f"{base_url}/peer-agents/{args.peer}/tasks/{task_id}")
        except urllib.error.HTTPError as exc:
            print_http_error(exc)
            return 2
        status = str(task["status"])
        print(f"status: {status}")
        if status in TERMINAL_STATUSES:
            events = None
            if args.show_events:
                try:
                    events = request_json(
                        "GET",
                        f"{base_url}/peer-agents/{args.peer}/tasks/{task_id}/events",
                    )
                except urllib.error.HTTPError as exc:
                    print_http_error(exc)
                    return 2
            print_result(task, events=events, show_log=args.show_log)
            return 0 if status == "completed" else 1
        if (
            cancel_deadline is not None
            and not cancel_requested
            and time.monotonic() >= cancel_deadline
        ):
            try:
                task = request_json(
                    "POST",
                    f"{base_url}/peer-agents/{args.peer}/tasks/{task_id}/cancel",
                )
            except urllib.error.HTTPError as exc:
                print_http_error(exc)
                return 2
            cancel_requested = True
            print(f"cancel_requested: {task_id}")
        time.sleep(args.poll_interval)

    print(f"timed out waiting for {task_id}", file=sys.stderr)
    return 1


def request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def print_http_error(exc: urllib.error.HTTPError) -> None:
    body = exc.read().decode("utf-8", errors="replace")
    print(f"Mindweft request failed: {exc.code} {body}", file=sys.stderr)


def print_result(
    task: dict[str, Any],
    *,
    events: dict[str, Any] | None,
    show_log: bool,
) -> None:
    print(f"exit_code: {task.get('exit_code')}")
    final_output = str(task.get("final_output") or "").strip()
    stdout_tail = str(task.get("stdout_tail") or "").strip()
    stderr_tail = str(task.get("stderr_tail") or "").strip()
    if final_output:
        print("\nfinal_output:")
        print(final_output)
    elif stdout_tail:
        print("\nstdout_tail:")
        print(stdout_tail)
    if show_log and stderr_tail:
        print("\nstderr_tail:")
        print(stderr_tail)
    elif stderr_tail:
        print("\nstderr_tail: hidden; rerun with --show-log to print it")
    if events is not None:
        print("\nevents:")
        print(json.dumps(events, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
