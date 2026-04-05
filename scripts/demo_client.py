from __future__ import annotations

import argparse
import json
import secrets
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
        "--skill-name",
        default=None,
        help="Skill to apply when creating a new thread.",
    )
    parser.add_argument(
        "--user-id",
        default="demo-user",
        help="Authenticated user ID sent as trusted headers when --api-token is not used.",
    )
    parser.add_argument(
        "--tenant-id",
        default="demo-tenant",
        help="Authenticated tenant ID sent as trusted headers when --api-token is not used.",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Mark the trusted-header principal as an admin when --api-token is not used.",
    )
    parser.add_argument(
        "--api-token",
        default=None,
        help="Bearer token sent via Authorization header. Prefer this when MINIGENT_AUTH_TOKENS is configured.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Send W3C traceparent headers and print the trace ID for log/span correlation.",
    )
    parser.add_argument(
        "messages",
        nargs="+",
        help="One or more user messages to send in sequence.",
    )
    return parser.parse_args()


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, method=method, data=data, headers=request_headers)
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


def ensure_thread(
    base_url: str,
    thread_id: str | None,
    *,
    skill_name: str | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    if thread_id:
        return thread_id
    payload = {"skill_name": skill_name} if skill_name is not None else None
    response = request_json("POST", f"{base_url}/threads", payload=payload, headers=headers)
    return response["thread_id"]


def build_trace_headers(trace_id: str | None) -> dict[str, str]:
    if trace_id is None:
        return {}
    parent_id = secrets.token_hex(8)
    return {"traceparent": f"00-{trace_id}-{parent_id}-01"}


def build_principal_headers(user_id: str, tenant_id: str, is_admin: bool) -> dict[str, str]:
    return {
        "X-Minigent-User-Id": user_id,
        "X-Minigent-Tenant-Id": tenant_id,
        "X-Minigent-Admin": "true" if is_admin else "false",
    }


def build_auth_headers(args: argparse.Namespace) -> dict[str, str]:
    if args.api_token:
        return {"Authorization": f"Bearer {args.api_token}"}
    return build_principal_headers(args.user_id, args.tenant_id, args.admin)


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    trace_id = secrets.token_hex(16) if args.trace else None
    request_headers = {
        **build_trace_headers(trace_id),
        **build_auth_headers(args),
    }

    config = request_json("GET", f"{base_url}/config", headers=build_trace_headers(trace_id))
    llm = config.get("llm", {}) if isinstance(config, dict) else {}
    print(
        "llm:"
        f" provider={llm.get('provider')}"
        f" model={llm.get('model')}"
        f" base_url={llm.get('base_url')}"
    )
    if trace_id is not None:
        print(f"trace_id={trace_id}")
        print("trace: sent via traceparent header; look for this trace_id in JSON logs or your trace backend")
    if args.api_token:
        print("principal: bearer token auth")
    else:
        print(
            f"principal: user_id={args.user_id} tenant_id={args.tenant_id} admin={'true' if args.admin else 'false'}"
        )

    thread_id = ensure_thread(
        base_url,
        args.thread_id,
        skill_name=args.skill_name,
        headers=request_headers,
    )

    print(f"thread_id={thread_id}")
    if args.skill_name is not None:
        print(f"skill_name={args.skill_name}")
    for content in args.messages:
        request_json(
            "POST",
            f"{base_url}/threads/{thread_id}/messages",
            {"content": content},
            headers=request_headers,
        )
        run_response = request_json(
            "POST",
            f"{base_url}/threads/{thread_id}/run",
            headers=request_headers,
        )
        print(f"user: {content}")
        print(f"assistant: {run_response['reply']}")

    messages = request_json(
        "GET",
        f"{base_url}/threads/{thread_id}/messages",
        headers=request_headers,
    )
    print("\ntranscript:")
    for message in messages:
        role = message["role"]
        tool_name = message.get("tool_name")
        suffix = f" ({tool_name})" if tool_name else ""
        print(f"- {role}{suffix}: {message['content']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
