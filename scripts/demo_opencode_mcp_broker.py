from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_opencode_backend import main as run_backend_demo

SMOKE_TEXT = "minigent-broker-smoke-ok"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ask the OpenCode peer backend to use Mindweft's MCP broker echo tool."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", default="demo-user")
    parser.add_argument("--tenant-id", default="demo-tenant")
    parser.add_argument("--api-token", default=None)
    parser.add_argument("--smoke-text", default=SMOKE_TEXT)
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
            "Use the Mindweft MCP broker echo tool with text "
            f"'{args.smoke_text}', then reply with only the echoed text. "
            "Do not edit files."
        ),
        "--expect-reply-contains",
        args.smoke_text,
    ]
    if args.api_token:
        command.extend(["--api-token", args.api_token])
    return run_backend_demo(command)


if __name__ == "__main__":
    raise SystemExit(main())
