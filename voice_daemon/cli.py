from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from app.config import load_environment
from voice_daemon.audio import AudioCaptureConfig, MicrophoneRecorder, open_microphone_stream
from voice_daemon.backends.manual_audio import ManualAudioActivationSource
from voice_daemon.backends.passive_audio import PassiveAudioActivationSource
from voice_daemon.backends.stdin_loop import StdinActivationSource
from voice_daemon.config import VoiceDaemonConfig
from voice_daemon.debug import CaptureDebugConfig, CaptureDebugger
from voice_daemon.ducking import MacOsAmbientVolumeDucker
from voice_daemon.minigent_client import MinigentClient
from voice_daemon.ring_buffer import AudioRingBuffer
from voice_daemon.service import VoiceDaemon
from voice_daemon.speech import ConsoleSpeechOutput, MacOsSaySpeechOutput, PiperSpeechOutput
from voice_daemon.stt import SpeechProviderConfig, build_transcription_adapter
from voice_daemon.vad import SileroVoiceActivityDetector
from voice_daemon.wakeword import OpenWakeWordDetector, PorcupineWakeWordDetector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Minigent voice daemon scaffold with a wake phrase loop."
    )
    parser.add_argument(
        "--backend",
        choices=("stdin", "manual-audio", "passive-audio"),
        default="stdin",
        help="Activation backend. `stdin` keeps the text wake-phrase scaffold, `manual-audio` uses the microphone after manual activation, and `passive-audio` adds continuous wake-word listening.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL for the Minigent API. Defaults to MINIGENT_BASE_URL or http://127.0.0.1:8000.",
    )
    parser.add_argument(
        "--wake-phrase",
        default=None,
        help="Text wake phrase for the stdin backend. Passive-audio uses the configured wake-word provider/model instead.",
    )
    parser.add_argument(
        "--wake-acknowledgement",
        default=None,
        help="Optional cue to emit after activation before recording. Use `bell` for a terminal bell or plain text for a spoken acknowledgement.",
    )
    parser.add_argument(
        "--capture-ended-acknowledgement",
        default=None,
        help="Optional cue to emit after microphone capture ends before processing. Use `bell` for a short sound or plain text for a spoken acknowledgement.",
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
        help="Optional sounddevice input device name or index for audio backends.",
    )
    parser.add_argument(
        "--debug-capture-path",
        default=None,
        help="Optional path to write the last captured WAV for debugging manual/passive audio backends.",
    )
    parser.add_argument(
        "--stt-debug-path",
        default=None,
        help="Optional directory to write STT request/response debug artifacts.",
    )
    parser.add_argument(
        "--ducking-mode",
        choices=("off", "input-only"),
        default=None,
        help="Optional ambient audio ducking mode. `input-only` lowers macOS output volume while the daemon is actively listening after activation.",
    )
    parser.add_argument(
        "--ducked-output-volume",
        default=None,
        type=bounded_output_volume,
        help="Optional macOS system output volume to use while ducking, from 0 to 100.",
    )
    parser.add_argument(
        "--follow-up-timeout-ms",
        default=None,
        type=int,
        help="Optional follow-up listening window after assistant speech. In passive-audio mode, speech during this window does not require the wake word.",
    )
    parser.add_argument(
        "--stt-provider",
        choices=("openai", "openrouter", "faster-whisper"),
        default=None,
        help="Speech-to-text provider for audio backends. Defaults to MINIGENT_VOICE_STT_PROVIDER.",
    )
    parser.add_argument(
        "--stt-device",
        default=None,
        help="Optional STT device override, for example `cpu` or `cuda`. Used by local providers such as faster-whisper.",
    )
    parser.add_argument(
        "--stt-compute-type",
        default=None,
        help="Optional STT compute type override such as `int8` or `float16`. Used by local providers such as faster-whisper.",
    )
    parser.add_argument(
        "--stt-language",
        default=None,
        help="Optional STT language hint such as `en`. When unset, providers use their default language detection behavior.",
    )
    parser.add_argument(
        "--tts-provider",
        choices=("console", "say", "piper"),
        default=None,
        help="Speech output provider. Defaults to MINIGENT_VOICE_TTS_PROVIDER.",
    )
    parser.add_argument(
        "--tts-voice",
        default=None,
        help="Optional voice name for the selected TTS provider. For `say`, this is the macOS voice passed to `say -v`.",
    )
    parser.add_argument(
        "--tts-model",
        default=None,
        help="Optional Piper model name or .onnx path when --tts-provider=piper. Defaults to MINIGENT_VOICE_TTS_MODEL.",
    )
    parser.add_argument(
        "--tts-model-dir",
        default=None,
        help="Optional directory for cached Piper voice models when --tts-provider=piper. Defaults to MINIGENT_VOICE_TTS_MODEL_DIR or ~/.cache/minigent/piper.",
    )
    parser.add_argument(
        "--tts-speaker",
        default=None,
        type=int,
        help="Optional speaker ID for multi-speaker Piper models. Defaults to MINIGENT_VOICE_TTS_SPEAKER.",
    )
    parser.add_argument(
        "--tts-length-scale",
        default=None,
        type=float,
        help="Optional Piper length scale. Values greater than 1.0 slow speech down. Defaults to MINIGENT_VOICE_TTS_LENGTH_SCALE.",
    )
    parser.add_argument(
        "--tts-sentence-silence",
        default=None,
        type=float,
        help="Optional Piper silence in seconds between detected sentences. Defaults to MINIGENT_VOICE_TTS_SENTENCE_SILENCE.",
    )
    parser.add_argument(
        "--wakeword-provider",
        choices=("porcupine", "openwakeword"),
        default=None,
        help="Wake-word provider for passive-audio mode. Defaults to MINIGENT_VOICE_WAKEWORD_PROVIDER.",
    )
    parser.add_argument(
        "--keyword-path",
        default=None,
        help="Path to the Porcupine keyword model file for passive-audio mode.",
    )
    parser.add_argument(
        "--oww-model",
        default=None,
        help="Built-in pyopen-wakeword model name for passive-audio mode, such as `okay_nabu`.",
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
        location=env_config.location,
        debug_show_prompt=env_config.debug_show_prompt,
        wake_acknowledgement=args.wake_acknowledgement or env_config.wake_acknowledgement,
        wake_acknowledgement_sound=env_config.wake_acknowledgement_sound,
        capture_ended_acknowledgement=(
            args.capture_ended_acknowledgement or env_config.capture_ended_acknowledgement
        ),
        capture_ended_acknowledgement_sound=env_config.capture_ended_acknowledgement_sound,
        stt_provider=args.stt_provider or env_config.stt_provider,
        stt_device=args.stt_device or env_config.stt_device,
        stt_compute_type=args.stt_compute_type or env_config.stt_compute_type,
        stt_language=args.stt_language or env_config.stt_language,
        tts_provider=args.tts_provider or env_config.tts_provider,
        tts_voice=args.tts_voice or env_config.tts_voice,
        tts_model=args.tts_model or env_config.tts_model,
        tts_model_dir=args.tts_model_dir or env_config.tts_model_dir,
        tts_speaker=args.tts_speaker if args.tts_speaker is not None else env_config.tts_speaker,
        tts_length_scale=(
            args.tts_length_scale
            if args.tts_length_scale is not None
            else env_config.tts_length_scale
        ),
        tts_sentence_silence=(
            args.tts_sentence_silence
            if args.tts_sentence_silence is not None
            else env_config.tts_sentence_silence
        ),
        wakeword_provider=args.wakeword_provider or env_config.wakeword_provider,
        skill_name=args.skill or env_config.skill_name,
        thread_id=args.thread_id or env_config.thread_id,
        audio_device=args.audio_device or env_config.audio_device,
        debug_capture_path=args.debug_capture_path or env_config.debug_capture_path,
        stt_debug_path=args.stt_debug_path or env_config.stt_debug_path,
        ducking_mode=args.ducking_mode or env_config.ducking_mode,
        ducked_output_volume=(
            args.ducked_output_volume
            if args.ducked_output_volume is not None
            else env_config.ducked_output_volume
        ),
        audio_sample_rate=env_config.audio_sample_rate,
        audio_block_size=env_config.audio_block_size,
        speech_silence_ms=env_config.speech_silence_ms,
        speech_max_seconds=env_config.speech_max_seconds,
        wakeword_cooldown_ms=env_config.wakeword_cooldown_ms,
        post_wake_speech_timeout_ms=env_config.post_wake_speech_timeout_ms,
        follow_up_timeout_ms=(
            args.follow_up_timeout_ms
            if args.follow_up_timeout_ms is not None
            else env_config.follow_up_timeout_ms
        ),
        post_wake_settle_ms=env_config.post_wake_settle_ms,
        wakeword_preroll_ms=env_config.wakeword_preroll_ms,
        stt_pad_leading_ms=env_config.stt_pad_leading_ms,
        stt_pad_trailing_ms=env_config.stt_pad_trailing_ms,
        vad_threshold=env_config.vad_threshold,
        stt_model=env_config.stt_model,
        openai_api_key=env_config.openai_api_key,
        openai_base_url=env_config.openai_base_url,
        openrouter_api_key=env_config.openrouter_api_key,
        openrouter_base_url=env_config.openrouter_base_url,
        openrouter_http_referer=env_config.openrouter_http_referer,
        openrouter_app_name=env_config.openrouter_app_name,
        picovoice_access_key=env_config.picovoice_access_key,
        porcupine_keyword_path=args.keyword_path or env_config.porcupine_keyword_path,
        openwakeword_model=args.oww_model or env_config.openwakeword_model,
        openwakeword_threshold=env_config.openwakeword_threshold,
        principal=principal,
    )


