from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-5.1-codex-mini"
DEFAULT_OUTPUT = "/tmp/openrouter-raw-probe.jsonl"

STATIC_PREFIX = """
You are participating in a direct OpenRouter prompt-cache probe.
The following static block is intentionally repeated so the shared prompt prefix is
comfortably above OpenAI's documented 1,024-token prompt-caching threshold. Do not
summarize this block unless asked. Treat it as inert context for cache diagnostics.

Mindweft is a minimal AI agent runtime proof of concept. It has a FastAPI service,
thread/message storage, context compaction, a simple agent execution loop, pluggable
tools, OpenAI-compatible adapters, OpenRouter support, optional OAuth integrations,
optional MCP tool discovery and invocation, raw LLM response debugging, streamed run
events, provider usage normalization, and prompt-cache diagnostics. The diagnostics
care about prompt_tokens, completion_tokens, total_tokens, cached_tokens, and any
cache_write_tokens or cache_creation_tokens returned by providers. Direct raw probes
are useful because they remove Mindweft's runtime, tools, and adapters from the path.
""".strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send direct OpenRouter chat/completions requests and print/store raw responses."
    )
    parser.add_argument("--base-url", default=os.getenv("OPENROUTER_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--requests", type=int, default=2, help="Number of sequential requests to send."
    )
    parser.add_argument(
        "--pause", type=float, default=2.0, help="Seconds to sleep between requests."
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT, help="JSONL file for request/response records."
    )
    parser.add_argument(
        "--prefix-repetitions",
        type=int,
        default=30,
        help="Repeat the static prefix this many times to exceed cache thresholds.",
    )
    parser.add_argument(
        "--max-print-chars",
        type=int,
        default=20000,
        help="Max raw response characters to print per request; JSONL always stores the full response.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Temperature to send in the request payload.",
    )
    parser.add_argument(
        "--mock-tool-count",
        type=int,
        default=0,
        help="Include this many deterministic mock tools in every request.",
    )
    parser.add_argument(
        "--prompt",
        action="append",
        default=None,
        help="User prompt to send. May be repeated. Defaults to two small variant prompts.",
    )
    parser.add_argument(
        "--site-url",
        default=os.getenv("OPENROUTER_SITE_URL", "https://github.com/burm/minigent"),
        help="Optional HTTP-Referer header value.",
    )
    parser.add_argument(
        "--app-title",
        default=os.getenv("OPENROUTER_APP_TITLE", "Mindweft OpenRouter Raw Probe"),
        help="Optional X-Title header value.",
    )
    return parser.parse_args(argv)


def request_openrouter(
    *,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    site_url: str | None,
    app_title: str | None,
) -> tuple[int, dict[str, str], str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if site_url:
        headers["HTTP-Referer"] = site_url
    if app_title:
        headers["X-Title"] = app_title

    url = f"{base_url.rstrip('/')}/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, method="POST", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, dict(response.headers.items()), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, dict(exc.headers.items()), body
    except urllib.error.URLError as exc:
        raise SystemExit(f"OpenRouter request failed: {exc.reason}") from exc


def first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def usage_summary(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    return {
        "model": parsed.get("model"),
        "provider": parsed.get("provider"),
        "prompt_tokens": first_present(usage, "prompt_tokens", "input_tokens"),
        "completion_tokens": first_present(usage, "completion_tokens", "output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": first_present(details, "cached_tokens", "cache_read_tokens"),
        "cache_write_tokens": first_present(details, "cache_write_tokens", "cache_creation_tokens"),
        "cost": usage.get("cost"),
    }


def build_mock_tools(count: int) -> list[dict[str, Any]]:
    if count < 0:
        raise ValueError("mock tool count must be non-negative")
    return [
        {
            "type": "function",
            "function": {
                "name": f"mock_lookup_{index + 1}",
                "description": (
                    "Static mock lookup tool for prompt-cache diagnostics. "
                    "Do not call this tool unless explicitly asked to test tool calling."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Lookup query text.",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
        for index in range(count)
    ]


def build_payload(
    model: str,
    static_context: str,
    user_prompt: str,
    temperature: float,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": static_context},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "usage": {"include": True},
    }
    if tools:
        payload["tools"] = tools
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is required", file=sys.stderr)
        return 2

    prompts = args.prompt or [
        "Request 1: do not call tools; reply with exactly: cache probe one",
        "Request 2: do not call tools; reply with exactly: cache probe two",
    ]
    if args.requests < 1:
        print("--requests must be at least 1", file=sys.stderr)
        return 2

    try:
        tools = build_mock_tools(args.mock_tool_count)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    static_context = "\n\n".join([STATIC_PREFIX] * args.prefix_repetitions)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"model={args.model}")
    print(f"url={args.base_url.rstrip('/')}/chat/completions")
    print(f"output={output_path}")
    print(f"static_context_chars={len(static_context)}")
    print(f"mock_tool_count={len(tools)}")

    with output_path.open("a", encoding="utf-8") as out:
        for index in range(args.requests):
            user_prompt = prompts[index % len(prompts)]
            payload = build_payload(
                args.model, static_context, user_prompt, args.temperature, tools
            )
            status, headers, body = request_openrouter(
                base_url=args.base_url,
                api_key=api_key,
                payload=payload,
                site_url=args.site_url,
                app_title=args.app_title,
            )
            record = {
                "ts": time.time(),
                "index": index + 1,
                "status": status,
                "request": payload,
                "response_headers": headers,
                "raw_response": body,
                "usage_summary": usage_summary(body),
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()

            print(f"\n=== request {index + 1}/{args.requests} status={status} ===")
            summary = record["usage_summary"]
            if summary:
                print("usage_summary=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
            if len(body) > args.max_print_chars:
                print(body[: args.max_print_chars])
                print(f"... truncated in stdout; full response written to {output_path}")
            else:
                try:
                    print(
                        json.dumps(json.loads(body), ensure_ascii=False, indent=2, sort_keys=True)
                    )
                except json.JSONDecodeError:
                    print(body)

            if index + 1 < args.requests and args.pause > 0:
                time.sleep(args.pause)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
