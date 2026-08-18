from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_pi_backend import main as run_backend_demo

SMOKE_EXPRESSION = "246813579 * 97531"
SMOKE_RESULT = "24071975173449"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ask the Pi peer backend to use Mindweft's MCP broker calculator tool."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", default="demo-user")
    parser.add_argument("--tenant-id", default="demo-tenant")
    parser.add_argument("--api-token", default=None)
    parser.add_argument("--expression", default=SMOKE_EXPRESSION)
    parser.add_argument("--expected-result", default=SMOKE_RESULT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    command = [
        "--base-url",
        args.base_url,
        "--user-id",
        args.user_id,
        "--tenant-id",
        args.tenant_id,
        "--message",
        (
            "Use the Mindweft MCP broker calculator tool to calculate "
            f"{args.expression}, then reply with only the numeric result. "
            "Do not edit files."
        ),
        "--expect-reply-contains",
        args.expected_result,
    ]
    if args.api_token:
        command.extend(["--api-token", args.api_token])
    return run_backend_demo(command)


if __name__ == "__main__":
    raise SystemExit(main())