def main(argv: list[str] | None = None) -> int:
    load_environment()
    parser = build_parser()
    args = parser.parse_args(argv)
    config = build_config(args)
    speech_output = build_speech_output(config)
    ambient_volume_controller = build_ambient_volume_controller(config)
    activation_feedback = wrap_feedback_with_ambient_restore(
        build_acknowledgement_feedback(
            config.wake_acknowledgement,
            config.wake_acknowledgement_sound,
            speech_output,
            async_bell=args.backend == "passive-audio",
        ),
        ambient_volume_controller,
        reduck_delay_seconds=0.5 if args.backend == "passive-audio" else 0.0,
    )
    capture_ended_feedback = wrap_feedback_with_ambient_restore(
        build_acknowledgement_feedback(
            config.capture_ended_acknowledgement,
            config.capture_ended_acknowledgement_sound,
            speech_output,
        ),
        ambient_volume_controller,
    )
    activation_source = build_activation_source(
        args.backend,
        config,
        activation_feedback=activation_feedback,
        capture_ended_feedback=capture_ended_feedback,
    )
    daemon = VoiceDaemon(
        wake_phrase=config.wake_phrase,
        activation_source=activation_source,
        minigent_client=MinigentClient(config, output_stream=sys.stdout),
        speech_output=speech_output,
        activation_feedback=None if args.backend == "passive-audio" else activation_feedback,
        follow_up_timeout_ms=config.follow_up_timeout_ms,
        ambient_volume_controller=ambient_volume_controller,
    )
    try:
        if args.once:
            daemon.run_once()
            return 0
        daemon.run_forever()
        return 0
    except KeyboardInterrupt:
        sys.stdout.write("[idle] shutting down\n")
        sys.stdout.flush()
        return 130
    finally:
        close_daemon = getattr(daemon, "close", None)
        if callable(close_daemon):
            close_daemon()
        close = getattr(activation_source, "close", None)
        if callable(close):
            close()


