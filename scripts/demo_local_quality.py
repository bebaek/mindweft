from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from fastapi.testclient import TestClient

from app.execution import FixedTenantExecutionResolver, TenantExecutionConfig, TenantQualityConfig
from app.llm import OpenAICompatibleAdapter
from app.tools import ToolRegistry

AUTH_HEADERS = {
    "X-Minigent-User-Id": "demo-user",
    "X-Minigent-Tenant-Id": "demo-tenant",
}
DEFAULT_MESSAGE = (
    "Explain whether a local-first agent can use a remote reviewer safely. "
    "For the privacy demo, mention this fake private path /Users/alice/secret-project "
    "and fake email alice@example.com in the draft if relevant."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an in-process Minigent demo with llama.cpp as the private/main LLM "
            "and optional sanitized remote quality critique."
        )
    )
    parser.add_argument("--llama-base-url", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--llama-model", default="local-model")
    parser.add_argument("--llama-api-key", default="llama.cpp")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument(
        "--quality-provider",
        default="mock",
        choices=["mock", "openai", "openrouter", "openai-compatible"],
        help="Remote quality reviewer provider. 'mock' exercises the path without a remote API key.",
    )
    parser.add_argument("--quality-model", default=None)
    parser.add_argument("--quality-base-url", default=None)
    parser.add_argument("--quality-api-key", default=None)
    parser.add_argument("--quality-max-payload-chars", type=int, default=6000)
    parser.add_argument(
        "--no-quality",
        action="store_true",
        help="Disable the quality layer and use llama.cpp only.",
    )
    return parser.parse_args(argv)


def build_client(args: argparse.Namespace) -> TestClient:
    # Importing app.main constructs its module-level ASGI app. Keep that import-time
    # app isolated from the user's .env so this demo does not initialize unrelated MCP
    # servers or remote LLM config before creating the explicit demo app below.
    os.environ["MINIGENT_TENANT_EXECUTION_CONFIGS"] = json.dumps(
        {"*": {"llm": {"provider": "mock"}, "tools": {"allowed_local_tools": []}}}
    )
    from app.main import create_app

    primary_llm = OpenAICompatibleAdapter(
        base_url=args.llama_base_url,
        api_key=args.llama_api_key,
        model=args.llama_model,
        timeout=120.0,
    )
    quality = TenantQualityConfig(
        enabled=not args.no_quality,
        provider=args.quality_provider,
        model=args.quality_model,
        base_url=args.quality_base_url,
        api_key=args.quality_api_key,
        max_payload_chars=args.quality_max_payload_chars,
    )
    resolver = FixedTenantExecutionResolver(
        primary_llm,
        ToolRegistry(),
        config=TenantExecutionConfig(tenant_id="demo-tenant", quality=quality),
    )
    return TestClient(create_app(execution_resolver=resolver))


def request_json(
    client: TestClient,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(method, url, json=payload, headers=AUTH_HEADERS)
    if response.status_code >= 400:
        raise SystemExit(f"{method} {url} failed: {response.status_code} {response.text}")
    parsed = response.json()
    if not isinstance(parsed, dict):
        raise SystemExit(f"{method} {url} returned non-object JSON")
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    client = build_client(args)
    config = request_json(client, "GET", "/config")
    print("config:")
    print(json.dumps(config, indent=2, sort_keys=True))

    thread_id = request_json(client, "POST", "/threads", payload={})["thread_id"]
    request_json(
        client,
        "POST",
        f"/threads/{thread_id}/messages",
        payload={"content": args.message},
    )

    print(f"\nthread_id={thread_id}")
    print(f"user: {args.message}\n")
    print("stream events:")
    final_reply = ""
    with client.stream("POST", f"/threads/{thread_id}/run/stream", headers=AUTH_HEADERS) as response:
        if response.status_code >= 400:
            raise SystemExit(f"run stream failed: {response.status_code} {response.text}")
        for line in response.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            event_type = event.get("type")
            if event_type == "assistant.message":
                final_reply = str(event.get("content", ""))
                print(f"- {event_type}: <final reply omitted until below>")
            else:
                print(f"- {event_type}: {json.dumps(_event_summary(event), sort_keys=True)}")

    print("\nassistant:")
    print(final_reply)
    return 0


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    hidden = {"content", "result", "arguments"}
    return {key: value for key, value in event.items() if key not in hidden and key != "thread_id"}


if __name__ == "__main__":
    sys.exit(main())
