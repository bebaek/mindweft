from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_PROMPTS = [
    "Read the first 200 lines of README.md and summarize them in one paragraph.",
    "Using the README.md content you just read, summarize the setup steps.",
    "Using the same README.md context, list the CLI commands.",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Drive repeated streamed Mindweft runs and print provider usage/cache counters. "
            "For raw provider payloads, start the Mindweft server with "
            "MINDWEFT_LLM_DEBUG_LOG_RESPONSES=true."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--thread-id", default=None, help="Existing thread to continue.")
    parser.add_argument("--skill-name", default=None, help="Skill to apply when creating a thread.")
    parser.add_argument(
        "--skill-names",
        nargs="+",
        default=None,
        help="Ordered prompt-overlay skills to apply when creating a thread.",
    )
    parser.add_argument(
        "--capability-profile",
        default=None,
        help="Capability profile to apply when creating a thread.",
    )
    parser.add_argument("--user-id", default="demo-user")
    parser.add_argument("--tenant-id", default="demo-tenant")
    parser.add_argument("--admin", action="store_true")
    parser.add_argument("--api-token", default=None, help="Bearer token for API auth.")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Send traceparent headers to correlate this script with server logs.",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="Seconds to sleep between runs; useful if the provider cache is eventually consistent.",
    )
    parser.add_argument(
        "--show-events",
        action="store_true",
        help="Print every NDJSON event as it arrives.",
    )
    parser.add_argument(
        "prompts",
        nargs="*",
        help="Prompts to run in sequence. Defaults to a README.md cache probe.",
    )
    return parser.parse_args(argv)


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
            body = response.read().decode("utf-8")
            if not body:
                return None
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} failed: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{method} {url} failed: {exc.reason}") from exc


def stream_run(
    url: str,
    *,
    headers: dict[str, str],
    show_events: bool,
) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, method="POST", headers=headers)
    events: list[dict[str, Any]] = []
    try:
        with urllib.request.urlopen(request) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                event = json.loads(line)
                events.append(event)
                if show_events:
                    print(json.dumps(event, ensure_ascii=False, sort_keys=True))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"POST {url} failed: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"POST {url} failed: {exc.reason}") from exc
    return events


def ensure_thread(base_url: str, args: argparse.Namespace, headers: dict[str, str]) -> str:
    if args.thread_id:
        return args.thread_id
    if args.skill_name is not None and args.skill_names is not None:
        raise SystemExit("Provide either --skill-name or --skill-names, not both.")
    payload: dict[str, Any] = {}
    if args.skill_name is not None:
        payload["skill_name"] = args.skill_name
    if args.skill_names is not None:
        payload["skill_names"] = args.skill_names
    if args.capability_profile is not None:
        payload["capability_profile"] = args.capability_profile
    response = request_json("POST", f"{base_url}/threads", payload=payload, headers=headers)
    return str(response["thread_id"])


def build_auth_headers(args: argparse.Namespace) -> dict[str, str]:
    if args.api_token:
        return {"Authorization": f"Bearer {args.api_token}"}
    return {
        "X-Mindweft-User-Id": args.user_id,
        "X-Mindweft-Tenant-Id": args.tenant_id,
        "X-Mindweft-Admin": "true" if args.admin else "false",
    }


def build_trace_headers(trace_id: str | None) -> dict[str, str]:
    if trace_id is None:
        return {}
    return {"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"}


def usages_from_events(events: list[dict[str, Any]]) -> list[dict[str, int]]:
    usages: list[dict[str, int]] = []
    for event in events:
        event_usage = event.get("usage")
        if not isinstance(event_usage, dict):
            continue
        normalized = {key: value for key, value in event_usage.items() if isinstance(value, int)}
        if normalized:
            usages.append(normalized)
    return usages


def usage_from_events(events: list[dict[str, Any]]) -> dict[str, int] | None:
    usages = usages_from_events(events)
    return usages[-1] if usages else None


def thread_context_from_events(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        context = event.get("thread_context")
        if isinstance(context, dict):
            return context
    return None


def format_count(value: int | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def print_run_summary(index: int, events: list[dict[str, Any]]) -> None:
    usages = usages_from_events(events)
    usage = usages[-1] if usages else {}
    cache_reads = [_int_or_none(item.get("cache_read_tokens")) or 0 for item in usages]
    cache_writes = [_int_or_none(item.get("cache_write_tokens")) or 0 for item in usages]
    context = thread_context_from_events(events) or {}
    types = [str(event.get("type", "")) for event in events]
    print(
        "summary"
        f" run={index}"
        f" events={','.join(types)}"
        f" context={format_count(_int_or_none(context.get('total_tokens')))}"
        f" llm_calls={len(usages)}"
        f" final_prompt={format_count(_int_or_none(usage.get('prompt_tokens') or usage.get('input_tokens')))}"
        f" final_completion={format_count(_int_or_none(usage.get('completion_tokens') or usage.get('output_tokens')))}"
        f" final_total={format_count(_int_or_none(usage.get('total_tokens')))}"
        f" final_cache_read={format_count(_int_or_none(usage.get('cache_read_tokens')))}"
        f" cache_read_total={format_count(sum(cache_reads))}"
        f" cache_read_max={format_count(max(cache_reads, default=0))}"
        f" cache_write_total={format_count(sum(cache_writes))}"
    )
    if len(usages) > 1:
        per_call = ",".join(
            f"{format_count(_int_or_none(item.get('prompt_tokens') or item.get('input_tokens')))}"
            f"/{format_count(_int_or_none(item.get('cache_read_tokens')))}"
            for item in usages
        )
        print(f"llm_call_prompt/cache_read={per_call}")


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_url = args.base_url.rstrip("/")
    trace_id = secrets.token_hex(16) if args.trace else None
    headers = {**build_auth_headers(args), **build_trace_headers(trace_id)}
    prompts = args.prompts or DEFAULT_PROMPTS

    print("Prompt cache investigation")
    print(f"base_url={base_url}")
    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print(
        "raw response logging: start the API with "
        "MINDWEFT_LLM_DEBUG_LOG_RESPONSES=true "
        "and inspect app.llm logs for matching requests."
    )

    config = request_json("GET", f"{base_url}/config", headers=build_trace_headers(trace_id))
    llm = config.get("llm", {}) if isinstance(config, dict) else {}
    print(f"llm={json.dumps(llm, ensure_ascii=False, sort_keys=True)}")

    thread_id = ensure_thread(base_url, args, headers)
    print(f"thread_id={thread_id}")

    for index, prompt in enumerate(prompts, start=1):
        if index > 1 and args.pause > 0:
            time.sleep(args.pause)
        print(f"\n[{index}] user: {prompt}")
        request_json(
            "POST",
            f"{base_url}/threads/{thread_id}/messages",
            {"content": prompt, "metadata": {"raw_user_prompt": prompt}},
            headers=headers,
        )
        events = stream_run(
            f"{base_url}/threads/{thread_id}/run/stream",
            headers=headers,
            show_events=args.show_events,
        )
        assistant = next(
            (event.get("content") for event in events if event.get("type") == "assistant.message"),
            None,
        )
        print_run_summary(index, events)
        if isinstance(assistant, str):
            print(f"assistant_preview={assistant[:240].replace(chr(10), ' ')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