def build_activation_source(
    backend: str,
    config: VoiceDaemonConfig,
    *,
    activation_feedback: Callable[[], None] | None = None,
    capture_ended_feedback: Callable[[], None] | None = None,
):
    if backend == "stdin":
        return StdinActivationSource(
            input_stream=sys.stdin,
            output_stream=sys.stdout,
        )
    if backend == "manual-audio":
        return ManualAudioActivationSource(
            input_stream=sys.stdin,
            output_stream=sys.stdout,
            recorder=build_microphone_recorder(config),
            transcriber=build_transcription_adapter(build_speech_provider_config(config)),
            capture_debugger=build_capture_debugger(config),
            capture_ended_feedback=capture_ended_feedback,
        )
    if backend == "passive-audio":
        wake_detector = build_wake_word_detector(config)
        recorder = build_microphone_recorder(config)
        stream_context = open_microphone_stream(
            AudioCaptureConfig(
                sample_rate=config.audio_sample_rate,
                block_size=wake_detector.frame_length,
                max_record_seconds=config.speech_max_seconds,
                end_silence_ms=config.speech_silence_ms,
                device=config.audio_device,
            )
        )
        return PassiveAudioActivationSource(
            output_stream=sys.stdout,
            stream=stream_context.__enter__(),
            recorder=recorder,
            transcriber=build_transcription_adapter(build_speech_provider_config(config)),
            wake_detector=wake_detector,
            preroll_buffer=AudioRingBuffer(
                max_bytes=max(
                    1,
                    int(
                        config.audio_sample_rate
                        * 2
                        * config.wakeword_preroll_ms
                        / 1000.0
                    ),
                )
            ),
            activation_feedback=activation_feedback,
            capture_debugger=build_capture_debugger(config),
            capture_ended_feedback=capture_ended_feedback,
            post_wake_speech_timeout_ms=config.post_wake_speech_timeout_ms,
            post_wake_settle_ms=config.post_wake_settle_ms,
            wakeword_cooldown_ms=config.wakeword_cooldown_ms,
            stt_pad_leading_ms=config.stt_pad_leading_ms,
            stt_pad_trailing_ms=config.stt_pad_trailing_ms,
            stream_context=stream_context,
        )
    raise ValueError(f"Unsupported backend '{backend}'")


