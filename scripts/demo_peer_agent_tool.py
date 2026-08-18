from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from minigent_config.unified_config import preferred_mindweft_env


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive the peer_agent_task tool through a running Mindweft server."
    )
    parser.add_argument(
        "--base-url",
        default=preferred_mindweft_env("BASE_URL", default="http://127.0.0.1:8000"),
        help="Base URL for the running Mindweft API service.",
    )
    parser.add_argument("--peer", default="pi")
    parser.add_argument("--cwd", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--prompt",
        default="Summarize this repository in one paragraph. Do not edit files.",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=None,
        help="HTTP timeout for Mindweft requests. Defaults to timeout + 30 seconds.",
    )
    parser.add_argument("--user-id", default="demo-user")
    parser.add_argument("--tenant-id", default="demo-tenant")
    parser.add_argument("--api-token", default=None)
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="Submit the peer task through the tool without polling for completion.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_url = args.base_url.rstrip("/")
    headers = build_auth_headers(args)
    http_timeout = args.http_timeout if args.http_timeout is not None else args.timeout + 30.0

    try:
        config = request_json("GET", f"{base_url}/config", timeout=http_timeout)
        peers = request_json("GET", f"{base_url}/peer-agents", timeout=http_timeout)
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 2
    except urllib.error.URLError as exc:
        print(f"Mindweft is not reachable at {base_url}: {exc}", file=sys.stderr)
        return 2
    except TimeoutError:
        print(f"Mindweft request timed out at {base_url}", file=sys.stderr)
        return 2

    local_tools = config.get("local_tools", []) if isinstance(config, dict) else []
    print(f"local_tools: {local_tools}")
    if "peer_agent_task" not in local_tools:
        print(
            "peer_agent_task is not enabled. Restart Mindweft with "
            "MINDWEFT_ENABLE_PEER_AGENT_TOOL=true.",
            file=sys.stderr,
        )
        return 2
    print(f"peers: {peers}")

    try:
        thread = request_json("POST", f"{base_url}/threads", headers=headers, timeout=http_timeout)
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 2
    except TimeoutError:
        print(f"Mindweft request timed out at {base_url}", file=sys.stderr)
        return 2

    thread_id = str(thread["thread_id"])
    tool_payload = {
        "peer": args.peer,
        "cwd": args.cwd,
        "prompt": args.prompt,
        "poll": not args.no_poll,
        "timeout_seconds": args.timeout,
        "poll_interval_seconds": args.poll_interval,
    }
    message = f"/tool peer_agent_task {json.dumps(tool_payload, separators=(',', ':'))}"
    try:
        request_json(
            "POST",
            f"{base_url}/threads/{thread_id}/messages",
            {"content": message},
            headers=headers,
            timeout=http_timeout,
        )
        run_response = request_json(
            "POST",
            f"{base_url}/threads/{thread_id}/run",
            headers=headers,
            timeout=http_timeout,
        )
        transcript = request_json(
            "GET",
            f"{base_url}/threads/{thread_id}/messages",
            headers=headers,
            timeout=http_timeout,
        )
    except urllib.error.HTTPError as exc:
        print_http_error(exc)
        return 2
    except TimeoutError:
        print(f"Mindweft run timed out after {http_timeout} seconds", file=sys.stderr)
        return 2

    print(f"thread_id: {thread_id}")
    print(f"user: {message}")
    print(f"assistant: {run_response['reply']}")
    peer_result = find_peer_agent_tool_result(transcript)
    if peer_result is not None:
        print(f"peer_summary: {format_peer_agent_summary(peer_result)}")
    print("\ntranscript:")
    for item in transcript:
        role = item["role"]
        tool_name = item.get("tool_name")
        suffix = f" ({tool_name})" if tool_name else ""
        print(f"- {role}{suffix}: {item['content']}")
    if peer_result is None:
        print("peer_agent_task result was not found in the transcript", file=sys.stderr)
        return 1
    status = peer_result.get("status")
    timed_out = peer_result.get("timed_out")
    if status not in {"completed", "succeeded"} or timed_out is True:
        print(f"peer_agent_task did not complete successfully: {peer_result}", file=sys.stderr)
        return 1
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


def find_peer_agent_tool_result(transcript: Any) -> dict[str, Any] | None:
    if not isinstance(transcript, list):
        return None
    for item in reversed(transcript):
        if not isinstance(item, dict):
            continue
        if item.get("role") != "tool" or item.get("tool_name") != "peer_agent_task":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            return None
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            return None
        return result if isinstance(result, dict) else None
    return None


def format_peer_agent_summary(result: dict[str, Any]) -> str:
    parts = [
        f"peer={result.get('peer', '')}",
        f"task_id={result.get('task_id', '')}",
        f"status={result.get('status', '')}",
        f"exit_code={result.get('exit_code')}",
        f"timed_out={result.get('timed_out')}",
        f"canceled_on_timeout={result.get('canceled_on_timeout')}",
        f"duration_seconds={result.get('duration_seconds')}",
    ]
    final_output_preview = str(result.get("final_output_preview") or "").strip()
    stderr_tail_preview = str(result.get("stderr_tail_preview") or "").strip()
    if final_output_preview:
        parts.append(f"final_output_preview={json.dumps(final_output_preview)}")
    if stderr_tail_preview:
        parts.append(f"stderr_tail_preview={json.dumps(stderr_tail_preview)}")
    return " ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
