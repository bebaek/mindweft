from __future__ import annotations

import argparse
import sys

from app.config import load_environment
from voice_daemon.audio import AudioCaptureConfig, MicrophoneRecorder
from voice_daemon.backends.manual_audio import ManualAudioActivationSource
from voice_daemon.backends.stdin_loop import ConsoleSpeechOutput, StdinActivationSource
from voice_daemon.config import VoiceDaemonConfig
from voice_daemon.minigent_client import MinigentClient
from voice_daemon.service import VoiceDaemon
from voice_daemon.stt import SpeechProviderConfig, build_transcription_adapter
from voice_daemon.vad import SileroVoiceActivityDetector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Minigent voice daemon scaffold with a wake phrase loop."
    )
    parser.add_argument(
        "--backend",
        choices=("stdin", "manual-audio"),
        default="stdin",
        help="Activation backend. `stdin` keeps the text wake-phrase scaffold, `manual-audio` uses the microphone after manual activation.",
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
    parser.add_argument(
        "--audio-device",
        default=None,
        help="Optional sounddevice input device name or index for manual-audio mode.",
    )
    parser.add_argument(
        "--stt-provider",
        choices=("openai", "openrouter"),
        default=None,
        help="Speech-to-text provider for manual-audio mode. Defaults to MINIGENT_VOICE_STT_PROVIDER.",
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
        stt_provider=args.stt_provider or env_config.stt_provider,
        skill_name=args.skill or env_config.skill_name,
        thread_id=args.thread_id or env_config.thread_id,
        audio_device=args.audio_device or env_config.audio_device,
        audio_sample_rate=env_config.audio_sample_rate,
        audio_block_size=env_config.audio_block_size,
        speech_silence_ms=env_config.speech_silence_ms,
        speech_max_seconds=env_config.speech_max_seconds,
        vad_threshold=env_config.vad_threshold,
        stt_model=env_config.stt_model,
        openai_api_key=env_config.openai_api_key,
        openai_base_url=env_config.openai_base_url,
        openrouter_api_key=env_config.openrouter_api_key,
        openrouter_base_url=env_config.openrouter_base_url,
        openrouter_http_referer=env_config.openrouter_http_referer,
        openrouter_app_name=env_config.openrouter_app_name,
        principal=principal,
    )


def main(argv: list[str] | None = None) -> int:
    load_environment()
    parser = build_parser()
    args = parser.parse_args(argv)
    config = build_config(args)
    activation_source = build_activation_source(args.backend, config)
    daemon = VoiceDaemon(
        wake_phrase=config.wake_phrase,
        activation_source=activation_source,
        minigent_client=MinigentClient(config),
        speech_output=ConsoleSpeechOutput(output_stream=sys.stdout),
    )
    if args.once:
        daemon.run_once()
        return 0
    daemon.run_forever()
    return 0


def build_activation_source(backend: str, config: VoiceDaemonConfig):
    if backend == "stdin":
        return StdinActivationSource(
            input_stream=sys.stdin,
            output_stream=sys.stdout,
        )
    if backend == "manual-audio":
        return ManualAudioActivationSource(
            input_stream=sys.stdin,
            output_stream=sys.stdout,
            recorder=MicrophoneRecorder(
                AudioCaptureConfig(
                    sample_rate=config.audio_sample_rate,
                    block_size=config.audio_block_size,
                    max_record_seconds=config.speech_max_seconds,
                    end_silence_ms=config.speech_silence_ms,
                    device=config.audio_device,
                ),
                detector=SileroVoiceActivityDetector(threshold=config.vad_threshold),
            ),
            transcriber=build_transcription_adapter(build_speech_provider_config(config)),
        )
    raise ValueError(f"Unsupported backend '{backend}'")


def build_speech_provider_config(config: VoiceDaemonConfig) -> SpeechProviderConfig:
    provider = config.stt_provider.lower()
    if provider == "openai":
        if not config.openai_api_key:
            raise SystemExit(
                "OPENAI_API_KEY is required when MINIGENT_VOICE_STT_PROVIDER=openai. "
                "Install voice deps with `uv sync --extra voice`."
            )
        return SpeechProviderConfig(
            provider=provider,
            model=config.stt_model,
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        )
    if provider == "openrouter":
        if not config.openrouter_api_key:
            raise SystemExit(
                "OPENROUTER_API_KEY is required when MINIGENT_VOICE_STT_PROVIDER=openrouter. "
                "Install voice deps with `uv sync --extra voice`."
            )
        return SpeechProviderConfig(
            provider=provider,
            model=config.stt_model,
            api_key=config.openrouter_api_key,
            base_url=config.openrouter_base_url,
            app_name=config.openrouter_app_name,
            http_referer=config.openrouter_http_referer,
        )
    raise SystemExit(
        f"Unsupported MINIGENT_VOICE_STT_PROVIDER '{config.stt_provider}'. "
        "Choose 'openai' or 'openrouter'."
    )


if __name__ == "__main__":
    raise SystemExit(main())