def build_microphone_recorder(config: VoiceDaemonConfig) -> MicrophoneRecorder:
    return MicrophoneRecorder(
        AudioCaptureConfig(
            sample_rate=config.audio_sample_rate,
            block_size=config.audio_block_size,
            max_record_seconds=config.speech_max_seconds,
            end_silence_ms=config.speech_silence_ms,
            device=config.audio_device,
        ),
        detector=SileroVoiceActivityDetector(threshold=config.vad_threshold),
    )


def build_capture_debugger(config: VoiceDaemonConfig) -> CaptureDebugger | None:
    if not config.debug_capture_path:
        return None
    return CaptureDebugger(
        CaptureDebugConfig(capture_path=config.debug_capture_path),
        output_stream=sys.stdout,
    )


def build_ambient_volume_controller(config: VoiceDaemonConfig):
    if config.ducking_mode == "off":
        return None
    if config.ducking_mode != "input-only":
        raise SystemExit(
            f"Unsupported MINIGENT_VOICE_DUCKING_MODE '{config.ducking_mode}'. "
            "Choose 'off' or 'input-only'."
        )
    try:
        MacOsAmbientVolumeDucker.validate_platform()
    except RuntimeError as exc:
        sys.stdout.write(f"[warning] ambient audio ducking disabled: {exc}\n")
        sys.stdout.flush()
        return None
    return MacOsAmbientVolumeDucker(
        ducked_output_volume=config.ducked_output_volume,
        output_stream=sys.stdout,
    )


