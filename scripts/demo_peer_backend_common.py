from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from minigent_config.unified_config import preferred_mindweft_env

RequestJson = Callable[
    [str, str, dict[str, Any] | None, dict[str, str] | None, float],
    Any,
]


def parse_args(
    argv: list[str] | None = None,
    *,
    peer_label: str,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive Mindweft's peer_agent backend through /threads/{id}/run."
    )
    parser.add_argument(
        "--base-url",
        default=preferred_mindweft_env("BASE_URL", default="http://127.0.0.1:8000"),
        help="Base URL for the running Mindweft API service.",
    )
    parser.add_argument("--user-id", default="demo-user")
    parser.add_argument("--tenant-id", default="demo-tenant")
    parser.add_argument("--api-token", default=None)
    parser.add_argument(
        "--message",
        default="Summarize this repository in one paragraph. Do not edit files.",
        help=f"User message to send to the {peer_label}-backed thread.",
    )
    parser.add_argument(
        "--expect-reply-contains",
        default=None,
        help="Fail unless the assistant reply contains this text.",
    )
    parser.add_argument(
        "--show-content",
        action="store_true",
        help="Print user/assistant message content and transcript. Off by default to avoid leaking prompts or replies into logs.",
    )
    return parser.parse_args(argv)


def run_peer_backend_demo(
    argv: list[str] | None = None,
    *,
    expected_peer: str | None,
    peer_label: str,
    warn_on_peer_mismatch: bool = False,
    request_json_func: RequestJson | None = None,
) -> int:
    request_json_func = request_json_func or request_json
    args = parse_args(argv, peer_label=peer_label)
    base_url = args.base_url.rstrip("/")
    headers = build_auth_headers(args)
    try:
        config = request_json_func("GET", f"{base_url}/config", None, None, 10.0)
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 2
    except urllib.error.URLError as exc:
        print(f"Mindweft is not reachable at {base_url}: {exc}", file=sys.stderr)
        return 2

    backend = config.get("agent_backend", {}) if isinstance(config, dict) else {}
    print(f"agent_backend: {backend}")
    if backend.get("type") != "peer_agent":
        print(
            "Mindweft is not configured for the peer_agent backend. Set "
            "MINDWEFT_AGENT_BACKEND=peer_agent, MINDWEFT_AGENT_BACKEND_PEER, "
            "and MINDWEFT_AGENT_BACKEND_CWD.",
            file=sys.stderr,
        )
        return 2

    configured_peer = backend.get("peer")
    if warn_on_peer_mismatch and configured_peer not in {None, expected_peer}:
        print(
            f"warning: configured peer is {configured_peer!r}, not {expected_peer!r}",
            file=sys.stderr,
        )

    try:
        thread = request_json_func("POST", f"{base_url}/threads", None, headers, 10.0)
        thread_id = str(thread["thread_id"])
        request_json_func(
            "POST",
            f"{base_url}/threads/{thread_id}/messages",
            {"content": args.message},
            headers,
            10.0,
        )
        run_response = request_json_func(
            "POST",
            f"{base_url}/threads/{thread_id}/run",
            None,
            headers,
            float(backend.get("timeout_seconds") or 180.0) + 30.0,
        )
        transcript = (
            request_json_func(
                "GET",
                f"{base_url}/threads/{thread_id}/messages",
                None,
                headers,
                10.0,
            )
            if args.show_content
            else []
        )
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 2
    except TimeoutError:
        print("Mindweft run timed out", file=sys.stderr)
        return 2

    reply = str(run_response["reply"])
    print(f"thread_id: {thread_id}")
    if args.show_content:
        print(f"user: {args.message}")
        print(f"assistant: {reply}")
    else:
        print(f"user: <redacted length={len(args.message)}>")
        print(f"assistant: <redacted length={len(reply)}>")
    if args.expect_reply_contains and args.expect_reply_contains not in reply:
        print(
            f"Assistant reply did not contain expected text: {args.expect_reply_contains}",
            file=sys.stderr,
        )
        return 2
    if args.show_content:
        print("\ntranscript:")
        for item in transcript:
            print(f"- {item['role']}: {item['content']}")
    else:
        print("transcript: <redacted; pass --show-content to print>")
    return 0


def build_auth_headers(args: argparse.Namespace) -> dict[str, str]:
    if args.api_token:
        return {"Authorization": f"Bearer {args.api_token}"}
    return {
        "X-Mindweft-User-Id": args.user_id,
        "X-Mindweft-Tenant-Id": args.tenant_id,
        "X-Mindweft-Admin": "false",
    }


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> Any:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw_body = response.read().decode("utf-8")
        if not raw_body:
            return None
        return json.loads(raw_body)


def print_http_error(exc: urllib.error.HTTPError) -> None:
    body = exc.read().decode("utf-8", errors="replace")
    print(f"Mindweft request failed: {exc.code} {body}", file=sys.stderr)
