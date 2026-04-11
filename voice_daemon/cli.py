from __future__ import annotations

import argparse
import sys

from voice_daemon.backends.stdin_loop import ConsoleSpeechOutput, StdinActivationSource
from voice_daemon.config import VoiceDaemonConfig
from voice_daemon.minigent_client import MinigentClient
from voice_daemon.service import VoiceDaemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Minigent voice daemon scaffold with a wake phrase loop."
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL for the Minigent API. Defaults to MINIGENT_BASE_URL or http://127.0.0.1:8000.",
    )
    parser.add_argument(
        "--wake-phrase",
        default=None,
        help="Wake phrase that activates the daemon. Defaults to MINIGENT_VOICE_WAKE_PHRASE.",
    )
    parser.add_argument(
        "--skill",
        default=None,
        help="Optional skill name when creating a new thread.",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Existing thread to continue instead of creating one on first activation.",
    )
    parser.add_argument(
        "--api-token",
        default=None,
        help="Bearer token for Minigent auth. If omitted, trusted dev headers are used.",
    )
    parser.add_argument("--user-id", default=None, help="User ID for trusted dev headers.")
    parser.add_argument("--tenant-id", default=None, help="Tenant ID for trusted dev headers.")
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Mark the trusted-header principal as admin.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Handle one activation and exit.",
    )
    return parser


def build_config(args: argparse.Namespace) -> VoiceDaemonConfig:
    env_config = VoiceDaemonConfig.from_env()
    principal = env_config.principal
    if any(value is not None for value in (args.api_token, args.user_id, args.tenant_id)) or args.admin:
        principal = type(principal)(
            user_id=args.user_id or principal.user_id,
            tenant_id=args.tenant_id or principal.tenant_id,
            is_admin=args.admin or principal.is_admin,
            api_token=args.api_token if args.api_token is not None else principal.api_token,
        )
    return VoiceDaemonConfig(
        base_url=(args.base_url or env_config.base_url).rstrip("/"),
        wake_phrase=(args.wake_phrase or env_config.wake_phrase).strip(),
        skill_name=args.skill or env_config.skill_name,
        thread_id=args.thread_id or env_config.thread_id,
        principal=principal,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = build_config(args)
    daemon = VoiceDaemon(
        wake_phrase=config.wake_phrase,
        activation_source=StdinActivationSource(
            input_stream=sys.stdin,
            output_stream=sys.stdout,
        ),
        minigent_client=MinigentClient(config),
        speech_output=ConsoleSpeechOutput(output_stream=sys.stdout),
    )
    if args.once:
        daemon.run_once()
        return 0
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