def build_speech_output(config: VoiceDaemonConfig):
    provider = config.tts_provider.lower()
    if provider == "console":
        return ConsoleSpeechOutput(output_stream=sys.stdout)
    if provider == "say":
        return MacOsSaySpeechOutput(output_stream=sys.stdout, voice=config.tts_voice)
    if provider == "piper":
        if not config.tts_model:
            raise SystemExit(
                "MINIGENT_VOICE_TTS_MODEL is required when MINIGENT_VOICE_TTS_PROVIDER=piper. "
                "Install voice deps with `uv sync --extra voice`."
            )
        return PiperSpeechOutput(
            output_stream=sys.stdout,
            model=config.tts_model,
            model_dir=config.tts_model_dir,
            speaker=config.tts_speaker,
            length_scale=config.tts_length_scale,
            sentence_silence=config.tts_sentence_silence,
        )
    raise SystemExit(
        f"Unsupported MINIGENT_VOICE_TTS_PROVIDER '{config.tts_provider}'. "
        "Choose 'console', 'say', or 'piper'."
    )


def build_activation_feedback(
    config: VoiceDaemonConfig,
    speech_output,
) -> Callable[[], None] | None:
    return build_acknowledgement_feedback(
        config.wake_acknowledgement,
        config.wake_acknowledgement_sound,
        speech_output,
    )


def build_acknowledgement_feedback(
    acknowledgement: str | None,
    sound_path: str | None,
    speech_output,
    *,
    async_bell: bool = False,
) -> Callable[[], None] | None:
    if not acknowledgement:
        return None
    if acknowledgement.lower() == "bell":
        if async_bell:
            return lambda: _emit_terminal_bell_async(sound_path)
        return lambda: _emit_terminal_bell(sound_path)
    return lambda: speech_output.speak(acknowledgement)


def wrap_feedback_with_ambient_restore(
    feedback: Callable[[], None] | None,
    ambient_volume_controller,
    *,
    reduck_delay_seconds: float = 0.0,
) -> Callable[[], None] | None:
    if feedback is None:
        return None
    temporarily_restore = getattr(ambient_volume_controller, "temporarily_restore", None)
    if not callable(temporarily_restore):
        return feedback
    return lambda: temporarily_restore(feedback, reduck_delay_seconds=reduck_delay_seconds)


def bounded_output_volume(value: str) -> int:
    parsed = int(value)
    if parsed < 0 or parsed > 100:
        raise argparse.ArgumentTypeError("ducked output volume must be between 0 and 100")
    return parsed


def _emit_terminal_bell(configured_sound_path: str | None = None) -> None:
    sound_path = _resolve_acknowledgement_sound(configured_sound_path)
    if sound_path is not None:
        for player_command in _resolve_acknowledgement_players(sound_path):
            command = player_command + [str(sound_path)]
            try:
                result = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                sys.stdout.write(
                    f"[warning] acknowledgement player failed: {command[0]}: {exc}\n"
                )
                sys.stdout.flush()
                continue
            if result.returncode == 0:
                return
            detail = result.stderr.strip()
            if detail:
                sys.stdout.write(
                    f"[warning] acknowledgement player failed: {command[0]}: {detail}\n"
                )
            else:
                sys.stdout.write(
                    f"[warning] acknowledgement player failed: {command[0]} exited {result.returncode}\n"
                )
            sys.stdout.flush()
        sys.stdout.write(f"[warning] no acknowledgement player could play {sound_path}\n")
        sys.stdout.flush()
    sys.stdout.write("\a")
    sys.stdout.flush()


def _emit_terminal_bell_async(configured_sound_path: str | None = None) -> None:
    threading.Thread(
        target=_emit_terminal_bell,
        args=(configured_sound_path,),
        daemon=True,
    ).start()


