from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Callable, Protocol

from app.config import load_environment
from minigent_client.api_client import MinigentAPIClient
from minigent_client.audio import AudioCaptureConfig, MicrophoneRecorder, open_microphone_stream
from minigent_client.backends.manual_audio import ManualAudioActivationSource
from minigent_client.backends.passive_audio import PassiveAudioActivationSource
from minigent_client.backends.stdin_loop import StdinActivationSource
from minigent_client.config import ClientConfig, build_client_config
from minigent_client.debug import CaptureDebugConfig, CaptureDebugger
from minigent_client.ducking import MacOsAmbientVolumeDucker
from minigent_client.one_shot_cli import _format_markdown_transcript
from minigent_client.output import style_text
from minigent_client.ring_buffer import AudioRingBuffer
from minigent_client.runtime import MinigentClientRuntime
from minigent_client.speech import (
    ConsoleSpeechOutput,
    MacOsSaySpeechOutput,
    PiperSpeechOutput,
    SilentSpeechOutput,
)
from minigent_client.state import STATE_DIR_NAME, ThreadHistoryItem, state_scope_key
from minigent_client.state import ClientState as PersistentClientState
from minigent_client.stt import SpeechProviderConfig, build_transcription_adapter
from minigent_client.vad import SileroVoiceActivityDetector
from minigent_client.wakeword import OpenWakeWordDetector, PorcupineWakeWordDetector

CHAT_HISTORY_FILE_NAME = "client-chat-history"


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
    env_config = build_client_config(
        base_url=args.base_url,
        api_token=args.api_token,
        user_id=args.user_id,
        tenant_id=args.tenant_id,
        admin=args.admin if args.admin else None,
        stream_runs=args.stream_runs or None,
        wake_phrase=args.wake_phrase,
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


class RememberingMinigentAPIClient:
    def __init__(self, client: object, config: ClientConfig) -> None:
        self._client = client
        self._remembering_config = config

    def __getattr__(self, name: str) -> object:
        return getattr(self._client, name)

    @property
    def thread_id(self) -> str | None:
        thread_id = getattr(self._client, "thread_id", None)
        return thread_id if isinstance(thread_id, str) else None

    def set_thread_id(self, thread_id: str | None) -> None:
        setter = getattr(self._client, "set_thread_id", None)
        if callable(setter):
            setter(thread_id)
            return
        self._client.thread_id = thread_id  # type: ignore[attr-defined]

    def set_debug_enabled(self, enabled: bool) -> None:
        setter = getattr(self._client, "set_debug_enabled", None)
        if callable(setter):
            setter(enabled)

    def send_user_message(self, content: str) -> object:
        message = self._client.send_user_message(content)  # type: ignore[attr-defined]
        thread_id = getattr(self._client, "thread_id", None)
        if isinstance(thread_id, str) and thread_id:
            remember_client_thread(
                self._remembering_config,
                thread_id,
                title=_thread_title_from_message(content),
            )
        return message


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
        {"run", "threads", "resume", "export", "health", "ping", "debug-bundle", "config", "admin"},
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
        minigent_client=RememberingMinigentAPIClient(
            MinigentAPIClient(config, output_stream=sys.stdout),
            config,
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


def run_chat_loop(config: ClientConfig, *, once: bool = False) -> int:
    output_stream = sys.stdout
    input_stream = sys.stdin
    prompt_session = _build_chat_prompt_session(
        input_stream=input_stream,
        output_stream=output_stream,
        submit_mode=config.chat_submit_mode,
    )
    client = RememberingMinigentAPIClient(
        MinigentAPIClient(config, output_stream=output_stream),
        config,
    )
    speech_output = ConsoleSpeechOutput(output_stream=output_stream)

    turns_completed = 0
    while True:
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
        if utterance == "/new":
            _handle_chat_new(client, config, output_stream)
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
        if utterance.startswith("/export"):
            _handle_chat_export(utterance, client, output_stream)
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
        try:
            client.send_user_message(utterance)
            reply = client.run_thread()
        except RuntimeError as exc:
            output_stream.write(f"[idle] request failed, staying in chat mode: {exc}\n")
            output_stream.flush()
            continue
        speech_output.speak(reply)
        turns_completed += 1
        if once and turns_completed >= 1:
            return 0


def _write_chat_help(output_stream: ChatOutputStream) -> None:
    output_stream.write(
        "[idle] chat commands: /help, /new, /threads, /switch <id>, /rename <title>, "
        "/copy-id, /export [markdown|json], /debug, /editor, /exit, /quit. "
        "Default: Enter submits; Esc+Enter or Ctrl+J inserts a newline. "
        "Set MINIGENT_CLIENT_CHAT_SUBMIT_MODE=alt-enter to make Esc+Enter submit.\n"
    )
    output_stream.flush()


def _handle_chat_new(
    client: RememberingMinigentAPIClient,
    config: ClientConfig,
    output_stream: ChatOutputStream,
) -> None:
    response = client.create_thread(skill_name=config.skill_name)
    thread_id = response.get("thread_id") if isinstance(response, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        output_stream.write("[idle] failed to create thread: missing thread_id\n")
    else:
        remember_client_thread(config, thread_id, title="New thread")
        output_stream.write(f"[idle] created thread {thread_id}\n")
    output_stream.flush()


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
    _write_thread_history_list(threads, current_thread_id=client.thread_id, output_stream=output_stream)


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
        output_stream.write(f"[idle] renamed {thread_id} to \"{title}\"\n")
    else:
        remember_client_thread(config, thread_id, title=title)
        output_stream.write(f"[idle] renamed {thread_id} to \"{title}\"\n")
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

        output_stream.write(json.dumps({"thread_id": thread_id, "messages": messages}, indent=2) + "\n")
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
    prompt_label = style_text("[user]", "user", stream=output_stream) + " "
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


def chat_history_file_path() -> Path:
    return Path.home() / STATE_DIR_NAME / CHAT_HISTORY_FILE_NAME


def _build_chat_prompt_session(
    *,
    input_stream: ChatInputStream,
    output_stream: ChatOutputStream,
    submit_mode: str = "enter",
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
        history_path = chat_history_file_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        key_bindings = KeyBindings()

        if submit_mode == "alt-enter":

            @key_bindings.add("enter")
            def _(event: object) -> None:
                event.current_buffer.insert_text("\n")

            @key_bindings.add("escape", "enter")
            def _(event: object) -> None:
                event.current_buffer.validate_and_handle()

        else:

            @key_bindings.add("enter")
            def _(event: object) -> None:
                event.current_buffer.validate_and_handle()

            @key_bindings.add("escape", "enter")
            def _(event: object) -> None:
                event.current_buffer.insert_text("\n")

        @key_bindings.add("c-j")
        def _(event: object) -> None:
            event.current_buffer.insert_text("\n")

        return PromptSession(
            history=FileHistory(str(history_path)),
            key_bindings=key_bindings,
            multiline=True,
        )
    except Exception:
        return None


def build_activation_source(
    backend: str,
    config: ClientConfig,
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
