from __future__ import annotations

import argparse
import base64
import hashlib
import mimetypes
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Protocol, TextIO, cast

from app.config import load_environment
from minigent_client.api_client import MinigentAPIClient
from minigent_client.audio import AudioCaptureConfig, MicrophoneRecorder, open_microphone_stream
from minigent_client.backends.manual_audio import ManualAudioActivationSource
from minigent_client.backends.passive_audio import PassiveAudioActivationSource
from minigent_client.backends.stdin_loop import StdinActivationSource
from minigent_client.config import AgentPreset, ClientConfig, build_client_config
from minigent_client.debug import CaptureDebugConfig, CaptureDebugger
from minigent_client.ducking import MacOsAmbientVolumeDucker
from minigent_client.errors import (
    MinigentAPIError,
    format_stream_run_error_summary,
    is_stream_run_error,
)
from minigent_client.one_shot_cli import _format_execution_options, _format_markdown_transcript
from minigent_client.output import (
    estimate_thread_token_usage,
    extract_reasoning_content,
    format_reasoning_block,
    format_usage_summary,
    style_text,
)
from minigent_client.ring_buffer import AudioRingBuffer
from minigent_client.runtime import ActivationSource, MinigentClientRuntime
from minigent_client.speech import (
    ConsoleSpeechOutput,
    MacOsSaySpeechOutput,
    PiperSpeechOutput,
    SilentSpeechOutput,
)
from minigent_client.state import (
    ClientState as PersistentClientState,
)
from minigent_client.state import (
    PromptCommand,
    ThreadHistoryItem,
    legacy_state_dir_path,
    normalize_prompt_command_name,
    state_dir_path,
    state_scope_key,
)
from minigent_client.stt import SpeechProviderConfig, build_transcription_adapter
from minigent_client.vad import SileroVoiceActivityDetector
from minigent_client.wakeword import OpenWakeWordDetector, PorcupineWakeWordDetector

CHAT_HISTORY_FILE_NAME = "client-chat-history"
CHAT_HISTORY_DIR_NAME = f"{CHAT_HISTORY_FILE_NAME}.d"


class ChatInputStream(Protocol):
    def isatty(self) -> bool: ...
    def readline(self) -> str: ...


class ChatOutputStream(Protocol):
    def isatty(self) -> bool: ...
    def write(self, __text: str) -> object: ...
    def flush(self) -> object: ...