def _resolve_wake_acknowledgement_sound() -> Path | None:
    return _resolve_acknowledgement_sound(VoiceDaemonConfig.from_env().wake_acknowledgement_sound)


def _resolve_acknowledgement_sound(configured_sound_path: str | None) -> Path | None:
    configured = configured_sound_path
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return candidate
    for candidate in _default_wake_acknowledgement_sounds():
        if candidate.exists():
            return candidate
    return None


def _default_wake_acknowledgement_sounds() -> list[Path]:
    system = platform.system()
    if system == "Darwin":
        return [Path("/System/Library/Sounds/Glass.aiff")]
    if system == "Linux":
        return [
            Path("/usr/share/sounds/alsa/Front_Center.wav"),
            Path("/usr/share/sounds/sound-icons/glass-water-1.wav"),
            Path("/usr/share/sounds/freedesktop/stereo/complete.oga"),
            Path("/usr/share/sounds/freedesktop/stereo/bell.oga"),
        ]
    return []


def _resolve_wake_acknowledgement_player(sound_path: Path | None) -> list[str] | None:
    return _resolve_acknowledgement_player(sound_path)


def _resolve_acknowledgement_player(sound_path: Path | None) -> list[str] | None:
    players = _resolve_acknowledgement_players(sound_path)
    if not players:
        return None
    return players[0]


def _resolve_acknowledgement_players(sound_path: Path | None) -> list[list[str]]:
    if sound_path is None:
        return []
    system = platform.system()
    if system == "Darwin":
        afplay = shutil.which("afplay")
        if afplay:
            return [[afplay]]
        return []
    if system == "Linux":
        suffix = sound_path.suffix.lower()
        if suffix in {".oga", ".ogg", ".opus", ".mp3"}:
            players = ("paplay", "play", "ffplay")
        elif suffix in {".wav", ".wave"}:
            players = ("aplay", "paplay", "play", "ffplay")
        else:
            players = ("paplay", "play", "ffplay")
        commands: list[list[str]] = []
        for player in players:
            resolved = shutil.which(player)
            if not resolved:
                continue
            if player == "ffplay":
                commands.append([resolved, "-nodisp", "-autoexit", "-loglevel", "quiet"])
            else:
                commands.append([resolved])
        return commands
    return []


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
            debug_path=config.stt_debug_path,
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
            debug_path=config.stt_debug_path,
        )
    if provider == "faster-whisper":
        return SpeechProviderConfig(
            provider=provider,
            model=config.stt_model,
            device=config.stt_device,
            compute_type=config.stt_compute_type,
            language=config.stt_language,
            debug_path=config.stt_debug_path,
        )
    raise SystemExit(
        f"Unsupported MINIGENT_VOICE_STT_PROVIDER '{config.stt_provider}'. "
        "Choose 'openai', 'openrouter', or 'faster-whisper'."
    )


def build_wake_word_detector(config: VoiceDaemonConfig):
    if config.wakeword_provider == "porcupine":
        if not config.picovoice_access_key:
            raise SystemExit(
                "PICOVOICE_ACCESS_KEY is required for passive-audio mode with Porcupine."
            )
        if not config.porcupine_keyword_path:
            raise SystemExit(
                "MINIGENT_VOICE_KEYWORD_PATH is required for passive-audio mode with Porcupine."
            )
        return PorcupineWakeWordDetector(
            access_key=config.picovoice_access_key,
            keyword_path=config.porcupine_keyword_path,
        )
    if config.wakeword_provider == "openwakeword":
        return OpenWakeWordDetector(
            model_name=config.openwakeword_model,
            threshold=config.openwakeword_threshold,
        )
    raise SystemExit(
        f"Unsupported MINIGENT_VOICE_WAKEWORD_PROVIDER '{config.wakeword_provider}'. "
        "Choose 'porcupine' or 'openwakeword'."
    )


if __name__ == "__main__":
    raise SystemExit(main())
