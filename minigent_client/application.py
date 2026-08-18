from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path
from typing import Sequence

from minigent_client.chat_commands import _read_run_message, build_client, build_config
from minigent_client.command_router import dispatch_command
from minigent_client.errors import MindweftAPIError
from minigent_client.one_shot_parser import build_parser
from minigent_client.output import print_json
from minigent_config.unified_config import preferred_mindweft_env


def _abort_detail(args: argparse.Namespace) -> tuple[str, bool]:
    server_cancelled = bool(getattr(args, "stream", False))
    if server_cancelled:
        return "server cancellation requested", server_cancelled
    return "server cancellation unavailable for non-streaming runs", server_cancelled


def _print_abort_message(args: argparse.Namespace) -> None:
    detail, server_cancelled = _abort_detail(args)
    if getattr(args, "json", False):
        print_json(
            {
                "error": {
                    "message": "Run aborted locally.",
                    "category": "aborted",
                    "server_cancelled": server_cancelled,
                    "detail": detail,
                }
            }
        )
        return
    print(f"[idle] locally aborted current run; {detail}.", file=sys.stderr)


def _apply_cli_env_file(args: argparse.Namespace) -> None:
    env_file = getattr(args, "env_file", None)
    if not env_file:
        return
    path = Path(env_file).expanduser()
    # Internal consumers still key off the legacy alias; startup normalization gives the
    # canonical MINDWEFT_DOTENV_FILE value precedence when users configure it directly.
    os.environ["MINIGENT_DOTENV_FILE"] = str(path)
    if not path.exists():
        return
    from dotenv import dotenv_values

    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)
    configured_base_url = preferred_mindweft_env("BASE_URL")
    if args.base_url == "http://127.0.0.1:8000" and configured_base_url:
        args.base_url = configured_base_url


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _apply_cli_env_file(args)

    trace_id = secrets.token_hex(16) if args.trace else None
    if args.command == "run":
        args.message = _read_run_message(args)
    config = build_config(args, trace_id)
    client = build_client(args, trace_id)

    try:
        result = dispatch_command(args, client, config, trace_id)
        if result is not None:
            return result
    except KeyboardInterrupt:
        try:
            client.cancel_current_run()
        except Exception:
            pass
        _print_abort_message(args)
        return 130
    except MindweftAPIError as exc:
        if args.json:
            print_json({"error": exc.to_dict(include_detail=args.verbose)})
        else:
            print(f"Error: {exc.message}", file=sys.stderr)
            if args.verbose and exc.detail:
                print(f"Detail: {exc.detail}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unhandled command: {args.command}")
    return 2
