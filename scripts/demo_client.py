from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive a running Minigent server from the command line."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL for the running API service.",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Existing thread to continue. If omitted, a new thread is created.",
    )
    parser.add_argument(
        "messages",
        nargs="+",
        help="One or more user messages to send in sequence.",
    )
    return parser.parse_args()


def request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            raw_body = response.read().decode("utf-8")
            if not raw_body:
                return None
            return json.loads(raw_body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} failed: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{method} {url} failed: {exc.reason}") from exc


def ensure_thread(base_url: str, thread_id: str | None) -> str:
    if thread_id:
        return thread_id
    response = request_json("POST", f"{base_url}/threads")
    return response["thread_id"]


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    config = request_json("GET", f"{base_url}/config")
    llm = config.get("llm", {}) if isinstance(config, dict) else {}
    print(
        "llm:"
        f" provider={llm.get('provider')}"
        f" model={llm.get('model')}"
        f" base_url={llm.get('base_url')}"
    )
    thread_id = ensure_thread(base_url, args.thread_id)

    print(f"thread_id={thread_id}")
    for content in args.messages:
        request_json(
            "POST",
            f"{base_url}/threads/{thread_id}/messages",
            {"content": content},
        )
        run_response = request_json("POST", f"{base_url}/threads/{thread_id}/run")
        print(f"user: {content}")
        print(f"assistant: {run_response['reply']}")

    messages = request_json("GET", f"{base_url}/threads/{thread_id}/messages")
    print("\ntranscript:")
    for message in messages:
        role = message["role"]
        tool_name = message.get("tool_name")
        suffix = f" ({tool_name})" if tool_name else ""
        print(f"- {role}{suffix}: {message['content']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
