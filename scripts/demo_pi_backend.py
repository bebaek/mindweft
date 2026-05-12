from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive Minigent's peer_agent backend through /threads/{id}/run."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("MINIGENT_BASE_URL", "http://127.0.0.1:8000"),
        help="Base URL for the running Minigent API service.",
    )
    parser.add_argument("--user-id", default="demo-user")
    parser.add_argument("--tenant-id", default="demo-tenant")
    parser.add_argument("--api-token", default=None)
    parser.add_argument(
        "--message",
        default="Summarize this repository in one paragraph. Do not edit files.",
        help="User message to send to the Pi-backed thread.",
    )
    parser.add_argument(
        "--expect-reply-contains",
        default=None,
        help="Fail unless the assistant reply contains this text.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_url = args.base_url.rstrip("/")
    headers = build_auth_headers(args)
    try:
        config = request_json("GET", f"{base_url}/config")
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 2
    except urllib.error.URLError as exc:
        print(f"Minigent is not reachable at {base_url}: {exc}", file=sys.stderr)
        return 2

    backend = config.get("agent_backend", {}) if isinstance(config, dict) else {}
    print(f"agent_backend: {backend}")
    if backend.get("type") != "peer_agent":
        print(
            "Minigent is not configured for the peer_agent backend. Set "
            "MINIGENT_AGENT_BACKEND=peer_agent, MINIGENT_AGENT_BACKEND_PEER, "
            "and MINIGENT_AGENT_BACKEND_CWD.",
            file=sys.stderr,
        )
        return 2

    configured_peer = backend.get("peer")
    if configured_peer not in {None, "pi"}:
        print(
            f"warning: configured peer is {configured_peer!r}, not 'pi'",
            file=sys.stderr,
        )

    try:
        thread = request_json("POST", f"{base_url}/threads", headers=headers)
        thread_id = str(thread["thread_id"])
        request_json(
            "POST",
            f"{base_url}/threads/{thread_id}/messages",
            {"content": args.message},
            headers=headers,
        )
        run_response = request_json(
            "POST",
            f"{base_url}/threads/{thread_id}/run",
            headers=headers,
            timeout=float(backend.get("timeout_seconds") or 180.0) + 30.0,
        )
        transcript = request_json(
            "GET",
            f"{base_url}/threads/{thread_id}/messages",
            headers=headers,
        )
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 2
    except TimeoutError:
        print("Minigent run timed out", file=sys.stderr)
        return 2

    print(f"thread_id: {thread_id}")
    print(f"user: {args.message}")
    print(f"assistant: {run_response['reply']}")
    if args.expect_reply_contains and args.expect_reply_contains not in str(run_response["reply"]):
        print(
            f"Assistant reply did not contain expected text: {args.expect_reply_contains}",
            file=sys.stderr,
        )
        return 2
    print("\ntranscript:")
    for item in transcript:
        print(f"- {item['role']}: {item['content']}")
    return 0


def build_auth_headers(args: argparse.Namespace) -> dict[str, str]:
    if args.api_token:
        return {"Authorization": f"Bearer {args.api_token}"}
    return {
        "X-Minigent-User-Id": args.user_id,
        "X-Minigent-Tenant-Id": args.tenant_id,
        "X-Minigent-Admin": "false",
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
    print(f"Minigent request failed: {exc.code} {body}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