class ChatPromptSession(Protocol):
    def prompt(self, message: object) -> str: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Minigent client for chat and voice.")
    parser.add_argument(
        "--backend",
        choices=("chat", "stdin", "manual-audio", "passive-audio"),
        default="stdin",
        help="Interaction backend. `chat` is a plain terminal chat loop, `stdin` keeps the text wake-phrase scaffold, `manual-audio` uses the microphone after manual activation, and `passive-audio` adds continuous wake-word listening.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a TOML client config file. Defaults to MINIGENT_CLIENT_CONFIG, $XDG_CONFIG_HOME/minigent/client.toml (or ~/.config/minigent/client.toml), legacy ~/.minigent/client.toml, or ./.minigent-client.toml.",
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
        "--stream-runs",
        action="store_true",
        help="Use Minigent's NDJSON run stream endpoint and print run/tool/peer progress.",
    )
    parser.add_argument(
        "--show-tool-results",
        action="store_true",
        help="With streaming runs, print expanded tool result bodies to stderr.",
    )
    parser.add_argument(
        "--show-reasoning",
        action="store_true",
        help="Show model reasoning/thinking content when available.",
    )
    parser.add_argument(
        "--tokens",
        choices=("auto", "live", "off"),
        default=None,
        help="Token display mode for streaming progress and the interactive /tokens command.",
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
    thread_target_group = parser.add_mutually_exclusive_group()
    thread_target_group.add_argument(
        "--thread-id",
        default=None,
        help="Existing thread to continue instead of creating one on first activation.",
    )
    thread_target_group.add_argument(
        "--resume-last",
        action="store_true",
        help="Resume the last locally remembered thread for this server and principal.",
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
        "--chat-submit-mode",
        choices=("enter", "alt-enter"),
        default=None,
        help="Interactive chat submit behavior. `enter` submits and Esc+Enter/Ctrl+J insert newlines; `alt-enter` makes Enter/Ctrl+J insert newlines and Esc+Enter submit.",
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
        help="Optional ambient audio ducking mode. `input-only` lowers macOS output volume while the client is actively listening after activation.",
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
        choices=("none", "console", "say", "piper"),
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


def build_config(args: argparse.Namespace) -> ClientConfig:
    env_config = ClientConfig.from_env(config_path=args.config)
    env_config = build_client_config(
        base_url=args.base_url,
        api_token=args.api_token,
        user_id=args.user_id,
        tenant_id=args.tenant_id,
        admin=args.admin if args.admin else None,
        stream_runs=args.stream_runs or None,
        wake_phrase=args.wake_phrase,
        env_config=env_config,
    )
    principal = env_config.principal
    config = ClientConfig(
        base_url=env_config.base_url,
        wake_phrase=env_config.wake_phrase,
        prompt_preamble=env_config.prompt_preamble,
        location=env_config.location,
        debug_show_prompt=env_config.debug_show_prompt,
        stream_runs=args.stream_runs or env_config.stream_runs,
        show_tool_results=args.show_tool_results or env_config.show_tool_results,
        show_reasoning=args.show_reasoning or env_config.show_reasoning,
        token_mode=args.tokens or env_config.token_mode,
        chat_submit_mode=args.chat_submit_mode or env_config.chat_submit_mode,
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
        agent_presets=env_config.agent_presets,
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
        extra_headers=env_config.extra_headers,
        resume_last=args.resume_last,
        config_path=env_config.config_path,
    )
    if args.resume_last:
        config = replace(config, thread_id=load_remembered_client_thread(config))
    return config


def client_state_scope_key(config: ClientConfig) -> str:
    return state_scope_key(
        config.base_url,
        api_token=config.principal.api_token,
        user_id=config.principal.user_id,
        tenant_id=config.principal.tenant_id,
        is_admin=config.principal.is_admin,
    )


def load_remembered_client_thread(config: ClientConfig) -> str:
    thread_id = PersistentClientState.load().get_last_thread(client_state_scope_key(config))
    if thread_id is None:
        raise SystemExit("No remembered thread for this server and principal. Start a chat first.")
    return thread_id


def remember_client_thread(
    config: ClientConfig,
    thread_id: str,
    *,
    title: str | None = None,
    message_count: int | None = None,
) -> None:
    state = PersistentClientState.load()
    state.set_last_thread(
        client_state_scope_key(config), thread_id, title=title, message_count=message_count
    )
    state.save()


def _thread_title_from_message(message: str) -> str:
    normalized = " ".join(message.split())
    if len(normalized) <= 60:
        return normalized or "Thread"
    return f"{normalized[:57]}..."


def _rename_client_thread(config: ClientConfig, thread_id: str, title: str) -> bool:
    state = PersistentClientState.load()
    changed = state.rename_thread(client_state_scope_key(config), thread_id, title)
    if changed:
        state.save()
    return changed


def _list_client_threads(config: ClientConfig):
    return PersistentClientState.load().list_threads(client_state_scope_key(config))


def forget_remembered_client_thread(config: ClientConfig, thread_id: str) -> bool:
    state = PersistentClientState.load()
    changed = state.forget_last_thread(client_state_scope_key(config), thread_id)
    if changed:
        state.save()
    return changed


class RememberingMinigentAPIClient:
    def __init__(self, client: MinigentAPIClient, config: ClientConfig) -> None:
        self._client = client
        self._remembering_config = config
        self.active_agent_preset: str | None = None
        self.active_agent_preset_config: AgentPreset | None = None
        self.active_llm_profile: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    @property
    def thread_id(self) -> str | None:
        thread_id = getattr(self._client, "thread_id", None)
        return thread_id if isinstance(thread_id, str) else None

    def create_thread(
        self,
        *,
        agent_name: str | None = None,
        skill_name: str | None = None,
        skills: list[str] | None = None,
        capability_profile: str | None = None,
        llm_profile: str | None = None,
    ) -> dict[str, Any]:
        create_kwargs: dict[str, object] = {
            "skill_name": skill_name,
            "skills": skills,
            "capability_profile": capability_profile,
        }
        if agent_name is not None:
            create_kwargs["agent_name"] = agent_name
        if llm_profile is not None:
            create_kwargs["llm_profile"] = llm_profile
        response = self._client.create_thread(**create_kwargs)  # type: ignore[attr-defined]
        self.active_agent_preset = None
        self.active_agent_preset_config = None
        self.active_llm_profile = llm_profile
        return response if isinstance(response, dict) else {}

    def execution_options(self) -> dict[str, Any]:
        response = self._client.execution_options()  # type: ignore[attr-defined]
        return response if isinstance(response, dict) else {}

    def set_thread_id(self, thread_id: str | None) -> None:
        setter = getattr(self._client, "set_thread_id", None)
        if callable(setter):
            setter(thread_id)
        else:
            self._client.thread_id = thread_id  # type: ignore[attr-defined]
        self.active_agent_preset = None
        self.active_agent_preset_config = None

    def set_debug_enabled(self, enabled: bool) -> None:
        setter = getattr(self._client, "set_debug_enabled", None)
        if callable(setter):
            setter(enabled)

    def flush_pending_token_summary(self) -> None:
        flusher = getattr(self._client, "flush_pending_token_summary", None)
        if callable(flusher):
            flusher()

    def compact_thread(self, thread_id: str) -> dict[str, Any]:
        response = self._client.compact_thread(thread_id)  # type: ignore[attr-defined]
        return response if isinstance(response, dict) else {}

    def send_user_message(
        self, content: str, *, parts: list[dict[str, Any]] | None = None
    ) -> object:
        try:
            if parts is None:
                message = self._client.send_user_message(content)  # type: ignore[attr-defined]
            else:
                message = self._client.send_user_message(content, parts=parts)  # type: ignore[attr-defined]
        except MinigentAPIError as exc:
            if self._forget_missing_resumed_thread(exc):
                thread_id = getattr(self._client, "thread_id", None)
                raise MinigentAPIError(
                    f"Remembered thread '{thread_id}' was not found. "
                    "The saved resume target was forgotten; start a new thread explicitly with /new.",
                    category="not_found",
                    status_code=exc.status_code,
                ) from exc
            raise
        thread_id = getattr(self._client, "thread_id", None)
        if isinstance(thread_id, str) and thread_id:
            remember_client_thread(
                self._remembering_config,
                thread_id,
                title=_thread_title_from_message(content),
            )
        return message

    def _forget_missing_resumed_thread(self, exc: MinigentAPIError) -> bool:
        if not self._remembering_config.resume_last or exc.category != "not_found":
            return False
        thread_id = getattr(self._client, "thread_id", None)
        if not isinstance(thread_id, str) or not thread_id:
            return False
        return forget_remembered_client_thread(self._remembering_config, thread_id)


def _first_cli_command(argv: list[str], commands: set[str]) -> str | None:
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--":
            return None
        if item in commands:
            return item
        if item.startswith("--"):
            if "=" not in item and item in _OPTIONS_WITH_VALUES:
                skip_next = True
            continue
        if item.startswith("-"):
            continue
        return None
    return None


_BACKEND_SUBCOMMANDS = {
    "chat": "chat",
    "stdin": "stdin",
    "manual-audio": "manual-audio",
    "passive-audio": "passive-audio",
    "voice": "manual-audio",
}

_OPTIONS_WITH_VALUES = {
    "--backend",
    "--config",
    "--base-url",
    "--wake-phrase",
    "--wake-acknowledgement",
    "--capture-ended-acknowledgement",
    "--skill",
    "--thread-id",
    "--api-token",
    "--user-id",
    "--tenant-id",
    "--chat-submit-mode",
    "--audio-device",
    "--debug-capture-path",
    "--stt-debug-path",
    "--ducking-mode",
    "--ducked-output-volume",
    "--follow-up-timeout-ms",
    "--stt-provider",
    "--stt-device",
    "--stt-compute-type",
    "--stt-language",
    "--tts-provider",
    "--tts-voice",
    "--tts-model",
    "--tts-model-dir",
    "--tts-speaker",
    "--tts-length-scale",
    "--tts-sentence-silence",
    "--wakeword-provider",
    "--keyword-path",
    "--oww-model",
}


def _consume_backend_subcommand(argv: list[str]) -> tuple[str | None, list[str]]:
    skip_next = False
    for index, item in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if item == "--":
            return None, argv
        if item in _BACKEND_SUBCOMMANDS:
            return _BACKEND_SUBCOMMANDS[item], argv[:index] + argv[index + 1 :]
        if item.startswith("--"):
            if "=" not in item and item in _OPTIONS_WITH_VALUES:
                skip_next = True
            continue
        if item.startswith("-"):
            continue
        return None, argv
    return None, argv


def main(argv: list[str] | None = None) -> int:
    load_environment()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    one_shot_command = _first_cli_command(
        raw_argv,
        {
            "run",
            "threads",
            "resume",
            "export",
            "health",
            "ping",
            "options",
            "skills",
            "capabilities",
            "debug-bundle",
            "config",
            "admin",
        },
    )
    if one_shot_command is not None:
        from minigent_client.one_shot_cli import main as one_shot_main

        return one_shot_main(raw_argv)
    backend_override, parser_argv = _consume_backend_subcommand(raw_argv)
    parser = build_parser()
    args = parser.parse_args(parser_argv)
    if backend_override is not None:
        args.backend = backend_override
    config = build_config(args)
    return run_backend(args.backend, config, once=args.once)


def run_backend(backend: str, config: ClientConfig, *, once: bool = False) -> int:
    if backend == "chat":
        return run_chat_loop(config, once=once)
    speech_output = build_speech_output(config)
    ambient_volume_controller = build_ambient_volume_controller(config)
    activation_feedback = wrap_feedback_with_ambient_restore(
        build_acknowledgement_feedback(
            config.wake_acknowledgement,
            config.wake_acknowledgement_sound,
            speech_output,
            async_bell=backend == "passive-audio",
        ),
        ambient_volume_controller,
        reduck_delay_seconds=0.5 if backend == "passive-audio" else 0.0,
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
        backend,
        config,
        activation_feedback=activation_feedback,
        capture_ended_feedback=capture_ended_feedback,
    )
    client_runtime = MinigentClientRuntime(
        wake_phrase=config.wake_phrase,
        activation_source=activation_source,
        minigent_client=cast(
            MinigentAPIClient,
            RememberingMinigentAPIClient(
                MinigentAPIClient(config, output_stream=sys.stdout),
                config,
            ),
        ),
        speech_output=speech_output,
        activation_feedback=None if backend == "passive-audio" else activation_feedback,
        follow_up_timeout_ms=config.follow_up_timeout_ms,
        ambient_volume_controller=ambient_volume_controller,
    )
    try:
        if once:
            client_runtime.run_once()
            return 0
        client_runtime.run_forever()
        return 0
    except KeyboardInterrupt:
        sys.stdout.write("[idle] shutting down\n")
        sys.stdout.flush()
        return 130
    finally:
        close_runtime = getattr(client_runtime, "close", None)
        if callable(close_runtime):
            close_runtime()
        close = getattr(activation_source, "close", None)
        if callable(close):
            close()


def _image_parts_from_macos_clipboard() -> list[dict[str, Any]]:
    if platform.system() != "Darwin":
        raise ValueError("clipboard image paste is currently supported only on macOS")
    if shutil.which("pngpaste") is None:
        raise ValueError("clipboard image paste requires pngpaste on macOS")
    with tempfile.NamedTemporaryFile(suffix=".png") as temp_file:
        result = subprocess.run(
            ["pngpaste", temp_file.name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise ValueError("no PNG image found on the clipboard")
        data = Path(temp_file.name).read_bytes()
    if not data:
        raise ValueError("no PNG image found on the clipboard")
    return [
        {
            "type": "image",
            "mime_type": "image/png",
            "data": base64.b64encode(data).decode("ascii"),
            "detail": "auto",
            "source_path": "clipboard",
        }
    ]


def _image_parts_from_paths(paths: list[str], *, detail: str = "auto") -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise ValueError(f"image file not found: {raw_path}")
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None or not mime_type.startswith("image/"):
            raise ValueError(f"could not determine image MIME type: {raw_path}")
        parts.append(
            {
                "type": "image",
                "mime_type": mime_type,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                "detail": detail,
                "source_path": str(path),
            }
        )
    return parts


def _message_parts_for_pending_images(
    content: str, pending_image_parts: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    if not pending_image_parts:
        return None
    parts: list[dict[str, Any]] = []
    if content:
        parts.append({"type": "text", "text": content})
    for part in pending_image_parts:
        clean_part = {key: value for key, value in part.items() if key != "source_path"}
        parts.append(clean_part)
    return parts


def _handle_chat_image_command(
    utterance: str,
    pending_image_parts: list[dict[str, Any]],
    output_stream: ChatOutputStream,
) -> None:
    try:
        args = shlex.split(utterance)
    except ValueError as exc:
        output_stream.write(f"[idle] image command parse error: {exc}\n")
        output_stream.flush()
        return
    if len(args) == 1 or (len(args) == 2 and args[1] in {"list", "status"}):
        if not pending_image_parts:
            output_stream.write("[idle] no images queued for the next message\n")
        else:
            output_stream.write(
                f"[idle] {len(pending_image_parts)} image(s) queued for the next message:\n"
            )
            for index, part in enumerate(pending_image_parts, start=1):
                output_stream.write(
                    f"  {index}. {part.get('source_path', '<inline>')} ({part.get('mime_type')})\n"
                )
        output_stream.flush()
        return
    if len(args) == 2 and args[1] in {"paste", "clipboard"}:
        try:
            pending_image_parts.extend(_image_parts_from_macos_clipboard())
        except ValueError as exc:
            output_stream.write(f"[idle] {exc}\n")
            output_stream.flush()
            return
        output_stream.write(
            f"[idle] queued clipboard image for the next message "
            f"({len(pending_image_parts)} total)\n"
        )
        output_stream.flush()
        return
    if len(args) == 2 and args[1] in {"clear", "reset"}:
        pending_image_parts.clear()
        output_stream.write("[idle] cleared queued images\n")
        output_stream.flush()
        return
    paths = args[1:]
    try:
        pending_image_parts.extend(_image_parts_from_paths(paths))
    except ValueError as exc:
        output_stream.write(f"[idle] {exc}\n")
        output_stream.flush()
        return
    output_stream.write(
        f"[idle] queued {len(paths)} image(s) for the next message "
        f"({len(pending_image_parts)} total)\n"
    )
    output_stream.flush()


def run_chat_loop(config: ClientConfig, *, once: bool = False) -> int:
    output_stream = sys.stdout
    input_stream = sys.stdin
    client = RememberingMinigentAPIClient(
        MinigentAPIClient(config, output_stream=output_stream),
        config,
    )
    prompt_session: ChatPromptSession | None = None
    prompt_session_thread_id: str | None | object = object()
    synced_prompt_history_thread_id: str | None = None
    speech_output = ConsoleSpeechOutput(output_stream=output_stream)
    pending_image_parts: list[dict[str, Any]] = []

    turns_completed = 0
    while True:
        if prompt_session is None or client.thread_id != prompt_session_thread_id:
            _ensure_chat_thread_for_prompt_history(
                client,
                config,
                input_stream=input_stream,
                output_stream=output_stream,
            )
            if client.thread_id and client.thread_id != synced_prompt_history_thread_id:
                _sync_chat_prompt_history_from_thread(client, config)
                synced_prompt_history_thread_id = client.thread_id
            prompt_session = _build_chat_prompt_session(
                input_stream=input_stream,
                output_stream=output_stream,
                submit_mode=config.chat_submit_mode,
                history_thread_id=client.thread_id,
                history_scope_key=client_state_scope_key(config),
            )
            prompt_session_thread_id = client.thread_id
        try:
            line = _read_chat_line(
                input_stream=input_stream,
                output_stream=output_stream,
                prompt_session=prompt_session,
            )
        except KeyboardInterrupt:
            output_stream.write("\n[idle] shutting down\n")
            output_stream.flush()
            return 130
        except EOFError:
            output_stream.write("[idle] shutting down\n")
            output_stream.flush()
            return 0
        if prompt_session is None and line == "":
            output_stream.write("[idle] shutting down\n")
            output_stream.flush()
            return 0
        utterance = line.strip()
        if not utterance:
            continue
        if utterance in {"/exit", "/quit"}:
            output_stream.write("[idle] shutting down\n")
            output_stream.flush()
            return 0
        if utterance == "/help":
            _write_chat_help(output_stream)
            continue
        if utterance == "/image" or utterance.startswith("/image "):
            _handle_chat_image_command(utterance, pending_image_parts, output_stream)
            continue
        if utterance == "/commands":
            _handle_chat_commands(output_stream)
            continue
        if utterance == "/command" or utterance.startswith("/command "):
            _handle_chat_command_manager(utterance, output_stream)
            continue
        if utterance == "/new":
            _handle_chat_new(client, config, output_stream)
            continue
        if utterance == "/agent" or utterance.startswith("/agent "):
            _handle_chat_agent(utterance, client, config, output_stream)
            continue
        if utterance == "/llm" or utterance.startswith("/llm "):
            _handle_chat_llm(utterance, client, config, output_stream)
            continue
        if utterance in {"/options", "/skills", "/profiles", "/capabilities"}:
            _handle_chat_execution_options(utterance, client, output_stream)
            continue
        if utterance == "/threads" or utterance.startswith("/threads "):
            _handle_chat_threads(utterance, client, config, output_stream)
            continue
        if utterance.startswith("/switch"):
            _handle_chat_switch(utterance, client, config, output_stream)
            continue
        if utterance.startswith("/rename"):
            _handle_chat_rename(utterance, client, config, output_stream)
            continue
        if utterance == "/copy-id":
            _handle_chat_copy_id(client, output_stream)
            continue
        if utterance == "/cancel":
            _handle_chat_cancel(client, output_stream)
            continue
        if utterance.startswith("/export"):
            _handle_chat_export(utterance, client, output_stream)
            continue
        if utterance == "/compact":
            _handle_chat_compact(client, output_stream)
            continue
        if utterance == "/actions":
            _handle_chat_private_actions(client, output_stream)
            continue
        if utterance == "/discard-action" or utterance.startswith("/discard-action "):
            _handle_chat_discard_private_action(utterance, client, output_stream)
            continue
        if utterance == "/tokens":
            _handle_chat_tokens(client, output_stream)
            continue
        if utterance == "/debug":
            debug_enabled = not getattr(client, "_chat_debug_enabled", config.debug_show_prompt)
            client._chat_debug_enabled = debug_enabled  # type: ignore[attr-defined]
            client.set_debug_enabled(debug_enabled)
            output_stream.write(f"[idle] debug {'on' if debug_enabled else 'off'}\n")
            output_stream.flush()
            continue
        if utterance == "/editor":
            edited = _read_editor_chat_prompt(output_stream=output_stream)
            if edited is None:
                continue
            utterance = edited.strip()
            if not utterance:
                continue
        expanded_utterance = _expand_custom_prompt_command(utterance, output_stream)
        if expanded_utterance is None:
            continue
        utterance = expanded_utterance
        try:
            message_parts = _message_parts_for_pending_images(utterance, pending_image_parts)
            if message_parts is None:
                client.send_user_message(utterance)
            else:
                client.send_user_message(utterance, parts=message_parts)
                pending_image_parts.clear()
            reply, metadata = client.run_thread()
            reply, metadata = _maybe_resume_private_value_consent(
                client,
                reply,
                metadata,
                input_stream=input_stream,
                output_stream=output_stream,
            )
        except KeyboardInterrupt:
            _cancel_current_run_after_interrupt(client)
            output_stream.write(f"\n{_chat_abort_message(config)}\n")
            output_stream.flush()
            continue
        except RuntimeError as exc:
            if is_stream_run_error(exc) and isinstance(exc, MinigentAPIError):
                message = format_stream_run_error_summary(exc)
            else:
                message = str(exc)
            output_stream.write(f"[idle] request failed, staying in chat mode: {message}\n")
            output_stream.flush()
            continue
        # Display reasoning content if present and enabled (only for non-streaming mode)
        # In streaming mode, reasoning is displayed via 'reasoning' events
        if config.show_reasoning and not config.stream_runs:
            reasoning = extract_reasoning_content(metadata)
            if reasoning:
                output_stream.write(format_reasoning_block(reasoning, stream=output_stream) + "\n")
                output_stream.flush()
        speech_output.speak(reply)
        client.flush_pending_token_summary()
        turns_completed += 1
        if once and turns_completed >= 1:
            return 0


def _maybe_resume_private_value_consent(
    client: Any,
    reply: str,
    metadata: dict[str, Any] | None,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> tuple[str, dict[str, Any] | None]:
    if not input_stream.isatty():
        return reply, metadata
    pending = client.list_pending_private_value_consents()
    if not pending:
        return reply, metadata
    consent = pending[0]
    consent_id = consent.get("consent_id")
    if not isinstance(consent_id, str):
        return reply, metadata
    tool_name = str(consent.get("tool_name") or "tool")
    disclosures = consent.get("disclosures")
    if isinstance(disclosures, list) and disclosures:
        output_stream.write(f"\n[consent] {tool_name} requests private values:\n")
        for item in disclosures:
            if not isinstance(item, dict):
                continue
            output_stream.write(
                f"  - {item.get('count', 1)} {item.get('kind', 'private value')} "
                f"at {item.get('path', 'unknown path')}\n"
            )
    else:
        output_stream.write(f"\n[consent] {tool_name} requests action approval.\n")
    output_stream.write("Approve this exact tool call once? [y/N] ")
    output_stream.flush()
    approved = input_stream.readline().strip().lower() in {"y", "yes"}
    client.decide_private_value_consent(
        consent_id,
        approve=approved,
        one_shot=True,
    )
    if not approved:
        output_stream.write("[consent] denied\n")
        output_stream.flush()
        return reply, metadata
    output_stream.write("[consent] approved; resuming exact tool call\n")
    output_stream.flush()
    return client.resume_private_value_consent(consent_id)


def _cancel_current_run_after_interrupt(client: Any) -> None:
    try:
        client.cancel_current_run()
    except Exception:
        return


def _chat_abort_message(config: ClientConfig) -> str:
    if config.stream_runs:
        return (
            "[idle] locally aborted current run; server cancellation requested. "
            "Press Ctrl+C again at prompt to exit."
        )
    return (
        "[idle] locally aborted current run; server cancellation unavailable for "
        "non-streaming runs. Press Ctrl+C again at prompt to exit."
    )


def _write_chat_help(output_stream: ChatOutputStream) -> None:
    output_stream.write(
        "[idle] chat commands: /help, /new, /agent [current|preset], /llm [current|profile], "
        "/options, /skills, /profiles, /threads, /switch <id>, /rename <title>, /copy-id, /cancel, "
        "/compact, /actions, /discard-action <consent-id>, /export [markdown|json], /tokens, "
        "/debug, /editor, /image <path...>|paste|list|clear, /commands, "
        "/command set|show|delete, "
        "/exit, /quit. Default: Enter submits; Esc+Enter or Ctrl+J inserts a newline. "
        "Set MINIGENT_CLIENT_CHAT_SUBMIT_MODE=alt-enter to make Esc+Enter submit.\n"
    )
    output_stream.flush()


def _handle_chat_llm(
    utterance: str,
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    selection = utterance.removeprefix("/llm").strip()
    try:
        response = client.execution_options()
    except RuntimeError as exc:
        output_stream.write(f"[idle] LLM profile request failed: {exc}\n")
        output_stream.flush()
        return
    section = response.get("llm_profiles")
    items = section.get("items", []) if isinstance(section, dict) else []
    names = [item.get("name") for item in items if isinstance(item, dict)]
    names = [name for name in names if isinstance(name, str)]
    default = section.get("default") if isinstance(section, dict) else None
    if not selection:
        if not names:
            output_stream.write("[idle] no named LLM profiles configured\n")
        else:
            output_stream.write(f"[idle] available LLM profiles: {', '.join(names)}\n")
            if isinstance(default, str):
                output_stream.write(f"[idle] default LLM profile: {default}\n")
        output_stream.flush()
        return
    if selection == "current":
        current = client.active_llm_profile or default or "legacy/default"
        output_stream.write(f"[idle] current LLM profile: {current}\n")
        output_stream.flush()
        return
    if selection not in names:
        output_stream.write(f"[idle] unknown LLM profile '{selection}'\n")
        output_stream.flush()
        return
    preset = client.active_agent_preset_config
    try:
        created = client.create_thread(
            skill_name=preset.skill_name if preset is not None else None,
            skills=list(preset.skills)
            if preset is not None and preset.skills is not None
            else None,
            capability_profile=preset.capability_profile if preset is not None else None,
            llm_profile=selection,
        )
    except RuntimeError as exc:
        output_stream.write(f"[idle] LLM switch failed: {exc}\n")
        output_stream.flush()
        return
    thread_id = created.get("thread_id") if isinstance(created, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        output_stream.write("[idle] LLM switch failed: missing thread_id\n")
        output_stream.flush()
        return
    if preset is not None:
        client.active_agent_preset = preset.name
        client.active_agent_preset_config = preset
    client.active_llm_profile = selection
    remember_client_thread(config, thread_id, title=f"LLM: {selection}")
    output_stream.write(f"[idle] switched to LLM profile {selection}; created thread {thread_id}\n")
    output_stream.flush()


def _handle_chat_execution_options(
    utterance: str,
    client: RememberingMinigentAPIClient,
    output_stream: ChatOutputStream,
) -> None:
    section = None
    if utterance == "/skills":
        section = "skills"
    elif utterance in {"/profiles", "/capabilities"}:
        section = "capability_profiles"
    try:
        response = client.execution_options()
    except RuntimeError as exc:
        output_stream.write(f"[idle] options request failed: {exc}\n")
        output_stream.flush()
        return
    for line in _format_execution_options(response, section=section).splitlines():
        output_stream.write(f"[idle] {line}\n")
    output_stream.flush()


def _handle_chat_commands(output_stream: ChatOutputStream) -> None:
    commands = PersistentClientState.load().list_prompt_commands()
    if not commands:
        output_stream.write(
            "[idle] no custom slash commands. Add one with /command set <name> <prompt>.\n"
        )
        output_stream.flush()
        return
    output_stream.write("[idle] custom slash commands:\n")
    for command in commands:
        summary = _summarize_prompt_template(command.prompt_template)
        output_stream.write(f"[idle] - /{command.name}: {summary}\n")
    output_stream.flush()


def _handle_chat_command_manager(utterance: str, output_stream: ChatOutputStream) -> None:
    parts = shlex.split(utterance)
    if len(parts) < 2:
        _write_chat_command_usage(output_stream)
        return
    action = parts[1].lower()
    state = PersistentClientState.load()
    if action == "set":
        if len(parts) < 4:
            output_stream.write("[idle] usage: /command set <name> <prompt template>\n")
            output_stream.flush()
            return
        name = parts[2]
        prompt_template = " ".join(parts[3:])
        try:
            command = state.set_prompt_command(name, prompt_template)
        except ValueError as exc:
            output_stream.write(f"[idle] command not saved: {exc}\n")
            output_stream.flush()
            return
        state.save()
        output_stream.write(f"[idle] saved /{command.name}\n")
        output_stream.flush()
        return
    if action == "delete":
        if len(parts) != 3:
            output_stream.write("[idle] usage: /command delete <name>\n")
            output_stream.flush()
            return
        if state.delete_prompt_command(parts[2]):
            state.save()
            output_stream.write(f"[idle] deleted /{normalize_prompt_command_name(parts[2])}\n")
        else:
            output_stream.write(
                f"[idle] no custom command /{normalize_prompt_command_name(parts[2])}\n"
            )
        output_stream.flush()
        return
    if action == "show":
        if len(parts) != 3:
            output_stream.write("[idle] usage: /command show <name>\n")
            output_stream.flush()
            return
        command = state.get_prompt_command(parts[2])
        if command is None:
            output_stream.write(
                f"[idle] no custom command /{normalize_prompt_command_name(parts[2])}\n"
            )
        else:
            output_stream.write(f"[idle] /{command.name}\n{command.prompt_template}\n")
        output_stream.flush()
        return
    if action == "list":
        _handle_chat_commands(output_stream)
        return
    _write_chat_command_usage(output_stream)


def _write_chat_command_usage(output_stream: ChatOutputStream) -> None:
    output_stream.write(
        "[idle] custom commands: /commands, /command set <name> <prompt>, "
        "/command show <name>, /command delete <name>. "
        "Use {input} in a template to place invocation text.\n"
    )
    output_stream.flush()


def _expand_custom_prompt_command(
    utterance: str,
    output_stream: ChatOutputStream,
) -> str | None:
    if not utterance.startswith("/"):
        return utterance
    command_token, _, command_input = utterance.partition(" ")
    name = normalize_prompt_command_name(command_token)
    if not name:
        return utterance
    command = PersistentClientState.load().get_prompt_command(name)
    if command is None:
        output_stream.write(
            f"[idle] unknown command /{name}; use /help or /commands to see available commands\n"
        )
        output_stream.flush()
        return None
    return _render_prompt_command(command, command_input.strip())


def _render_prompt_command(command: PromptCommand, command_input: str) -> str:
    template = command.prompt_template
    if any(placeholder in template for placeholder in ("{input}", "{{input}}", "{selection}")):
        return (
            template.replace("{{input}}", command_input)
            .replace("{input}", command_input)
            .replace("{selection}", command_input)
        )
    if command_input:
        return f"{template}\n\n{command_input}"
    return template


def _summarize_prompt_template(prompt_template: str, *, max_length: int = 72) -> str:
    summary = " ".join(prompt_template.split())
    if len(summary) <= max_length:
        return summary
    return f"{summary[: max_length - 1]}…"


def _handle_chat_new(
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    active_preset = client.active_agent_preset_config
    if active_preset is None:
        response = client.create_thread(skill_name=config.skill_name)
        title = "New thread"
    else:
        response = client.create_thread(
            skill_name=active_preset.skill_name,
            skills=list(active_preset.skills) if active_preset.skills is not None else None,
            capability_profile=active_preset.capability_profile,
        )
        # create_thread resets the client-side active label for ordinary thread creation;
        # /new is expected to keep the selected agent context.
        client.active_agent_preset = active_preset.name
        client.active_agent_preset_config = active_preset
        title = f"Agent: {active_preset.name}"
    thread_id = response.get("thread_id") if isinstance(response, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        output_stream.write("[idle] failed to create thread: missing thread_id\n")
    else:
        remember_client_thread(config, thread_id, title=title)
        output_stream.write(f"[idle] created thread {thread_id}")
        if active_preset is not None:
            output_stream.write(f" with agent {active_preset.name}")
        output_stream.write("\n")
    output_stream.flush()


def _handle_chat_agent(
    utterance: str,
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    selection = utterance.removeprefix("/agent").strip()
    if not selection:
        _write_agent_preset_list(
            _merged_agent_presets(config.agent_presets, _server_agent_presets(client)),
            output_stream,
        )
        return
    if selection == "current":
        _write_current_agent(client, config, output_stream)
        return
    preset = _find_agent_preset(
        _merged_agent_presets(config.agent_presets, _server_agent_presets(client)), selection
    )
    if preset is None:
        output_stream.write(f"[idle] unknown agent preset '{selection}'\n")
        all_presets = _merged_agent_presets(config.agent_presets, _server_agent_presets(client))
        if all_presets:
            output_stream.write("[idle] use /agent to list available presets\n")
        else:
            output_stream.write(
                "[idle] no agent presets configured; add server agents or set MINIGENT_CLIENT_AGENT_PRESETS\n"
            )
        output_stream.flush()
        return
    try:
        response = client.create_thread(
            skill_name=preset.skill_name,
            skills=list(preset.skills) if preset.skills is not None else None,
            capability_profile=preset.capability_profile,
        )
    except RuntimeError as exc:
        output_stream.write(f"[idle] agent switch failed: {exc}\n")
        output_stream.flush()
        return
    thread_id = response.get("thread_id") if isinstance(response, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        output_stream.write("[idle] agent switch failed: missing thread_id\n")
        output_stream.flush()
        return
    client.active_agent_preset = preset.name
    client.active_agent_preset_config = preset
    remember_client_thread(config, thread_id, title=f"Agent: {preset.name}")
    output_stream.write(f"[idle] switched to agent {preset.name}; created thread {thread_id}\n")
    detail = _format_agent_preset_detail(preset)
    if detail:
        output_stream.write(f"[idle] {detail}\n")
    output_stream.flush()


def _server_agent_presets(client: RememberingMinigentAPIClient) -> tuple[AgentPreset, ...]:
    try:
        response = client.execution_options()
    except (AttributeError, RuntimeError):
        return ()
    agents = response.get("agents")
    if not isinstance(agents, dict):
        return ()
    items = agents.get("items")
    if not isinstance(items, list):
        return ()
    presets: list[AgentPreset] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        skill_name = item.get("skill_name") or item.get("skillName")
        raw_skills = item.get("skills") or item.get("skill_names") or item.get("skillNames")
        capability_profile = item.get("capability_profile") or item.get("capabilityProfile")
        description = item.get("description")
        presets.append(
            AgentPreset(
                name=name.strip(),
                skill_name=skill_name if isinstance(skill_name, str) and skill_name else None,
                skills=tuple(raw_skills)
                if isinstance(raw_skills, list)
                and all(isinstance(skill, str) for skill in raw_skills)
                else None,
                capability_profile=capability_profile
                if isinstance(capability_profile, str) and capability_profile
                else None,
                description=description if isinstance(description, str) and description else None,
            )
        )
    return tuple(presets)


def _merged_agent_presets(
    local_presets: tuple[AgentPreset, ...], server_presets: tuple[AgentPreset, ...]
) -> tuple[AgentPreset, ...]:
    merged: list[AgentPreset] = []
    seen: set[str] = set()
    for preset in (*local_presets, *server_presets):
        normalized = preset.name.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        merged.append(preset)
    return tuple(merged)


def _write_agent_preset_list(
    presets: tuple[AgentPreset, ...],
    output_stream: ChatOutputStream,
) -> None:
    if not presets:
        output_stream.write(
            "[idle] no agent presets configured; set MINIGENT_CLIENT_AGENT_PRESETS\n"
        )
        output_stream.flush()
        return
    output_stream.write("[idle] available agents:\n")
    for preset in presets:
        detail = _format_agent_preset_detail(preset)
        suffix = f"  {detail}" if detail else ""
        output_stream.write(f"[idle] - {preset.name}{suffix}\n")
    output_stream.flush()


def _write_current_agent(
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    active = client.active_agent_preset
    if active:
        output_stream.write(f"[idle] current agent: {active}\n")
    elif config.skill_name:
        output_stream.write(f"[idle] current agent: default skill={config.skill_name}\n")
    else:
        output_stream.write("[idle] current agent: default tenant configuration\n")
    if client.thread_id:
        output_stream.write(f"[idle] current thread: {client.thread_id}\n")
    output_stream.flush()


def _find_agent_preset(presets: tuple[AgentPreset, ...], name: str) -> AgentPreset | None:
    normalized = name.casefold()
    for preset in presets:
        if preset.name.casefold() == normalized:
            return preset
    return None


def _format_agent_preset_detail(preset: AgentPreset) -> str:
    parts: list[str] = []
    if preset.skill_name:
        parts.append(f"skill={preset.skill_name}")
    if preset.skills:
        parts.append("skills=" + ",".join(preset.skills))
    if preset.capability_profile:
        parts.append(f"profile={preset.capability_profile}")
    if preset.description:
        parts.append(f"- {preset.description}")
    return " ".join(parts)


def _handle_chat_threads(
    utterance: str,
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    selector = utterance.removeprefix("/threads").strip()
    if selector:
        _switch_to_thread(selector, client, config, output_stream)
        return
    threads = _list_client_threads(config)
    if not threads:
        output_stream.write("[idle] no locally remembered threads\n")
        output_stream.flush()
        return
    if getattr(output_stream, "isatty", lambda: False)():
        selected_thread_id = _pick_thread_from_history(threads, output_stream=output_stream)
        if selected_thread_id:
            _switch_to_thread(selected_thread_id, client, config, output_stream)
        return
    _write_thread_history_list(
        threads, current_thread_id=client.thread_id, output_stream=output_stream
    )


def _write_thread_history_list(
    threads: list[ThreadHistoryItem],
    *,
    current_thread_id: str | None,
    output_stream: ChatOutputStream,
) -> None:
    for item in threads:
        marker = "*" if item.thread_id == current_thread_id else " "
        output_stream.write(f"[idle] {marker} {_format_thread_history_item(item)}\n")
    output_stream.flush()


def _format_thread_history_item(item: ThreadHistoryItem) -> str:
    title = item.title or "Untitled thread"
    updated_at = item.updated_at or "unknown"
    message_count = "?" if item.message_count is None else str(item.message_count)
    return f"{item.thread_id}  {title}  {updated_at}  messages={message_count}"


def _pick_thread_from_history(
    threads: list[ThreadHistoryItem],
    *,
    output_stream: ChatOutputStream,
) -> str | None:
    if not threads:
        return None
    try:
        prompt_toolkit_module = import_module("prompt_toolkit")
        completion_module = import_module("prompt_toolkit.completion")
    except ImportError:
        return _pick_thread_from_numbered_list(threads, output_stream=output_stream)
    PromptSession = prompt_toolkit_module.PromptSession
    WordCompleter = completion_module.WordCompleter
    choices = [item.thread_id for item in threads]
    output_stream.write("[idle] select a thread by number, ID, or search text; blank cancels\n")
    _write_numbered_thread_history(threads, output_stream=output_stream)
    output_stream.flush()
    session = PromptSession(completer=WordCompleter(choices, ignore_case=True))
    try:
        selection = session.prompt("[thread] ").strip()
    except (EOFError, KeyboardInterrupt):
        output_stream.write("\n[idle] thread selection cancelled\n")
        output_stream.flush()
        return None
    return _resolve_thread_selection(selection, threads)


def _pick_thread_from_numbered_list(
    threads: list[ThreadHistoryItem],
    *,
    output_stream: ChatOutputStream,
) -> str | None:
    output_stream.write("[idle] select a thread by number, ID, or search text; blank cancels\n")
    _write_numbered_thread_history(threads, output_stream=output_stream)
    output_stream.write("[thread] ")
    output_stream.flush()
    selection = sys.stdin.readline().strip()
    return _resolve_thread_selection(selection, threads)


def _write_numbered_thread_history(
    threads: list[ThreadHistoryItem],
    *,
    output_stream: ChatOutputStream,
) -> None:
    for index, item in enumerate(threads, start=1):
        output_stream.write(f"[idle] {index}. {_format_thread_history_item(item)}\n")


def _resolve_thread_selection(selection: str, threads: list[ThreadHistoryItem]) -> str | None:
    if selection == "/threads" or selection.startswith("/threads "):
        selection = selection.removeprefix("/threads").strip()
    if not selection:
        return None
    if selection.isdigit():
        index = int(selection) - 1
        if 0 <= index < len(threads):
            return threads[index].thread_id
    for item in threads:
        if item.thread_id == selection:
            return item.thread_id
    normalized = selection.casefold()
    matches = [
        item
        for item in threads
        if normalized in item.thread_id.casefold()
        or normalized in (item.title or "").casefold()
        or normalized in (item.updated_at or "").casefold()
    ]
    if len(matches) == 1:
        return matches[0].thread_id
    return None


def _handle_chat_switch(
    utterance: str,
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    parts = utterance.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        output_stream.write("[idle] usage: /switch <thread-id>\n")
        output_stream.flush()
        return
    _switch_to_thread(parts[1].strip(), client, config, output_stream)


def _switch_to_thread(
    thread_id: str,
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    try:
        thread = client.get_thread(thread_id)
    except RuntimeError as exc:
        output_stream.write(f"[idle] switch failed: {exc}\n")
        output_stream.flush()
        return
    client.set_thread_id(thread_id)
    messages = thread.get("messages") if isinstance(thread, dict) else None
    _append_missing_user_messages_to_chat_history(
        chat_history_file_path(thread_id=thread_id, scope_key=client_state_scope_key(config)),
        messages,
    )
    title = _title_from_thread_messages(messages)
    remember_client_thread(
        config,
        thread_id,
        title=title,
        message_count=len(messages) if isinstance(messages, list) else None,
    )
    output_stream.write(f"[idle] switched to {thread_id}\n")
    output_stream.flush()


def _handle_chat_rename(
    utterance: str,
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    title = utterance.removeprefix("/rename").strip()
    thread_id = client.thread_id
    if not thread_id:
        output_stream.write("[idle] no current thread\n")
    elif not title:
        output_stream.write("[idle] usage: /rename <title>\n")
    elif _rename_client_thread(config, thread_id, title):
        output_stream.write(f'[idle] renamed {thread_id} to "{title}"\n')
    else:
        remember_client_thread(config, thread_id, title=title)
        output_stream.write(f'[idle] renamed {thread_id} to "{title}"\n')
    output_stream.flush()


def _handle_chat_copy_id(
    client: RememberingMinigentAPIClient,
    output_stream: ChatOutputStream,
) -> None:
    thread_id = client.thread_id
    if not thread_id:
        output_stream.write("[idle] no current thread\n")
        output_stream.flush()
        return
    if platform.system() == "Darwin" and shutil.which("pbcopy"):
        try:
            subprocess.run(["pbcopy"], input=thread_id, text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            output_stream.write(f"[idle] {thread_id} (clipboard unavailable: {exc})\n")
        else:
            output_stream.write(f"[idle] copied {thread_id}\n")
    else:
        output_stream.write(f"[idle] {thread_id} (clipboard unavailable)\n")
    output_stream.flush()


def _handle_chat_cancel(
    client: RememberingMinigentAPIClient,
    output_stream: ChatOutputStream,
) -> None:
    thread_id = client.thread_id
    if not thread_id:
        output_stream.write("[idle] no current thread\n")
        output_stream.flush()
        return
    try:
        response = client.cancel_current_run(thread_id)  # type: ignore[attr-defined]
    except Exception as exc:
        output_stream.write(f"[idle] cancel failed: {exc}\n")
        output_stream.flush()
        return
    if isinstance(response, dict) and response.get("cancelled") is True:
        output_stream.write(f"[idle] cancelled active run for {thread_id}\n")
    else:
        output_stream.write(f"[idle] cleared run state for {thread_id}\n")
    output_stream.flush()


def _handle_chat_private_actions(
    client: RememberingMinigentAPIClient,
    output_stream: ChatOutputStream,
) -> None:
    thread_id = client.thread_id
    if not thread_id:
        output_stream.write("[idle] no current thread\n")
        output_stream.flush()
        return
    try:
        actions = client.list_private_value_actions(thread_id)
    except Exception as exc:
        output_stream.write(f"[idle] private action request failed: {exc}\n")
        output_stream.flush()
        return
    if not actions:
        output_stream.write("[idle] no pending or executing private actions\n")
        output_stream.flush()
        return
    output_stream.write("[idle] private actions:\n")
    for action in actions:
        output_stream.write(
            f"  {action.get('consent_id', 'unknown')}  "
            f"{action.get('state', 'unknown')}  "
            f"{action.get('tool_name', 'tool')}  "
            f"expires={action.get('expires_at', 'unknown')}\n"
        )
    output_stream.flush()


def _handle_chat_discard_private_action(
    utterance: str,
    client: RememberingMinigentAPIClient,
    output_stream: ChatOutputStream,
) -> None:
    consent_id = utterance.removeprefix("/discard-action").strip()
    if not consent_id:
        output_stream.write("[idle] usage: /discard-action <consent-id>\n")
        output_stream.flush()
        return
    thread_id = client.thread_id
    if not thread_id:
        output_stream.write("[idle] no current thread\n")
        output_stream.flush()
        return
    try:
        result = client.discard_private_value_action(consent_id, thread_id=thread_id)
    except Exception as exc:
        output_stream.write(f"[idle] private action discard failed: {exc}\n")
        output_stream.flush()
        return
    output_stream.write(
        f"[idle] discarded {result.get('state', 'unknown')} private action "
        f"{result.get('consent_id', consent_id)}\n"
    )
    output_stream.flush()


def _handle_chat_compact(
    client: RememberingMinigentAPIClient,
    output_stream: ChatOutputStream,
) -> None:
    thread_id = client.thread_id
    if not thread_id:
        output_stream.write("[idle] no current thread\n")
        output_stream.flush()
        return
    try:
        response = client.compact_thread(thread_id)
    except Exception as exc:
        output_stream.write(f"[idle] compact failed: {exc}\n")
        output_stream.flush()
        return
    compacted = response.get("compacted_message_count") if isinstance(response, dict) else None
    retained = response.get("message_count") if isinstance(response, dict) else None
    if isinstance(compacted, int) and isinstance(retained, int):
        output_stream.write(
            f"[idle] compacted {compacted} messages; retained {retained} raw messages\n"
        )
    else:
        output_stream.write(f"[idle] compacted thread {thread_id}\n")
    output_stream.flush()


def _handle_chat_tokens(
    client: RememberingMinigentAPIClient,
    output_stream: ChatOutputStream,
) -> None:
    thread_id = client.thread_id
    if not thread_id:
        output_stream.write("[idle] no current thread\n")
        output_stream.flush()
        return
    try:
        thread = client.get_thread(thread_id)
    except RuntimeError as exc:
        output_stream.write(f"[idle] tokens unavailable: {exc}\n")
        output_stream.flush()
        return
    messages = thread.get("messages") if isinstance(thread, dict) else None
    if not isinstance(messages, list):
        output_stream.write("[idle] tokens unavailable: thread messages missing\n")
        output_stream.flush()
        return
    usage = estimate_thread_token_usage(messages)
    summary = format_usage_summary({"usage": usage}) or "tokens unavailable"
    output_stream.write(f"[idle] {summary} · estimated from {len(messages)} messages\n")
    output_stream.flush()


def _handle_chat_export(
    utterance: str,
    client: RememberingMinigentAPIClient,
    output_stream: ChatOutputStream,
) -> None:
    thread_id = client.thread_id
    if not thread_id:
        output_stream.write("[idle] no current thread\n")
        output_stream.flush()
        return
    export_format = utterance.removeprefix("/export").strip() or "markdown"
    if export_format not in {"markdown", "json"}:
        output_stream.write("[idle] usage: /export [markdown|json]\n")
        output_stream.flush()
        return
    try:
        thread = client.get_thread(thread_id)
    except RuntimeError as exc:
        output_stream.write(f"[idle] export failed: {exc}\n")
        output_stream.flush()
        return
    messages = thread.get("messages") if isinstance(thread, dict) else None
    if not isinstance(messages, list):
        output_stream.write("[idle] export failed: thread messages missing\n")
    elif export_format == "json":
        import json

        output_stream.write(
            json.dumps({"thread_id": thread_id, "messages": messages}, indent=2) + "\n"
        )
    else:
        output_stream.write(_format_markdown_transcript(thread_id, messages))
    output_stream.flush()


def _title_from_thread_messages(messages: object) -> str | None:
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return _thread_title_from_message(content)
    return None


def _read_editor_chat_prompt(*, output_stream: ChatOutputStream) -> str | None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        output_stream.write("[idle] set EDITOR or VISUAL to use /editor\n")
        output_stream.flush()
        return None
    with tempfile.TemporaryDirectory(prefix="minigent-prompt-") as prompt_dir:
        prompt_path = Path(prompt_dir) / "prompt.md"
        prompt_path.write_text(
            "\n# Write your Minigent prompt above. Lines starting with # are ignored.\n",
            encoding="utf-8",
        )
        try:
            subprocess.run([*shlex.split(editor), str(prompt_path)], check=False)
        except OSError as exc:
            output_stream.write(f"[idle] failed to open editor: {exc}\n")
            output_stream.flush()
            return None
        try:
            content = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            output_stream.write(f"[idle] failed to read editor content: {exc}\n")
            output_stream.flush()
            return None
    lines = [line for line in content.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def _read_chat_line(
    *,
    input_stream: ChatInputStream,
    output_stream: ChatOutputStream,
    prompt_session: ChatPromptSession | None,
) -> str:
    prompt_label = style_text("[user]", "user", stream=cast(TextIO, output_stream)) + " "
    if prompt_session is not None:
        prompt = prompt_session.prompt
        return prompt(_prompt_toolkit_label(prompt_label))
    output_stream.write(prompt_label)
    output_stream.flush()
    return input_stream.readline()


def _prompt_toolkit_label(prompt_label: str) -> object:
    if "\033[" not in prompt_label:
        return prompt_label
    try:
        formatted_text_module = import_module("prompt_toolkit.formatted_text")
    except ImportError:
        return prompt_label
    ansi = getattr(formatted_text_module, "ANSI", None)
    if ansi is None:
        return prompt_label
    return ansi(prompt_label)


def chat_history_file_path(
    *,
    thread_id: str | None = None,
    scope_key: str | None = None,
) -> Path:
    state_dir = state_dir_path()
    legacy_state_dir = legacy_state_dir_path()
    if not thread_id:
        path = state_dir / CHAT_HISTORY_FILE_NAME
        _copy_legacy_history_if_missing(path, legacy_state_dir / CHAT_HISTORY_FILE_NAME)
        return path
    scope_digest = hashlib.sha256((scope_key or "default").encode("utf-8")).hexdigest()[:16]
    safe_thread_id = re.sub(r"[^A-Za-z0-9_.-]", "_", thread_id).strip("._") or "thread"
    relative_path = Path(CHAT_HISTORY_DIR_NAME) / scope_digest / safe_thread_id
    path = state_dir / relative_path
    _copy_legacy_history_if_missing(path, legacy_state_dir / relative_path)
    return path


def _copy_legacy_history_if_missing(path: Path, legacy_path: Path) -> None:
    if path.exists() or not legacy_path.is_file():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, path)
    except OSError:
        return


def _extract_user_prompt_history_entries(messages: object) -> list[str]:
    if not isinstance(messages, list):
        return []
    entries: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        entry = _prompt_history_entry_from_stored_user_message(message)
        if entry:
            entries.append(entry)
    return entries


def _prompt_history_entry_from_stored_user_message(message: dict[str, object]) -> str | None:
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        raw_user_prompt = metadata.get("raw_user_prompt")
        if isinstance(raw_user_prompt, str) and raw_user_prompt:
            return raw_user_prompt
    return _prompt_history_entry_from_stored_user_content(message.get("content"))


def _prompt_history_entry_from_stored_user_content(content: object) -> str | None:
    if not isinstance(content, str) or not content:
        return None
    context_prefix = "Client context:\n"
    if not content.startswith(context_prefix):
        return content
    _, separator, prompt = content.partition("\n\n")
    if not separator:
        return content
    return prompt or None


def _read_prompt_toolkit_history_entries(history_path: Path) -> list[str]:
    if not history_path.exists():
        return []
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[str] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("#"):
            if current:
                entries.append("\n".join(current))
            current = []
        elif line.startswith("+"):
            if current is None:
                current = []
            current.append(line[1:])
    if current:
        entries.append("\n".join(current))
    return entries


def _append_prompt_toolkit_history_entry(history_path: Path, entry: str) -> None:
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as history_file:
            history_file.write(f"\n# {time.time()}\n")
            for line in entry.split("\n"):
                history_file.write(f"+{line}\n")
    except OSError:
        return


def _append_missing_user_messages_to_chat_history(history_path: Path, messages: object) -> None:
    entries = _extract_user_prompt_history_entries(messages)
    if not entries:
        return
    existing_entries = set(_read_prompt_toolkit_history_entries(history_path))
    for entry in entries:
        if entry in existing_entries:
            continue
        _append_prompt_toolkit_history_entry(history_path, entry)
        existing_entries.add(entry)


def _sync_chat_prompt_history_from_thread(
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
) -> None:
    thread_id = client.thread_id
    if not thread_id:
        return
    try:
        thread = client.get_thread(thread_id)
    except (AttributeError, RuntimeError):
        return
    messages = thread.get("messages") if isinstance(thread, dict) else None
    _append_missing_user_messages_to_chat_history(
        chat_history_file_path(thread_id=thread_id, scope_key=client_state_scope_key(config)),
        messages,
    )
    if isinstance(messages, list):
        remember_client_thread(
            config,
            thread_id,
            title=_title_from_thread_messages(messages),
            message_count=len(messages),
        )


def _ensure_chat_thread_for_prompt_history(
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    *,
    input_stream: ChatInputStream,
    output_stream: ChatOutputStream,
) -> None:
    input_is_tty = getattr(input_stream, "isatty", lambda: False)
    output_is_tty = getattr(output_stream, "isatty", lambda: False)
    if client.thread_id or not input_is_tty() or not output_is_tty():
        return
    try:
        response = client.create_thread(skill_name=config.skill_name)
    except RuntimeError:
        return
    thread_id = response.get("thread_id") if isinstance(response, dict) else None
    if isinstance(thread_id, str) and thread_id:
        remember_client_thread(config, thread_id, title="New thread")


def _build_chat_prompt_session(
    *,
    input_stream: ChatInputStream,
    output_stream: ChatOutputStream,
    submit_mode: str = "enter",
    history_thread_id: str | None = None,
    history_scope_key: str | None = None,
) -> ChatPromptSession | None:
    input_is_tty = getattr(input_stream, "isatty", lambda: False)
    output_is_tty = getattr(output_stream, "isatty", lambda: False)
    if not input_is_tty() or not output_is_tty():
        return None
    try:
        prompt_toolkit_module = import_module("prompt_toolkit")
        history_module = import_module("prompt_toolkit.history")
        key_binding_module = import_module("prompt_toolkit.key_binding")
    except ImportError:
        return None
    PromptSession = prompt_toolkit_module.PromptSession
    FileHistory = history_module.FileHistory
    KeyBindings = key_binding_module.KeyBindings
    try:
        history_path = (
            chat_history_file_path()
            if history_thread_id is None and history_scope_key is None
            else chat_history_file_path(
                thread_id=history_thread_id,
                scope_key=history_scope_key,
            )
        )
        history_path.parent.mkdir(parents=True, exist_ok=True)
        key_bindings = KeyBindings()

        if submit_mode == "alt-enter":

            @key_bindings.add("enter")
            def _(event: Any) -> None:
                event.current_buffer.insert_text("\n")

            @key_bindings.add("escape", "enter")
            def _(event: Any) -> None:
                event.current_buffer.validate_and_handle()

        else:

            @key_bindings.add("enter")
            def _(event: Any) -> None:
                event.current_buffer.validate_and_handle()

            @key_bindings.add("escape", "enter")
            def _(event: Any) -> None:
                event.current_buffer.insert_text("\n")

        @key_bindings.add("c-j")
        def _(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        # Keep prompt_toolkit's accepted input in place. Erasing it and replaying the
        # text causes visible duplicate prompt rows when terminal cursor tracking and
        # tmux pane redraws disagree.
        return PromptSession(
            history=FileHistory(str(history_path)),
            key_bindings=key_bindings,
            multiline=True,
            prompt_continuation="",
        )
    except Exception:
        return None


def build_activation_source(
    backend: str,
    config: ClientConfig,
    *,
    activation_feedback: Callable[[], None] | None = None,
    capture_ended_feedback: Callable[[], None] | None = None,
) -> ActivationSource:
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
                    int(config.audio_sample_rate * 2 * config.wakeword_preroll_ms / 1000.0),
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


def build_microphone_recorder(config: ClientConfig) -> MicrophoneRecorder:
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


def build_capture_debugger(config: ClientConfig) -> CaptureDebugger | None:
    if not config.debug_capture_path:
        return None
    return CaptureDebugger(
        CaptureDebugConfig(capture_path=config.debug_capture_path),
        output_stream=sys.stdout,
    )


def build_ambient_volume_controller(config: ClientConfig):
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


def build_speech_output(config: ClientConfig):
    provider = config.tts_provider.lower()
    if provider == "none":
        return SilentSpeechOutput(output_stream=sys.stdout)
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
        "Choose 'none', 'console', 'say', or 'piper'."
    )


def build_activation_feedback(
    config: ClientConfig,
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

    def run_feedback() -> None:
        temporarily_restore(feedback, reduck_delay_seconds=reduck_delay_seconds)

    return run_feedback


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
                sys.stdout.write(f"[warning] acknowledgement player failed: {command[0]}: {exc}\n")
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
    return _resolve_acknowledgement_sound(ClientConfig.from_env().wake_acknowledgement_sound)


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


def build_speech_provider_config(config: ClientConfig) -> SpeechProviderConfig:
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


def build_wake_word_detector(config: ClientConfig):
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
