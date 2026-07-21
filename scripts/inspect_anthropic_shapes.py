#!/usr/bin/env python3
"""Inspect Anthropic Messages API request and response shapes.

The script is safe to run without credentials in ``--dry-run`` mode. Live mode reads
``ANTHROPIC_API_KEY`` from the process environment, sends one or more small requests, and
prints structural paths/types by default. Pass ``--include-values`` only when raw prompt,
response, and thinking values are safe to display in the terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_PROMPT = "What is 19 + 23? Answer in one short sentence."


def _shape_paths(value: Any, path: str = "$") -> Iterator[str]:
    if isinstance(value, dict):
        yield f"{path}: object[{len(value)}]"
        for key, child in value.items():
            yield from _shape_paths(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        yield f"{path}: array[{len(value)}]"
        if value:
            yield from _shape_paths(value[0], f"{path}[0]")
        return
    if isinstance(value, str):
        yield f"{path}: string(len={len(value)})"
        return
    yield f"{path}: {type(value).__name__}"


def _request_payload(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": args.prompt}],
    }
    if mode == "manual":
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": args.budget_tokens,
        }
    elif mode == "adaptive":
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        payload["output_config"] = {"effort": args.effort}
    return payload


def _print_document(label: str, value: Any, *, include_values: bool) -> None:
    print(f"\n{label} shape")
    print("-" * (len(label) + 6))
    print("\n".join(_shape_paths(value)))
    if include_values:
        print(f"\n{label} values")
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _parse_response(response: httpx.Response) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError:
        return {"non_json_body": response.text}


def _modes(selected: str) -> list[str]:
    return ["none", "manual", "adaptive"] if selected == "all" else [selected]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--anthropic-version", default="2023-06-01")
    parser.add_argument("--mode", choices=("none", "manual", "adaptive", "all"), default="adaptive")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--budget-tokens", type=int, default=1024)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print request shapes without reading ANTHROPIC_API_KEY or making network calls.",
    )
    parser.add_argument(
        "--include-values",
        action="store_true",
        help="Print full request/response JSON; may expose prompt and thinking content.",
    )
    args = parser.parse_args()

    api_key = None if args.dry_run else os.getenv("ANTHROPIC_API_KEY")
    if not args.dry_run and not api_key:
        parser.error("live mode requires ANTHROPIC_API_KEY in the process environment")

    headers = {
        "content-type": "application/json",
        "anthropic-version": args.anthropic_version,
    }
    if api_key:
        headers["x-api-key"] = api_key

    failures = 0
    with httpx.Client(timeout=args.timeout) as client:
        for mode in _modes(args.mode):
            payload = _request_payload(args, mode)
            print(f"\n=== mode={mode} model={args.model} ===")
            _print_document("request", payload, include_values=args.include_values)
            if args.dry_run:
                continue

            try:
                response = client.post(
                    f"{args.base_url.rstrip('/')}/messages",
                    headers=headers,
                    json=payload,
                )
            except httpx.HTTPError as exc:
                failures += 1
                print(f"request failed before a response was received: {exc}", file=sys.stderr)
                continue

            print(
                f"\nHTTP {response.status_code} content-type={response.headers.get('content-type', '')}"
            )
            response_payload = _parse_response(response)
            _print_document("response", response_payload, include_values=args.include_values)
            if response.is_error:
                failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
