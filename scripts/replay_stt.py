from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from minigent_client.audio import (
    RecordedAudio,
    apply_gain,
    load_recorded_audio_from_wav,
    normalize_peak,
    pad_with_silence,
)
from minigent_client.stt import SpeechProviderConfig, build_transcription_adapter
from minigent_config.environment import load_environment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay a prerecorded WAV file through the Mindweft STT adapters."
    )
    parser.add_argument("wav_path", help="Path to a mono 16-bit PCM WAV file.")
    parser.add_argument(
        "--provider",
        choices=("openai", "openrouter", "faster-whisper"),
        default=os.getenv("MINIGENT_VOICE_STT_PROVIDER", "openai"),
        help="Speech-to-text provider to use.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the STT model. Defaults to the provider-specific env/config convention.",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Only print WAV metadata without making an STT request.",
    )
    parser.add_argument(
        "--stt-debug-path",
        default=os.getenv("MINIGENT_VOICE_STT_DEBUG_PATH"),
        help="Optional directory to write STT request/response debug artifacts.",
    )
    parser.add_argument(
        "--gain",
        type=float,
        default=float(os.getenv("MINIGENT_VOICE_STT_GAIN", "1.0")),
        help="Optional gain multiplier applied before STT.",
    )
    parser.add_argument(
        "--normalize-peak",
        action="store_true",
        help="Normalize peak level before STT.",
    )
    parser.add_argument(
        "--pad-leading-ms",
        type=int,
        default=int(os.getenv("MINIGENT_VOICE_STT_PAD_LEADING_MS", "0")),
        help="Prepend silence before STT.",
    )
    parser.add_argument(
        "--pad-trailing-ms",
        type=int,
        default=int(os.getenv("MINIGENT_VOICE_STT_PAD_TRAILING_MS", "0")),
        help="Append silence before STT.",
    )
    return parser.parse_args()


def main() -> int:
    load_environment()
    args = parse_args()
    audio = load_recorded_audio_from_wav(args.wav_path)
    if args.normalize_peak:
        audio, applied_gain = normalize_peak(audio)
        print(f"normalize_peak_gain={applied_gain:.3f}")
    elif abs(args.gain - 1.0) > 1e-9:
        audio = apply_gain(audio, args.gain)
        print(f"gain_multiplier={args.gain:.3f}")
    if args.pad_leading_ms or args.pad_trailing_ms:
        audio = pad_with_silence(
            audio,
            leading_ms=args.pad_leading_ms,
            trailing_ms=args.pad_trailing_ms,
        )
        print(f"pad_leading_ms={args.pad_leading_ms} pad_trailing_ms={args.pad_trailing_ms}")
    print_metadata(Path(args.wav_path), audio)
    if args.metadata_only:
        return 0
    provider_config = build_provider_config(args.provider, args.model, args.stt_debug_path)
    transcript = build_transcription_adapter(provider_config).transcribe(audio)
    print(f"provider={provider_config.provider} model={provider_config.model}")
    print(f"transcript={transcript}")
    return 0


def print_metadata(path: Path, audio: RecordedAudio) -> None:
    print(
        f"path={path} duration_s={audio.duration_seconds:.2f} bytes={len(audio.pcm_bytes)} "
        f"sample_rate={audio.sample_rate} channels={audio.channels} "
        f"nonzero_samples={audio.nonzero_samples} peak_dbfs={audio.peak_dbfs:.2f} "
        f"rms_dbfs={audio.rms_dbfs:.2f}"
    )


def build_provider_config(
    provider: str, model: str | None, stt_debug_path: str | None
) -> SpeechProviderConfig:
    normalized = provider.strip().lower()
    if normalized == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit("OPENAI_API_KEY is required for --provider openai")
        return SpeechProviderConfig(
            provider="openai",
            model=model or os.getenv("MINIGENT_VOICE_STT_MODEL", "gpt-4o-mini-transcribe"),
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            debug_path=stt_debug_path,
        )
    if normalized == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise SystemExit("OPENROUTER_API_KEY is required for --provider openrouter")
        return SpeechProviderConfig(
            provider="openrouter",
            model=model or os.getenv("MINIGENT_VOICE_STT_MODEL", "openai/gpt-audio"),
            api_key=api_key,
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            app_name=os.getenv("OPENROUTER_APP_NAME"),
            http_referer=os.getenv("OPENROUTER_HTTP_REFERER"),
            debug_path=stt_debug_path,
        )
    if normalized == "faster-whisper":
        return SpeechProviderConfig(
            provider="faster-whisper",
            model=model or os.getenv("MINIGENT_VOICE_STT_MODEL", "base"),
            device=os.getenv("MINIGENT_VOICE_STT_DEVICE"),
            compute_type=os.getenv("MINIGENT_VOICE_STT_COMPUTE_TYPE"),
            language=os.getenv("MINIGENT_VOICE_STT_LANGUAGE"),
            debug_path=stt_debug_path,
        )
    raise SystemExit(f"Unsupported provider '{provider}'")


if __name__ == "__main__":
    sys.exit(main())
