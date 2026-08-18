from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.llm import OpenAICompatibleAdapter
from app.models import Message, MessageRole, ToolSpec
from minigent_config.unified_config import normalize_mindweft_env

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-5.1-codex-mini"
DEFAULT_OUTPUT = "/tmp/mindweft-openrouter-adapter-probe.jsonl"
DEFAULT_RAW_OUTPUT = "/tmp/mindweft-openrouter-adapter-raw.jsonl"

STATIC_PREFIX = """
You are participating in a Mindweft OpenAICompatibleAdapter prompt-cache probe.
The following static block is intentionally repeated so the shared prompt prefix is
comfortably above OpenAI's documented 1,024-token prompt-caching threshold. Do not
summarize this block unless asked. Treat it as inert context for cache diagnostics.

Mindweft is a minimal AI agent runtime proof of concept. It has a FastAPI service,
thread/message storage, context compaction, a simple agent execution loop, pluggable
tools, OpenAI-compatible adapters, OpenRouter support, optional OAuth integrations,
optional MCP tool discovery and invocation, raw LLM response debugging, streamed run
events, provider usage normalization, and prompt-cache diagnostics. The diagnostics
care about prompt_tokens, completion_tokens, total_tokens, cached_tokens, and any
cache_write_tokens or cache_creation_tokens returned by providers. Direct adapter probes
are useful because they keep Mindweft's adapter code in the path while removing the
agent runtime, tools, and changing conversation history from the request.
""".strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe OpenRouter prompt caching through Mindweft's OpenAICompatibleAdapter "
            "without the agent runtime/tool loop."
        )
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
        "--output", default=DEFAULT_OUTPUT, help="JSONL file for normalized probe records."
    )
    parser.add_argument(
        "--raw-output",
        default=DEFAULT_RAW_OUTPUT,
        help="JSONL file for Mindweft raw LLM response debug records.",
    )
    parser.add_argument(
        "--prefix-repetitions",
        type=int,
        default=30,
        help="Repeat the static prefix this many times to exceed cache thresholds.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--mock-tool-count",
        type=int,
        default=0,
        help="Include this many deterministic mock tools in every adapter request.",
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
        help="Optional HTTP-Referer header value sent by the adapter.",
    )
    parser.add_argument(
        "--app-title",
        default=os.getenv("OPENROUTER_APP_TITLE", "Mindweft OpenRouter Adapter Probe"),
        help="Optional X-OpenRouter-Title header value sent by the adapter.",
    )
    return parser.parse_args(argv)


def first_present(mapping: dict[str, Any] | None, *keys: str) -> Any:
    if not mapping:
        return None
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def cache_summary(usage: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "prompt_tokens": first_present(usage, "prompt_tokens", "input_tokens"),
        "completion_tokens": first_present(usage, "completion_tokens", "output_tokens"),
        "total_tokens": first_present(usage, "total_tokens"),
        "cache_read_tokens": first_present(usage, "cache_read_tokens", "cached_tokens"),
        "cache_write_tokens": first_present(usage, "cache_write_tokens", "cache_creation_tokens"),
    }


def build_mock_tools(count: int) -> list[ToolSpec]:
    if count < 0:
        raise ValueError("mock tool count must be non-negative")
    return [
        ToolSpec(
            name=f"mock_lookup_{index + 1}",
            description=(
                "Static mock lookup tool for prompt-cache diagnostics. "
                "Do not call this tool unless explicitly asked to test tool calling."
            ),
            input_schema={
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
        )
        for index in range(count)
    ]


def build_messages(thread_id: str, static_context: str, user_prompt: str) -> list[Message]:
    return [
        Message(thread_id=thread_id, role=MessageRole.SYSTEM, content=static_context),
        Message(thread_id=thread_id, role=MessageRole.USER, content=user_prompt),
    ]


async def run_probe(args: argparse.Namespace) -> int:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is required", file=sys.stderr)
        return 2
    if args.requests < 1:
        print("--requests must be at least 1", file=sys.stderr)
        return 2

    prompts = args.prompt or [
        "Request 1: do not call tools; reply with exactly: cache probe one",
        "Request 2: do not call tools; reply with exactly: cache probe two",
    ]
    static_context = "\n\n".join([STATIC_PREFIX] * args.prefix_repetitions)
    output_path = Path(args.output)
    raw_output_path = Path(args.raw_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("MINDWEFT_LLM_DEBUG_LOG_RESPONSES", "true")
    os.environ.setdefault("MINDWEFT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS", "10000000")
    os.environ["MINDWEFT_LLM_DEBUG_RESPONSE_LOG_PATH"] = str(raw_output_path)
    normalize_mindweft_env(os.environ)

    extra_headers: dict[str, str] = {}
    if args.site_url:
        extra_headers["HTTP-Referer"] = args.site_url
    if args.app_title:
        extra_headers["X-OpenRouter-Title"] = args.app_title

    adapter = OpenAICompatibleAdapter(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        extra_headers=extra_headers,
        timeout=args.timeout,
    )
    thread_id = f"adapter-cache-probe-{uuid4()}"
    try:
        tools = build_mock_tools(args.mock_tool_count)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"model={args.model}")
    print(f"base_url={args.base_url.rstrip('/')}")
    print(f"output={output_path}")
    print(f"raw_output={raw_output_path}")
    print(f"thread_id={thread_id}")
    print(f"static_context_chars={len(static_context)}")
    print(f"mock_tool_count={len(tools)}")

    with output_path.open("a", encoding="utf-8") as out:
        for index in range(args.requests):
            user_prompt = prompts[index % len(prompts)]
            messages = build_messages(thread_id, static_context, user_prompt)
            started = time.time()
            response = await adapter.generate(messages, tools)
            elapsed = time.time() - started
            record = {
                "ts": time.time(),
                "index": index + 1,
                "elapsed_seconds": elapsed,
                "model": args.model,
                "base_url": args.base_url,
                "thread_id": thread_id,
                "request": {
                    "messages": [message.model_dump(mode="json") for message in messages],
                    "tools": [tool.model_dump(mode="json") for tool in tools],
                },
                "response": response.model_dump(mode="json"),
                "usage_summary": cache_summary(response.usage),
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()

            print(f"\n=== request {index + 1}/{args.requests} elapsed={elapsed:.2f}s ===")
            print("usage_summary=" + json.dumps(record["usage_summary"], sort_keys=True))
            print("content=" + repr(response.content))
            if response.tool_call:
                print("tool_call=" + response.tool_call.model_dump_json())

            if index + 1 < args.requests and args.pause > 0:
                await asyncio.sleep(args.pause)

    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run_probe(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
