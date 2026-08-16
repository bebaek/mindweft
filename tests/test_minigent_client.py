from __future__ import annotations

import argparse
import json
import subprocess
import wave
from io import StringIO
from pathlib import Path

import pytest

import minigent_client.cli as voice_cli
from minigent_client.api_client import MinigentAPIClient
from minigent_client.audio import (
    AudioCaptureConfig,
    MicrophoneRecorder,
    RecordedAudio,
    apply_gain,
    load_recorded_audio_from_wav,
    normalize_peak,
    pad_with_silence,
    pcm16le_to_floats,
    split_pcm_chunk,
)
from minigent_client.backends.manual_audio import ManualAudioActivationSource
from minigent_client.backends.passive_audio import PassiveAudioActivationSource
from minigent_client.backends.stdin_loop import StdinActivationSource
from minigent_client.cli import (
    bounded_output_volume,
    build_ambient_volume_controller,
    build_config,
    build_parser,
    build_speech_output,
    build_speech_provider_config,
    build_wake_word_detector,
    run_chat_loop,
)
from minigent_client.config import (
    CLIENT_CONFIG_ENV_BY_FIELD,
    PRINCIPAL_CONFIG_ENV_BY_FIELD,
    AgentPreset,
    ClientConfig,
    PrincipalConfig,
    default_client_config_paths,
    parse_agent_presets,
)
from minigent_client.debug import CaptureDebugConfig, CaptureDebugger
from minigent_client.ducking import MacOsAmbientVolumeDucker, should_duck_for_state
from minigent_client.errors import MinigentAPIError
from minigent_client.output import (
    StreamProgressRenderer,
    format_thread_context_summary,
    style_assistant_markdown,
    style_line,
    style_stream_progress_line,
)
from minigent_client.ring_buffer import AudioRingBuffer
from minigent_client.runtime import Activation, ClientState, MinigentClientRuntime
from minigent_client.speech import (
    ConsoleSpeechOutput,
    MacOsSaySpeechOutput,
    PiperSpeechOutput,
    SilentSpeechOutput,
    _sanitize_text_for_tts,
)
from minigent_client.state import ClientState as PersistentClientState
from minigent_client.state import PromptCommand, principal_key, state_scope_key
from minigent_client.stt import (
    FasterWhisperTranscriptionAdapter,
    FasterWhisperTranscriptionConfig,
    OpenAITranscriptionAdapter,
    OpenAITranscriptionConfig,
    OpenRouterTranscriptionAdapter,
    OpenRouterTranscriptionConfig,
    SpeechToTextError,
    build_transcription_adapter,
)


class FakeMinigentClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages: list[str] = []

    def send_user_message(self, content: str) -> dict[str, str]:
        self.messages.append(content)
        return {"id": "message-1"}

    def run_thread(self) -> tuple[str, dict[str, object] | None]:
        return (self.reply, None)

    def flush_pending_token_summary(self) -> None:
        pass


class TtyStringIO(StringIO):
    def isatty(self) -> bool:
        return True


class FakeActivationSource:
    def __init__(self, activation: Activation, utterance: str = "") -> None:
        self.activation = activation
        self.utterance = utterance
        self.wait_calls: list[str] = []
        self.capture_calls = 0
        self.follow_up_timeout_ms: list[int] = []
        self.follow_up_utterance: str | None = None

    def wait_for_activation(self, wake_phrase: str) -> Activation:
        self.wait_calls.append(wake_phrase)
        return self.activation

    def capture_utterance(self) -> str:
        self.capture_calls += 1
        return self.utterance

    def capture_follow_up_utterance(self, timeout_ms: int) -> str | None:
        self.follow_up_timeout_ms.append(timeout_ms)
        utterance = self.follow_up_utterance
        self.follow_up_utterance = None
        return utterance

    def wait_for_barge_in(self, wake_phrase: str, should_continue) -> Activation | None:
        _ = wake_phrase, should_continue
        return None


class FakeSpeechOutput:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self._speaking = False

    def speak(self, text: str) -> None:
        self.start(text)
        self.wait()

    def start(self, text: str) -> None:
        self._speaking = True
        self.spoken.append(text)
        self._speaking = False

    def stop(self) -> None:
        self._speaking = False

    def is_speaking(self) -> bool:
        return self._speaking

    def wait(self) -> None:
        self._speaking = False


class FakeAmbientVolumeController:
    def __init__(self) -> None:
        self.states: list[ClientState] = []
        self.close_calls = 0

    def sync_state(self, state: ClientState) -> None:
        self.states.append(state)

    def close(self) -> None:
        self.close_calls += 1


def test_default_client_config_paths_honor_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "xdg-config"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    paths = default_client_config_paths()

    assert paths[0] == config_home / "minigent" / "client.toml"
    assert paths[1] == tmp_path / "home" / ".minigent" / "client.toml"


def test_persistent_client_state_round_trips_last_thread(tmp_path: Path) -> None:
    state_path = tmp_path / "cli-state.json"
    key = state_scope_key(
        "http://127.0.0.1:8000/",
        api_token=None,
        user_id="demo-user",
        tenant_id="demo-tenant",
        is_admin=False,
    )
    state = PersistentClientState.load(state_path)

    assert key == "http://127.0.0.1:8000|dev:demo-user:demo-tenant:false"
    assert state.get_last_thread(key) is None

    state.set_last_thread(key, "thread-1", title="First thread", updated_at="2026-05-19T10:00:00Z")
    state.save()

    loaded = PersistentClientState.load(state_path)
    assert loaded.get_last_thread(key) == "thread-1"
    history = loaded.list_threads(key)
    assert len(history) == 1
    assert history[0].thread_id == "thread-1"
    assert history[0].title == "First thread"
    assert history[0].updated_at == "2026-05-19T10:00:00Z"
    assert loaded.forget_last_thread(key, "thread-2") is False
    assert loaded.forget_last_thread(key, "thread-1") is True
    assert loaded.get_last_thread(key) is None
    assert loaded.list_threads(key) == []


def test_persistent_client_state_migrates_legacy_default_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    legacy_path = tmp_path / ".minigent" / "cli-state.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps({"recent_threads": {"scope": "legacy-thread"}}), encoding="utf-8"
    )

    state = PersistentClientState.load()

    assert state.get_last_thread("scope") == "legacy-thread"
    assert state.path == tmp_path / "xdg-state" / "minigent" / "cli-state.json"
    assert state.path.exists()


def test_persistent_client_state_round_trips_prompt_commands(tmp_path: Path) -> None:
    state_path = tmp_path / "cli-state.json"
    state = PersistentClientState.load(state_path)

    command = state.set_prompt_command("/Rewrite-Friendly", "Rewrite this politely:\n\n{input}")
    state.save()

    assert command.name == "rewrite-friendly"
    loaded = PersistentClientState.load(state_path)
    assert [item.name for item in loaded.list_prompt_commands()] == ["rewrite-friendly"]
    loaded_command = loaded.get_prompt_command("rewrite-friendly")
    assert loaded_command is not None
    assert loaded_command.prompt_template == "Rewrite this politely:\n\n{input}"

    assert loaded.delete_prompt_command("/rewrite-friendly") is True
    assert loaded.get_prompt_command("rewrite-friendly") is None


def test_persistent_client_state_uses_stable_token_fingerprint() -> None:
    first_key = principal_key(
        api_token="secret-token",
        user_id="ignored-user",
        tenant_id="ignored-tenant",
        is_admin=False,
    )
    second_key = principal_key(
        api_token="secret-token",
        user_id="other-user",
        tenant_id="other-tenant",
        is_admin=True,
    )

    assert first_key == second_key
    assert first_key.startswith("bearer:")
    assert "secret-token" not in first_key


def test_minigent_client_uses_transcript_hint_without_capture() -> None:
    activation_source = FakeActivationSource(Activation(transcript_hint="what time is it"))
    minigent_client = FakeMinigentClient(reply="Tool result: 3:00 PM")
    speech_output = FakeSpeechOutput()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
    )

    reply = daemon.run_once()

    assert reply == "Tool result: 3:00 PM"
    assert activation_source.wait_calls == ["hey minigent"]
    assert activation_source.capture_calls == 0
    assert minigent_client.messages == ["what time is it"]
    assert speech_output.spoken == ["Tool result: 3:00 PM"]


def test_minigent_client_captures_utterance_after_activation() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="continue")
    minigent_client = FakeMinigentClient(reply="continued")
    speech_output = FakeSpeechOutput()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
    )

    reply = daemon.run_once()

    assert reply == "continued"
    assert activation_source.capture_calls == 1
    assert minigent_client.messages == ["continue"]
    assert speech_output.spoken == ["continued"]


def test_minigent_client_emits_activation_feedback_before_capture() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="continue")
    minigent_client = FakeMinigentClient(reply="continued")
    speech_output = FakeSpeechOutput()
    feedback_calls: list[str] = []
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        activation_feedback=lambda: feedback_calls.append("ack"),
    )

    reply = daemon.run_once()

    assert reply == "continued"
    assert feedback_calls == ["ack"]
    assert minigent_client.messages == ["continue"]


def test_minigent_client_skips_activation_feedback_for_transcript_hint() -> None:
    activation_source = FakeActivationSource(Activation(transcript_hint="continue"))
    minigent_client = FakeMinigentClient(reply="continued")
    speech_output = FakeSpeechOutput()
    feedback_calls: list[str] = []
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        activation_feedback=lambda: feedback_calls.append("ack"),
    )

    reply = daemon.run_once()

    assert reply == "continued"
    assert feedback_calls == []


def test_minigent_client_supports_barge_in() -> None:
    class FakeBargeInActivationSource:
        def __init__(self) -> None:
            self.capture_calls = 0
            self.barge_in_calls = 0

        def wait_for_activation(self, wake_phrase: str) -> Activation:
            assert wake_phrase == "hey minigent"
            return Activation(transcript_hint="first request")

        def capture_utterance(self) -> str:
            self.capture_calls += 1
            if self.capture_calls == 1:
                return "second request"
            return ""

        def wait_for_barge_in(self, wake_phrase: str, should_continue) -> Activation | None:
            assert wake_phrase == "hey minigent"
            self.barge_in_calls += 1
            if self.barge_in_calls == 1 and should_continue():
                return Activation()
            return None

    class FakeInterruptibleSpeechOutput(FakeSpeechOutput):
        def __init__(self) -> None:
            super().__init__()
            self.started: list[str] = []
            self.stops = 0
            self.waits = 0

        def start(self, text: str) -> None:
            self._speaking = True
            self.started.append(text)

        def stop(self) -> None:
            self.stops += 1
            self._speaking = False

        def wait(self) -> None:
            self.waits += 1
            self._speaking = False

    activation_source = FakeBargeInActivationSource()
    minigent_client = FakeMinigentClient(reply="first reply")
    speech_output = FakeInterruptibleSpeechOutput()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
    )
    replies = iter(["first reply", "second reply"])
    minigent_client.run_thread = lambda: (next(replies), None)

    reply = daemon.run_once()

    assert reply == "second reply"
    assert minigent_client.messages == ["first request", "second request"]
    assert speech_output.started == ["first reply", "second reply"]
    assert speech_output.stops == 1
    assert speech_output.waits == 2


def test_minigent_client_uses_follow_up_window_without_wake_word() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="first request")
    activation_source.follow_up_utterance = "second request"
    minigent_client = FakeMinigentClient(reply="first reply")
    speech_output = FakeSpeechOutput()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        follow_up_timeout_ms=6000,
    )
    replies = iter(["first reply", "second reply"])
    minigent_client.run_thread = lambda: (next(replies), None)

    reply = daemon.run_once()

    assert reply == "second reply"
    assert activation_source.wait_calls == ["hey minigent"]
    assert activation_source.follow_up_timeout_ms == [6000, 6000]
    assert minigent_client.messages == ["first request", "second request"]


def test_minigent_client_returns_to_idle_when_follow_up_window_expires() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="first request")
    activation_source.follow_up_utterance = None
    minigent_client = FakeMinigentClient(reply="first reply")
    speech_output = FakeSpeechOutput()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        follow_up_timeout_ms=4000,
    )

    reply = daemon.run_once()

    assert reply == "first reply"
    assert daemon.state == ClientState.IDLE
    assert activation_source.follow_up_timeout_ms == [4000]


def test_minigent_client_recovers_from_backend_error() -> None:
    class FailingMinigentClient(FakeMinigentClient):
        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            raise RuntimeError(
                'POST http://127.0.0.1:8000/threads/thread-1/run failed: 502 {"detail":"LLM provider returned no message content"}'
            )

    activation_source = FakeActivationSource(Activation(), utterance="okay")
    minigent_client = FailingMinigentClient(reply="")
    speech_output = FakeSpeechOutput()
    output_stream = StringIO()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        output_stream=output_stream,
    )

    reply = daemon.run_once()

    assert reply == ""
    assert daemon.state == ClientState.IDLE
    assert minigent_client.messages == ["okay"]
    assert speech_output.spoken == ["I hit an upstream error."]
    assert "[idle] request failed, returning to wake-word mode:" in output_stream.getvalue()


def test_minigent_client_updates_ambient_volume_for_listening_states() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="first request")
    activation_source.follow_up_utterance = None
    minigent_client = FakeMinigentClient(reply="first reply")
    speech_output = FakeSpeechOutput()
    ambient_volume = FakeAmbientVolumeController()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        follow_up_timeout_ms=4000,
        ambient_volume_controller=ambient_volume,
    )

    reply = daemon.run_once()

    assert reply == "first reply"
    assert ambient_volume.states == [
        ClientState.IDLE,
        ClientState.LISTENING,
        ClientState.THINKING,
        ClientState.SPEAKING,
        ClientState.FOLLOW_UP_LISTENING,
        ClientState.IDLE,
    ]


def test_minigent_client_closes_ambient_volume_controller() -> None:
    ambient_volume = FakeAmbientVolumeController()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=FakeActivationSource(Activation()),
        minigent_client=FakeMinigentClient(reply=""),
        speech_output=FakeSpeechOutput(),
        ambient_volume_controller=ambient_volume,
    )

    daemon.close()

    assert ambient_volume.close_calls == 1


def test_stdin_activation_source_waits_for_wake_phrase() -> None:
    input_stream = StringIO("hello there\nhey minigent summarize this\n")
    output_stream = StringIO()
    source = StdinActivationSource(input_stream=input_stream, output_stream=output_stream)

    activation = source.wait_for_activation("hey minigent")

    assert activation == Activation(transcript_hint="summarize this")
    assert output_stream.getvalue().count("[idle]") == 2


def test_stdin_activation_source_reads_followup_utterance() -> None:
    input_stream = StringIO("write an email\n")
    output_stream = StringIO()
    source = StdinActivationSource(input_stream=input_stream, output_stream=output_stream)

    utterance = source.capture_utterance()

    assert utterance == "write an email"
    assert output_stream.getvalue() == "[listening] "


def test_console_speech_output_prints_reply() -> None:
    output_stream = StringIO()

    ConsoleSpeechOutput(output_stream=output_stream).speak("done")

    assert output_stream.getvalue() == "[assistant] done\n"


def test_console_speech_output_styles_tty_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output_stream = TtyStringIO()

    ConsoleSpeechOutput(output_stream=output_stream).speak("done")

    assert output_stream.getvalue() == "\033[32m[assistant]\033[0m done\n"


def test_cli_style_line_respects_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output_stream = TtyStringIO()

    assert style_line("[warning] careful", stream=output_stream) == "[warning] careful"


def test_cli_styles_assistant_markdown_without_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output_stream = TtyStringIO()
    markdown = "## Summary\nUse `minigent-client chat`.\n```python\nprint('raw')\n```\n> quoted"

    assert style_assistant_markdown(markdown, stream=output_stream) == (
        "\033[1;94m## Summary\033[0m\n"
        "Use \033[36m`minigent-client chat`\033[0m.\n"
        "\033[38;5;248m```python\033[0m\n"
        "\033[36mprint('raw')\033[0m\n"
        "\033[38;5;248m```\033[0m\n"
        "\033[38;5;248m> quoted\033[0m"
    )


def test_cli_styles_assistant_markdown_code_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output_stream = TtyStringIO()
    markdown = (
        "```python\n"
        "# setup\n"
        "print('http://example.test')  # keep URL literal\n"
        "/* block\n"
        "still comment */ const value = 1;\n"
        "```"
    )

    assert style_assistant_markdown(markdown, stream=output_stream) == (
        "\033[38;5;248m```python\033[0m\n"
        "\033[38;5;248m# setup\033[0m\n"
        "\033[36mprint('http://example.test')  \033[0m"
        "\033[38;5;248m# keep URL literal\033[0m\n"
        "\033[38;5;248m/* block\033[0m\n"
        "\033[38;5;248mstill comment */\033[0m"
        "\033[36m const value = 1;\033[0m\n"
        "\033[38;5;248m```\033[0m"
    )


def test_cli_styles_assistant_markdown_bold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output_stream = TtyStringIO()
    markdown = "This is **bold** and __also bold__ text."

    assert style_assistant_markdown(markdown, stream=output_stream) == (
        "This is \033[1m**bold**\033[0m and \033[1m__also bold__\033[0m text."
    )


def test_cli_assistant_markdown_respects_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output_stream = TtyStringIO()
    markdown = "## Summary\nUse `raw`."

    assert style_assistant_markdown(markdown, stream=output_stream) == markdown


def test_cli_stream_progress_styles_muted_full_tty_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output_stream = TtyStringIO()

    assert (
        style_stream_progress_line("● preparing", stream=output_stream)
        == "\033[38;5;248m● preparing\033[0m"
    )
    assert (
        style_stream_progress_line('🔧 calculator(expression="1+1") ...', stream=output_stream)
        == '\033[38;5;248m🔧 calculator(expression="1+1") ...\033[0m'
    )
    assert (
        style_stream_progress_line("[peer] task created", stream=output_stream)
        == "\033[38;5;248m[peer] task created\033[0m"
    )
    assert (
        style_stream_progress_line("   result:", stream=output_stream)
        == "\033[38;5;248m   result:\033[0m"
    )


def test_cli_stream_progress_keeps_errors_high_contrast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    output_stream = TtyStringIO()

    assert (
        style_stream_progress_line("✖ error 502: upstream", stream=output_stream)
        == "\033[31m✖\033[0m error 502: upstream"
    )


def test_cli_stream_progress_respects_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output_stream = TtyStringIO()

    assert style_stream_progress_line("● preparing", stream=output_stream) == "● preparing"


def test_stream_progress_renderer_can_stop_spinner_on_interrupt() -> None:
    renderer = StreamProgressRenderer(stream=TtyStringIO())
    renderer.render({"type": "llm.request", "iteration": 1})

    renderer.stop_active_progress()

    assert renderer._current_spinner is None


def test_streaming_run_stops_progress_when_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ClientConfig(
        base_url="http://api.example.test",
        wake_phrase="hey minigent",
        stream_runs=True,
    )
    client = MinigentAPIClient(config)

    class FakeRenderer:
        def __init__(self) -> None:
            self.rendered: list[dict[str, object]] = []
            self.stop_calls = 0

        def render(self, event: dict[str, object]) -> None:
            self.rendered.append(event)

        def stop_active_progress(self) -> None:
            self.stop_calls += 1

    renderer = FakeRenderer()
    client._stream_progress_renderer = renderer  # type: ignore[assignment]

    def fake_events(method: str, url: str):
        assert method == "POST"
        assert url == "http://api.example.test/threads/thread-1/run/stream"
        yield {"type": "llm.request", "iteration": 1}
        raise KeyboardInterrupt

    monkeypatch.setattr(client, "request_ndjson_events", fake_events)

    with pytest.raises(KeyboardInterrupt):
        client.run_thread("thread-1")

    assert renderer.rendered == [{"type": "llm.request", "iteration": 1}]
    assert renderer.stop_calls == 1


def test_silent_speech_output_prints_reply_without_audio() -> None:
    output_stream = StringIO()

    SilentSpeechOutput(output_stream=output_stream).speak("done")

    assert output_stream.getvalue() == "[assistant] done\n"


def test_macos_say_speech_output_prints_and_speaks(monkeypatch: pytest.MonkeyPatch) -> None:
    output_stream = StringIO()
    captured: dict[str, object] = {}

    class FakeProcess:
        def poll(self) -> int | None:
            return 0

        def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

    def fake_popen(command: list[str], text: bool) -> FakeProcess:
        captured["command"] = command
        captured["text"] = text
        return FakeProcess()

    monkeypatch.setattr("minigent_client.speech.subprocess.Popen", fake_popen)

    MacOsSaySpeechOutput(output_stream=output_stream, voice="Samantha").speak("*done*")

    assert output_stream.getvalue() == "[assistant] *done*\n"
    assert captured == {"command": ["say", "-v", "Samantha", "done"], "text": True}


def test_piper_speech_output_prints_and_plays(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    output_stream = StringIO()
    captured: dict[str, object] = {}

    def fake_run(command, input, text, capture_output, check):
        captured["command"] = command
        captured["input"] = input
        captured["text"] = text
        captured["capture_output"] = capture_output
        captured["check"] = check
        output_path = command[command.index("--output_file") + 1]
        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x01\x00\x02\x00")

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    class FakeSoundDevice:
        def __init__(self) -> None:
            self.play_calls: list[tuple[object, int, bool]] = []
            self.wait_calls = 0
            self.stop_calls = 0

        def play(self, samples, samplerate: int, blocking: bool) -> None:
            self.play_calls.append((samples.copy(), samplerate, blocking))

        def wait(self) -> None:
            self.wait_calls += 1

        def stop(self) -> None:
            self.stop_calls += 1

    sounddevice = FakeSoundDevice()

    monkeypatch.setattr("minigent_client.speech.platform.system", lambda: "Linux")
    monkeypatch.setattr("minigent_client.speech.subprocess.run", fake_run)
    monkeypatch.setattr("minigent_client.speech._resolve_piper_executable", lambda: "piper")
    monkeypatch.setattr("minigent_client.speech._load_sounddevice_for_output", lambda: sounddevice)
    monkeypatch.setattr(
        "minigent_client.speech._resolve_piper_model_path",
        lambda model, model_dir: tmp_path / "voice.onnx",
    )

    PiperSpeechOutput(
        output_stream=output_stream,
        model=str(tmp_path / "voice.onnx"),
        speaker=3,
        length_scale=1.15,
        sentence_silence=0.45,
    ).speak("*done*")

    assert output_stream.getvalue() == "[assistant] *done*\n"
    assert captured["command"][:4] == [
        "piper",
        "--model",
        str(tmp_path / "voice.onnx"),
        "--output_file",
    ]
    assert "--speaker" in captured["command"]
    assert captured["command"][captured["command"].index("--speaker") + 1] == "3"
    assert "--length-scale" in captured["command"]
    assert captured["command"][captured["command"].index("--length-scale") + 1] == "1.15"
    assert "--sentence-silence" in captured["command"]
    assert captured["command"][captured["command"].index("--sentence-silence") + 1] == "0.45"
    assert captured["input"] == "done"
    assert captured["text"] is True
    assert captured["capture_output"] is True
    assert captured["check"] is False
    assert sounddevice.wait_calls == 1
    assert sounddevice.stop_calls == 0
    samples, sample_rate, blocking = sounddevice.play_calls[0]
    assert sample_rate == 22050
    assert blocking is False
    assert list(samples) == [1, 2]


def test_build_speech_output_supports_none_console_say_and_piper() -> None:
    silent = build_speech_output(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            tts_provider="none",
        )
    )
    console = build_speech_output(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            tts_provider="console",
        )
    )
    say = build_speech_output(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            tts_provider="say",
            tts_voice="Samantha",
        )
    )
    piper = build_speech_output(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            tts_provider="piper",
            tts_model="/tmp/en_US-lessac-medium.onnx",
            tts_speaker=5,
            tts_length_scale=1.1,
            tts_sentence_silence=0.5,
        )
    )

    assert isinstance(silent, SilentSpeechOutput)
    assert isinstance(console, ConsoleSpeechOutput)
    assert isinstance(say, MacOsSaySpeechOutput)
    assert say.voice == "Samantha"
    assert isinstance(piper, PiperSpeechOutput)
    assert piper.model == "/tmp/en_US-lessac-medium.onnx"
    assert piper.speaker == 5
    assert piper.length_scale == 1.1
    assert piper.sentence_silence == 0.5


def test_build_ambient_volume_controller_supports_off_and_input_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        build_ambient_volume_controller(
            ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
        )
        is None
    )
    monkeypatch.setattr(
        "minigent_client.cli.MacOsAmbientVolumeDucker.validate_platform", lambda: None
    )

    controller = build_ambient_volume_controller(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            ducking_mode="input-only",
            ducked_output_volume=15,
        )
    )

    assert isinstance(controller, MacOsAmbientVolumeDucker)
    assert controller.ducked_output_volume == 15


def test_build_ambient_volume_controller_warns_and_continues_when_unsupported(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        "minigent_client.cli.MacOsAmbientVolumeDucker.validate_platform",
        lambda: (_ for _ in ()).throw(
            RuntimeError("ambient audio ducking is currently supported only on macOS")
        ),
    )

    controller = build_ambient_volume_controller(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            ducking_mode="input-only",
        )
    )

    assert controller is None
    assert "ambient audio ducking disabled" in capsys.readouterr().out


def test_build_activation_feedback_prefers_system_sound_for_bell(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, stdout, stderr, text, timeout, check):
        captured["command"] = command
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["text"] = text
        captured["timeout"] = timeout
        captured["check"] = check

        class FakeResult:
            returncode = 0
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(
        "minigent_client.cli._resolve_acknowledgement_sound",
        lambda configured_sound_path: Path("/tmp/glass.aiff"),
    )
    monkeypatch.setattr(
        "minigent_client.cli._resolve_acknowledgement_players",
        lambda sound_path: [["/usr/bin/afplay"]] if sound_path == Path("/tmp/glass.aiff") else [],
    )
    monkeypatch.setattr("minigent_client.cli.subprocess.run", fake_run)

    bell = voice_cli.build_activation_feedback(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            wake_acknowledgement="bell",
        ),
        FakeSpeechOutput(),
    )
    assert bell is not None
    bell()
    assert capsys.readouterr().out == ""
    assert captured["command"] == ["/usr/bin/afplay", "/tmp/glass.aiff"]
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.PIPE
    assert captured["text"] is True
    assert captured["timeout"] == 3
    assert captured["check"] is False


def test_build_activation_feedback_falls_back_to_terminal_bell(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        "minigent_client.cli._resolve_acknowledgement_sound",
        lambda configured_sound_path: None,
    )
    monkeypatch.setattr(
        "minigent_client.cli._resolve_acknowledgement_players", lambda sound_path: []
    )

    bell = voice_cli.build_activation_feedback(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            wake_acknowledgement="bell",
        ),
        FakeSpeechOutput(),
    )
    assert bell is not None
    bell()
    assert capsys.readouterr().out == "\a"

    speech_output = FakeSpeechOutput()
    spoken = voice_cli.build_activation_feedback(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            wake_acknowledgement="ready",
        ),
        speech_output,
    )
    assert spoken is not None
    spoken()
    assert speech_output.spoken == ["ready"]


def test_build_activation_feedback_tries_next_player_after_failure(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, stdout, stderr, text, timeout, check):
        del stdout, stderr, text, timeout, check
        calls.append(command)

        class FakeResult:
            def __init__(self, returncode: int, stderr: str) -> None:
                self.returncode = returncode
                self.stderr = stderr

        if command[0] == "/usr/bin/paplay":
            return FakeResult(1, "connection refused")
        return FakeResult(0, "")

    monkeypatch.setattr(
        "minigent_client.cli._resolve_acknowledgement_sound",
        lambda configured_sound_path: Path("/tmp/wake.wav"),
    )
    monkeypatch.setattr(
        "minigent_client.cli._resolve_acknowledgement_players",
        lambda sound_path: [["/usr/bin/paplay"], ["/usr/bin/aplay"]],
    )
    monkeypatch.setattr("minigent_client.cli.subprocess.run", fake_run)

    bell = voice_cli.build_activation_feedback(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            wake_acknowledgement="bell",
        ),
        FakeSpeechOutput(),
    )
    assert bell is not None
    bell()

    assert calls == [["/usr/bin/paplay", "/tmp/wake.wav"], ["/usr/bin/aplay", "/tmp/wake.wav"]]
    assert "acknowledgement player failed" in capsys.readouterr().out


def test_build_acknowledgement_feedback_can_emit_bell_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[tuple[object, tuple[object, ...]]] = []

    class FakeThread:
        def __init__(self, target, args=(), daemon: bool = False) -> None:
            assert daemon is True
            self.target = target
            self.args = args

        def start(self) -> None:
            started.append((self.target, self.args))

    monkeypatch.setattr("minigent_client.cli.threading.Thread", FakeThread)

    bell = voice_cli.build_acknowledgement_feedback(
        "bell",
        "/tmp/wake.aiff",
        FakeSpeechOutput(),
        async_bell=True,
    )
    assert bell is not None
    bell()

    assert started == [(voice_cli._emit_terminal_bell, ("/tmp/wake.aiff",))]


def test_resolve_wake_acknowledgement_sound_prefers_configured_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    sound_path = tmp_path / "wake.wav"
    sound_path.write_bytes(b"sound")
    monkeypatch.setenv("MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT_SOUND", str(sound_path))
    monkeypatch.setattr("minigent_client.cli._default_wake_acknowledgement_sounds", lambda: [])

    assert voice_cli._resolve_wake_acknowledgement_sound() == sound_path


def test_default_wake_acknowledgement_sounds_supports_macos_and_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("minigent_client.cli.platform.system", lambda: "Darwin")
    assert voice_cli._default_wake_acknowledgement_sounds() == [
        Path("/System/Library/Sounds/Glass.aiff")
    ]

    monkeypatch.setattr("minigent_client.cli.platform.system", lambda: "Linux")
    assert voice_cli._default_wake_acknowledgement_sounds() == [
        Path("/usr/share/sounds/alsa/Front_Center.wav"),
        Path("/usr/share/sounds/sound-icons/glass-water-1.wav"),
        Path("/usr/share/sounds/freedesktop/stereo/complete.oga"),
        Path("/usr/share/sounds/freedesktop/stereo/bell.oga"),
    ]


def test_resolve_wake_acknowledgement_player_supports_macos_and_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("minigent_client.cli.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "minigent_client.cli.shutil.which",
        lambda name: "/usr/bin/afplay" if name == "afplay" else None,
    )
    assert voice_cli._resolve_wake_acknowledgement_player(Path("/tmp/test.aiff")) == [
        "/usr/bin/afplay"
    ]

    monkeypatch.setattr("minigent_client.cli.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "minigent_client.cli.shutil.which",
        lambda name: "/usr/bin/paplay" if name == "paplay" else None,
    )
    assert voice_cli._resolve_wake_acknowledgement_player(Path("/tmp/test.oga")) == [
        "/usr/bin/paplay"
    ]

    monkeypatch.setattr(
        "minigent_client.cli.shutil.which",
        lambda name: "/usr/bin/aplay" if name == "aplay" else None,
    )
    assert voice_cli._resolve_wake_acknowledgement_player(Path("/tmp/test.oga")) is None
    assert voice_cli._resolve_wake_acknowledgement_player(Path("/tmp/test.wav")) == [
        "/usr/bin/aplay"
    ]

    monkeypatch.setattr(
        "minigent_client.cli.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"aplay", "paplay"} else None,
    )
    assert voice_cli._resolve_acknowledgement_players(Path("/tmp/test.wav")) == [
        ["/usr/bin/aplay"],
        ["/usr/bin/paplay"],
    ]


def test_sanitize_text_for_tts_strips_common_markdown() -> None:
    text = "# Heading\n- **bold** item with [link](https://example.com) and `code`"

    assert _sanitize_text_for_tts(text) == "Heading. Bold item with link and code."


def test_sanitize_text_for_tts_preserves_boundary_around_headers() -> None:
    text = "Before\n## Details\nAfter"

    assert _sanitize_text_for_tts(text) == "Before Details. After"


def test_sanitize_text_for_tts_adds_boundaries_for_common_markdown_lists() -> None:
    text = "* first item\n* second item\n1) third item\n2) fourth item"

    assert _sanitize_text_for_tts(text) == "First item. Second item. Third item. Fourth item."


def test_sanitize_text_for_tts_strips_task_list_markers() -> None:
    text = "- [x] shipped\n- [ ] pending"

    assert _sanitize_text_for_tts(text) == "Shipped. Pending."


def test_sanitize_text_for_tts_promotes_short_list_items_to_sentences() -> None:
    text = (
        "- flights\n"
        "- hotel / Airbnb\n"
        "- itinerary\n"
        "- airport\n"
        "- trip / travel\n"
        "- city names or countries"
    )

    assert _sanitize_text_for_tts(text) == (
        "Flights. Hotel / Airbnb. Itinerary. Airport. Trip / travel. City names or countries."
    )


def test_macos_say_speech_output_can_be_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    output_stream = StringIO()
    terminated = {"value": False}

    class FakeProcess:
        def __init__(self) -> None:
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else -15

        def wait(self) -> int:
            self.running = False
            return -15

        def terminate(self) -> None:
            terminated["value"] = True
            self.running = False

    monkeypatch.setattr(
        "minigent_client.speech.subprocess.Popen", lambda command, text: FakeProcess()
    )
    speech = MacOsSaySpeechOutput(output_stream=output_stream, voice="Samantha")

    speech.start("done")
    assert speech.is_speaking()
    speech.stop()
    speech.wait()

    assert terminated["value"] is True
    assert not speech.is_speaking()


def test_piper_speech_output_can_be_interrupted(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    output_stream = StringIO()

    def fake_run(command, input, text, capture_output, check):
        output_path = command[command.index("--output_file") + 1]
        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00\x01\x00")

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    class FakeSoundDevice:
        def __init__(self) -> None:
            import threading

            self._stopped = threading.Event()
            self.stop_calls = 0

        def play(self, samples, samplerate: int, blocking: bool) -> None:
            return None

        def wait(self) -> None:
            self._stopped.wait(timeout=1)

        def stop(self) -> None:
            self.stop_calls += 1
            self._stopped.set()

    sounddevice = FakeSoundDevice()

    monkeypatch.setattr("minigent_client.speech.platform.system", lambda: "Linux")
    monkeypatch.setattr("minigent_client.speech.subprocess.run", fake_run)
    monkeypatch.setattr("minigent_client.speech._resolve_piper_executable", lambda: "piper")
    monkeypatch.setattr("minigent_client.speech._load_sounddevice_for_output", lambda: sounddevice)
    monkeypatch.setattr(
        "minigent_client.speech._resolve_piper_model_path",
        lambda model, model_dir: tmp_path / "voice.onnx",
    )

    speech = PiperSpeechOutput(output_stream=output_stream, model=str(tmp_path / "voice.onnx"))
    speech.start("done")
    assert speech.is_speaking()
    speech.stop()
    speech.wait()

    assert sounddevice.stop_calls == 1
    assert not speech.is_speaking()


def test_piper_speech_output_uses_afplay_on_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    output_stream = StringIO()
    commands: list[list[str]] = []

    def fake_run(command, input, text, capture_output, check):
        output_path = Path(command[command.index("--output_file") + 1])
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00\x01\x00")

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    class FakeProcess:
        def __init__(self, command: list[str]) -> None:
            self.command = command
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def wait(self) -> int:
            self.running = False
            return 0

        def terminate(self) -> None:
            self.running = False

    def fake_popen(command: list[str]):
        commands.append(command)
        return FakeProcess(command)

    monkeypatch.setattr("minigent_client.speech.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "minigent_client.speech.shutil.which",
        lambda name: "/usr/bin/afplay" if name == "afplay" else None,
    )
    monkeypatch.setattr("minigent_client.speech.subprocess.run", fake_run)
    monkeypatch.setattr("minigent_client.speech.subprocess.Popen", fake_popen)
    monkeypatch.setattr("minigent_client.speech._resolve_piper_executable", lambda: "piper")
    monkeypatch.setattr(
        "minigent_client.speech._resolve_piper_model_path",
        lambda model, model_dir: tmp_path / "voice.onnx",
    )

    PiperSpeechOutput(output_stream=output_stream, model=str(tmp_path / "voice.onnx")).speak("done")

    assert commands
    assert commands[0][0] == "/usr/bin/afplay"
    assert output_stream.getvalue() == "[assistant] done\n"


def test_piper_speech_output_can_be_interrupted_on_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    output_stream = StringIO()
    terminated = {"value": False}

    def fake_run(command, input, text, capture_output, check):
        output_path = Path(command[command.index("--output_file") + 1])
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00\x01\x00")

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        return Result()

    class FakeProcess:
        def __init__(self, command: list[str]) -> None:
            self.command = command
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def wait(self) -> int:
            self.running = False
            return 0

        def terminate(self) -> None:
            terminated["value"] = True
            self.running = False

    monkeypatch.setattr("minigent_client.speech.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "minigent_client.speech.shutil.which",
        lambda name: "/usr/bin/afplay" if name == "afplay" else None,
    )
    monkeypatch.setattr("minigent_client.speech.subprocess.run", fake_run)
    monkeypatch.setattr("minigent_client.speech.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("minigent_client.speech._resolve_piper_executable", lambda: "piper")
    monkeypatch.setattr(
        "minigent_client.speech._resolve_piper_model_path",
        lambda model, model_dir: tmp_path / "voice.onnx",
    )

    speech = PiperSpeechOutput(output_stream=output_stream, model=str(tmp_path / "voice.onnx"))
    speech.start("done")
    assert speech.is_speaking()
    speech.stop()
    speech.wait()

    assert terminated["value"] is True
    assert not speech.is_speaking()


def test_piper_speech_output_reports_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    output_stream = StringIO()

    monkeypatch.setattr("minigent_client.speech.shutil.which", lambda name: None)
    monkeypatch.setattr("minigent_client.speech.os.environ", {"PATH": "/tmp/does-not-exist"})
    monkeypatch.setattr("minigent_client.speech.sys.executable", "/tmp/does-not-exist/python")

    speech = PiperSpeechOutput(output_stream=output_stream, model="/tmp/voice.onnx")

    with pytest.raises(RuntimeError, match="not available on PATH"):
        speech.start("done")


def test_resolve_piper_executable_finds_sibling_of_active_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from minigent_client.speech import _resolve_piper_executable

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python_path = bin_dir / "python"
    python_path.write_text("", encoding="utf-8")
    piper_path = bin_dir / "piper"
    piper_path.write_text("#!/bin/sh\n", encoding="utf-8")
    piper_path.chmod(0o755)

    monkeypatch.setattr("minigent_client.speech.shutil.which", lambda name: None)
    monkeypatch.setattr("minigent_client.speech.os.environ", {"PATH": "/tmp/does-not-exist"})
    monkeypatch.setattr("minigent_client.speech.sys.executable", str(python_path))

    assert _resolve_piper_executable() == str(piper_path)


def test_resolve_piper_model_path_uses_existing_onnx_path(tmp_path) -> None:
    from minigent_client.speech import _resolve_piper_model_path

    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"model")

    assert _resolve_piper_model_path(str(model_path), None) == model_path


def test_resolve_piper_model_path_downloads_named_voice(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from minigent_client import speech as speech_module

    downloaded: dict[str, object] = {}

    def fake_download_voice(voice: str, download_dir) -> None:
        downloaded["voice"] = voice
        downloaded["download_dir"] = download_dir
        (download_dir / f"{voice}.onnx").write_bytes(b"model")
        (download_dir / f"{voice}.onnx.json").write_text("{}", encoding="utf-8")

    class FakeDownloadVoices:
        @staticmethod
        def download_voice(voice: str, download_dir) -> None:
            fake_download_voice(voice, download_dir)

    monkeypatch.setattr(
        "pathlib.Path.home",
        lambda: tmp_path,
    )
    monkeypatch.setattr("piper.download_voices.download_voice", FakeDownloadVoices.download_voice)

    model_path = speech_module._resolve_piper_model_path("en_US-lessac-medium", None)

    assert model_path == tmp_path / ".cache" / "minigent" / "piper" / "en_US-lessac-medium.onnx"
    assert downloaded["voice"] == "en_US-lessac-medium"
    assert downloaded["download_dir"] == tmp_path / ".cache" / "minigent" / "piper"


def test_principal_config_prefers_bearer_token() -> None:
    principal = PrincipalConfig(
        user_id="user-1",
        tenant_id="tenant-1",
        is_admin=True,
        api_token="secret-token",
    )

    assert principal.build_headers() == {"Authorization": "Bearer secret-token"}


def test_parse_agent_presets_supports_object_and_array_forms() -> None:
    assert parse_agent_presets(
        {
            "coding-inspect": {
                "skill_names": ["coding-workspace"],
                "capability_profile": "inspect",
            },
            "support": {"skill_name": "support"},
        }
    ) == (
        AgentPreset(
            name="coding-inspect",
            skills=("coding-workspace",),
            capability_profile="inspect",
        ),
        AgentPreset(name="support", skill_name="support"),
    )
    assert parse_agent_presets(
        [
            {
                "name": "home-assistant",
                "skillNames": ["home-assistant", "concise"],
                "capabilityProfile": "home-assistant",
            }
        ]
    ) == (
        AgentPreset(
            name="home-assistant",
            skills=("home-assistant", "concise"),
            capability_profile="home-assistant",
        ),
    )


def test_parse_agent_presets_rejects_ambiguous_or_empty_presets() -> None:
    with pytest.raises(ValueError, match="cannot set both skill_name and skill_names"):
        parse_agent_presets({"coding": {"skill_name": "coding", "skill_names": ["review"]}})
    with pytest.raises(ValueError, match="must set skill_name, skill_names, or capability_profile"):
        parse_agent_presets({"empty": {"description": "No effective thread settings"}})
    with pytest.raises(ValueError, match="Duplicate agent preset"):
        parse_agent_presets(
            [
                {"name": "Support", "skill_name": "support"},
                {"name": "support", "skill_name": "support"},
            ]
        )


def test_minigent_client_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_BASE_URL", "http://127.0.0.1:9000/")
    monkeypatch.setenv("MINIGENT_VOICE_WAKE_PHRASE", "computer")
    monkeypatch.setenv("MINIGENT_VOICE_PROMPT_PREAMBLE", "timezone=America/Chicago")
    monkeypatch.setenv("MINIGENT_VOICE_LOCATION", "Austin, TX, US; timezone=America/Chicago")
    monkeypatch.setenv("MINIGENT_VOICE_DEBUG_SHOW_PROMPT", "true")
    monkeypatch.setenv("MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT", "bell")
    monkeypatch.setenv("MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT_SOUND", "/tmp/wake.wav")
    monkeypatch.setenv("MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT", "done")
    monkeypatch.setenv("MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT_SOUND", "/tmp/done.wav")
    monkeypatch.setenv("MINIGENT_VOICE_STT_PROVIDER", "openrouter")
    monkeypatch.setenv("MINIGENT_VOICE_STT_DEVICE", "cpu")
    monkeypatch.setenv("MINIGENT_VOICE_STT_COMPUTE_TYPE", "int8")
    monkeypatch.setenv("MINIGENT_VOICE_STT_LANGUAGE", "en")
    monkeypatch.setenv("MINIGENT_VOICE_TTS_PROVIDER", "say")
    monkeypatch.setenv("MINIGENT_VOICE_TTS_VOICE", "Samantha")
    monkeypatch.setenv("MINIGENT_VOICE_TTS_MODEL", "/tmp/en_US-lessac-medium.onnx")
    monkeypatch.setenv("MINIGENT_VOICE_TTS_MODEL_DIR", "/tmp/minigent-piper")
    monkeypatch.setenv("MINIGENT_VOICE_TTS_SPEAKER", "7")
    monkeypatch.setenv("MINIGENT_VOICE_TTS_LENGTH_SCALE", "1.2")
    monkeypatch.setenv("MINIGENT_VOICE_TTS_SENTENCE_SILENCE", "0.6")
    monkeypatch.setenv("MINIGENT_VOICE_WAKEWORD_PROVIDER", "porcupine")
    monkeypatch.setenv("MINIGENT_VOICE_SKILL", "support")
    monkeypatch.setenv(
        "MINIGENT_CLIENT_AGENT_PRESETS",
        json.dumps(
            {
                "coding-inspect": {
                    "skill_names": ["coding-workspace"],
                    "capability_profile": "inspect",
                    "description": "Read-only coding agent",
                }
            }
        ),
    )
    monkeypatch.setenv("MINIGENT_VOICE_THREAD_ID", "thread-123")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_DEVICE", "Built-in Mic")
    monkeypatch.setenv("MINIGENT_VOICE_DEBUG_CAPTURE_PATH", "/tmp/minigent-last-capture.wav")
    monkeypatch.setenv("MINIGENT_VOICE_STT_DEBUG_PATH", "/tmp/minigent-stt-debug")
    monkeypatch.setenv("MINIGENT_VOICE_DUCKING_MODE", "input-only")
    monkeypatch.setenv("MINIGENT_VOICE_DUCKED_OUTPUT_VOLUME", "18")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_SAMPLE_RATE", "22050")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_BLOCK_SIZE", "1024")
    monkeypatch.setenv("MINIGENT_VOICE_END_SILENCE_MS", "600")
    monkeypatch.setenv("MINIGENT_VOICE_MAX_RECORD_SECONDS", "9.5")
    monkeypatch.setenv("MINIGENT_VOICE_FOLLOW_UP_TIMEOUT_MS", "0")
    monkeypatch.setenv("MINIGENT_VOICE_POST_WAKE_SETTLE_MS", "300")
    monkeypatch.setenv("MINIGENT_VOICE_WAKEWORD_PREROLL_MS", "1200")
    monkeypatch.setenv("MINIGENT_VOICE_STT_PAD_LEADING_MS", "200")
    monkeypatch.setenv("MINIGENT_VOICE_STT_PAD_TRAILING_MS", "650")
    monkeypatch.setenv("MINIGENT_VOICE_VAD_THRESHOLD", "0.65")
    monkeypatch.setenv("MINIGENT_VOICE_STT_MODEL", "openai/gpt-audio")
    monkeypatch.setenv("PICOVOICE_ACCESS_KEY", "picovoice-key")
    monkeypatch.setenv("MINIGENT_VOICE_KEYWORD_PATH", "/tmp/hey-minigent.ppn")
    monkeypatch.setenv("MINIGENT_VOICE_OWW_MODEL", "okay_nabu")
    monkeypatch.setenv("MINIGENT_VOICE_OWW_THRESHOLD", "0.6")
    monkeypatch.setenv("MINIGENT_VOICE_USER_ID", "voice-user")
    monkeypatch.setenv("MINIGENT_VOICE_TENANT_ID", "voice-tenant")
    monkeypatch.setenv("MINIGENT_VOICE_ADMIN", "true")
    monkeypatch.setenv("MINIGENT_VOICE_API_TOKEN", "voice-token")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com/v1/")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.example/api/v1/")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://minigent.example")
    monkeypatch.setenv("OPENROUTER_APP_NAME", "minigent")

    config = ClientConfig.from_env()

    assert config == ClientConfig(
        base_url="http://127.0.0.1:9000",
        wake_phrase="computer",
        prompt_preamble="timezone=America/Chicago",
        location="Austin, TX, US; timezone=America/Chicago",
        debug_show_prompt=True,
        wake_acknowledgement="bell",
        wake_acknowledgement_sound="/tmp/wake.wav",
        capture_ended_acknowledgement="done",
        capture_ended_acknowledgement_sound="/tmp/done.wav",
        stt_provider="openrouter",
        stt_device="cpu",
        stt_compute_type="int8",
        stt_language="en",
        tts_provider="say",
        tts_voice="Samantha",
        tts_model="/tmp/en_US-lessac-medium.onnx",
        tts_model_dir="/tmp/minigent-piper",
        tts_speaker=7,
        tts_length_scale=1.2,
        tts_sentence_silence=0.6,
        wakeword_provider="porcupine",
        skill_name="support",
        agent_presets=(
            AgentPreset(
                name="coding-inspect",
                skills=("coding-workspace",),
                capability_profile="inspect",
                description="Read-only coding agent",
            ),
        ),
        thread_id="thread-123",
        audio_device="Built-in Mic",
        debug_capture_path="/tmp/minigent-last-capture.wav",
        stt_debug_path="/tmp/minigent-stt-debug",
        ducking_mode="input-only",
        ducked_output_volume=18,
        audio_sample_rate=22050,
        audio_block_size=1024,
        speech_silence_ms=600,
        speech_max_seconds=9.5,
        wakeword_cooldown_ms=1500,
        post_wake_speech_timeout_ms=2500,
        follow_up_timeout_ms=0,
        post_wake_settle_ms=300,
        wakeword_preroll_ms=1200,
        stt_pad_leading_ms=200,
        stt_pad_trailing_ms=650,
        vad_threshold=0.65,
        stt_model="openai/gpt-audio",
        openai_api_key="openai-key",
        openai_base_url="https://api.example.com/v1",
        openrouter_api_key="openrouter-key",
        openrouter_base_url="https://openrouter.example/api/v1",
        openrouter_http_referer="https://minigent.example",
        openrouter_app_name="minigent",
        picovoice_access_key="picovoice-key",
        porcupine_keyword_path="/tmp/hey-minigent.ppn",
        openwakeword_model="okay_nabu",
        openwakeword_threshold=0.6,
        principal=PrincipalConfig(
            user_id="voice-user",
            tenant_id="voice-tenant",
            is_admin=True,
            api_token="voice-token",
        ),
    )


def test_minigent_client_config_file_supplies_defaults_and_agent_presets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for env_names in CLIENT_CONFIG_ENV_BY_FIELD.values():
        for env_name in env_names:
            monkeypatch.delenv(env_name, raising=False)
    for env_name in PRINCIPAL_CONFIG_ENV_BY_FIELD.values():
        monkeypatch.delenv(env_name, raising=False)
    config_path = tmp_path / "client.toml"
    config_path.write_text(
        """
base_url = "http://api.example.test/"
stream_runs = true
show_reasoning = true
chat_submit_mode = "alt-enter"

[principal]
user_id = "file-user"
tenant_id = "file-tenant"
is_admin = true
api_token = "file-token"

[voice]
wake_phrase = "computer"
stt_provider = "faster-whisper"
stt_device = "cpu"
tts_provider = "say"
tts_voice = "Samantha"
follow_up_timeout_ms = 3000

[voice.wakeword]
provider = "openwakeword"
model = "okay_nabu"
threshold = 0.7

[agents.coding-inspect]
skill_names = ["coding-workspace"]
capability_profile = "inspect"
description = "Read-only coding agent"
""".strip(),
        encoding="utf-8",
    )

    config = ClientConfig.from_env(config_path=config_path)

    assert config.config_path == str(config_path)
    assert config.base_url == "http://api.example.test"
    assert config.stream_runs is True
    assert config.show_reasoning is True
    assert config.chat_submit_mode == "alt-enter"
    assert config.wake_phrase == "computer"
    assert config.stt_provider == "faster-whisper"
    assert config.stt_device == "cpu"
    assert config.tts_provider == "say"
    assert config.tts_voice == "Samantha"
    assert config.follow_up_timeout_ms == 3000
    assert config.wakeword_provider == "openwakeword"
    assert config.openwakeword_model == "okay_nabu"
    assert config.openwakeword_threshold == 0.7
    assert config.principal == PrincipalConfig(
        user_id="file-user",
        tenant_id="file-tenant",
        is_admin=True,
        api_token="file-token",
    )
    assert config.agent_presets == (
        AgentPreset(
            name="coding-inspect",
            skills=("coding-workspace",),
            capability_profile="inspect",
            description="Read-only coding agent",
        ),
    )

    monkeypatch.setenv("MINIGENT_BASE_URL", "http://env.example.test")
    monkeypatch.setenv("MINIGENT_VOICE_USER_ID", "env-user")
    overridden = ClientConfig.from_env(config_path=config_path)

    assert overridden.base_url == "http://env.example.test"
    assert overridden.principal.user_id == "env-user"
    assert overridden.principal.tenant_id == "file-tenant"


def test_build_config_accepts_explicit_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MINIGENT_BASE_URL", raising=False)
    config_path = tmp_path / "client.toml"
    config_path.write_text(
        'base_url = "http://api.example.test"\n[voice]\nwake_phrase = "computer"\n',
        encoding="utf-8",
    )

    args = build_parser().parse_args(["--config", str(config_path), "--wake-phrase", "jarvis"])
    config = build_config(args)

    assert config.config_path == str(config_path)
    assert config.base_url == "http://api.example.test"
    assert config.wake_phrase == "jarvis"


def test_minigent_client_sends_raw_message_when_location_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def fake_urlopen(request: object) -> FakeResponse:
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "payload": payload,
                "headers": dict(request.header_items()),
            }
        )
        if request.full_url.endswith("/threads/thread-123/messages"):
            return FakeResponse({"id": "message-1"})
        raise AssertionError(f"unexpected url {request.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        )
    )

    response = client.send_user_message("what time is it")

    assert response == {"id": "message-1"}
    assert requests == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/threads/thread-123/messages",
            "payload": {
                "content": "what time is it",
                "metadata": {"raw_user_prompt": "what time is it"},
            },
            "headers": {
                "X-minigent-user-id": "user-1",
                "X-minigent-tenant-id": "tenant-1",
                "X-minigent-admin": "false",
                "Content-type": "application/json",
            },
        }
    ]


def test_minigent_client_can_cancel_current_run(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request: object) -> FakeResponse:
        requests.append({"method": request.get_method(), "url": request.full_url})
        return FakeResponse({"cancelled": True, "thread_id": "thread-123"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
        )
    )

    client.cancel_current_run()

    assert requests == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/threads/thread-123/run/cancel",
        }
    ]


def test_minigent_client_uses_location_as_compatibility_preamble_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_payloads: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def fake_urlopen(request: object) -> FakeResponse:
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        seen_payloads.append(payload)
        return FakeResponse({"id": "message-1"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            location="Austin, TX, US; timezone=America/Chicago",
            thread_id="thread-123",
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        )
    )

    client.send_user_message("find coffee nearby")

    assert seen_payloads == [
        {
            "content": (
                "Client context:\n"
                "location=Austin, TX, US; timezone=America/Chicago\n\n"
                "find coffee nearby"
            ),
            "metadata": {"raw_user_prompt": "find coffee nearby"},
        }
    ]


def test_minigent_client_prefers_explicit_prompt_preamble_over_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_payloads: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def fake_urlopen(request: object) -> FakeResponse:
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        seen_payloads.append(payload)
        return FakeResponse({"id": "message-1"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            prompt_preamble="timezone=America/Chicago\nnote=prefer local context",
            location="Austin, TX, US; timezone=America/Chicago",
            thread_id="thread-123",
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        )
    )

    client.send_user_message("What's my location?")

    assert seen_payloads == [
        {
            "content": (
                "Client context:\n"
                "timezone=America/Chicago\n"
                "note=prefer local context\n\n"
                "What's my location?"
            ),
            "metadata": {"raw_user_prompt": "What's my location?"},
        }
    ]


def test_minigent_client_can_log_full_prompt_for_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_payloads: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def fake_urlopen(request: object) -> FakeResponse:
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        seen_payloads.append(payload)
        return FakeResponse({"id": "message-1"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    output_stream = StringIO()

    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            location="Austin, TX, US; timezone=America/Chicago",
            debug_show_prompt=True,
            thread_id="thread-123",
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        ),
        output_stream=output_stream,
    )

    client.send_user_message("What's my location?")

    assert seen_payloads == [
        {
            "content": (
                "Client context:\n"
                "location=Austin, TX, US; timezone=America/Chicago\n\n"
                "What's my location?"
            ),
            "metadata": {"raw_user_prompt": "What's my location?"},
        }
    ]
    assert output_stream.getvalue() == (
        "[prompt]\n"
        "Client context:\n"
        "location=Austin, TX, US; timezone=America/Chicago\n\n"
        "What's my location?\n"
    )


def test_minigent_api_client_exposes_shared_thread_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, payload: object | None = None) -> None:
            self._payload = payload

        def read(self) -> bytes:
            if self._payload is None:
                return b""
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def fake_urlopen(request: object) -> FakeResponse:
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "payload": payload,
                "headers": dict(request.header_items()),
            }
        )
        if request.full_url.endswith("/health"):
            return FakeResponse({"status": "ok"})
        if request.full_url.endswith("/config"):
            return FakeResponse({"llm_provider": "mock"})
        if request.full_url.endswith("/execution-options"):
            return FakeResponse(
                {
                    "skills": {
                        "default": "support",
                        "items": [{"name": "support", "description": "Support"}],
                    },
                    "capability_profiles": {"default": None, "items": []},
                }
            )
        if request.full_url.endswith("/threads"):
            return FakeResponse({"thread_id": "thread-1"})
        if (
            request.full_url.endswith("/threads/thread-1/messages")
            and request.get_method() == "POST"
        ):
            return FakeResponse({"id": "message-1"})
        if (
            request.full_url.endswith("/threads/thread-1/messages")
            and request.get_method() == "GET"
        ):
            return FakeResponse([{"role": "user", "content": "hello"}])
        if request.full_url.endswith("/threads/thread-1/compact"):
            return FakeResponse({"compacted_message_count": 1, "message_count": 2})
        if request.full_url.endswith("/threads/thread-1/run"):
            return FakeResponse({"reply": "hi"})
        if request.full_url.endswith("/threads/thread-1"):
            return FakeResponse()
        raise AssertionError(f"unexpected url {request.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        )
    )

    assert client.health() == {"status": "ok"}
    assert client.config() == {"llm_provider": "mock"}
    assert client.execution_options() == {
        "skills": {
            "default": "support",
            "items": [{"name": "support", "description": "Support"}],
        },
        "capability_profiles": {"default": None, "items": []},
    }
    assert client.create_thread(
        agent_name="reviewer",
        skills=["coding", "review"],
        capability_profile="dev",
        llm_profile="claude",
    ) == {"thread_id": "thread-1"}
    assert client.add_message("thread-1", "hello") == {"id": "message-1"}
    assert client.get_thread("thread-1") == {
        "thread_id": "thread-1",
        "messages": [{"role": "user", "content": "hello"}],
    }
    assert client.run_thread("thread-1", stream=False) == ("hi", None)
    assert client.compact_thread("thread-1") == {"compacted_message_count": 1, "message_count": 2}
    client.delete_thread("thread-1")

    assert [request["method"] for request in requests] == [
        "GET",
        "GET",
        "GET",
        "POST",
        "POST",
        "GET",
        "POST",
        "POST",
        "DELETE",
    ]
    assert requests[3]["payload"] == {
        "agent_name": "reviewer",
        "skill_names": ["coding", "review"],
        "capability_profile": "dev",
        "llm_profile": "claude",
    }


def test_minigent_client_discards_upload_when_message_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        )
    )
    calls: list[tuple[str, str]] = []

    def fake_request_json(
        method: str,
        url: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> object:
        del payload
        calls.append((method, url))
        if url.endswith("/attachments"):
            return {"attachment_id": "attachment-1"}
        if method == "POST" and url.endswith("/messages"):
            raise MinigentAPIError("message failed", status_code=500)
        if method == "DELETE" and url.endswith("/attachments/attachment-1"):
            return None
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(client, "request_json", fake_request_json)

    with pytest.raises(MinigentAPIError, match="message failed"):
        client.add_message(
            "thread-1",
            "describe",
            parts=[
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": "aW1hZ2U=",
                }
            ],
        )

    assert [method for method, _url in calls] == ["POST", "POST", "DELETE"]
    assert calls[-1][1].endswith("/threads/thread-1/attachments/attachment-1")


def test_minigent_client_can_run_thread_with_ndjson_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class FakeStreamResponse:
        def __iter__(self):
            lines = [
                {"type": "run.started"},
                {"type": "tool.call", "name": "echo"},
                {"type": "tool.result", "name": "echo", "is_error": False},
                {
                    "type": "peer.task.created",
                    "peer": "pi",
                    "task_id": "task-1",
                    "status": "queued",
                },
                {"type": "peer.task.poll", "peer": "pi", "task_id": "task-1", "status": "running"},
                {"type": "peer.task.poll", "peer": "pi", "task_id": "task-1", "status": "running"},
                {
                    "type": "peer.task.event",
                    "task_id": "task-1",
                    "event": {"type": "message_update"},
                },
                {
                    "type": "peer.task.event",
                    "task_id": "task-1",
                    "event": {"type": "message_update"},
                },
                {
                    "type": "peer.task.event",
                    "task_id": "task-1",
                    "event": {"type": "tool_execution_start", "tool_name": "read"},
                },
                {"type": "assistant.message", "content": "streamed reply"},
                {"type": "run.completed"},
            ]
            return iter((json.dumps(line) + "\n").encode("utf-8") for line in lines)

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def fake_urlopen(request: object) -> FakeStreamResponse:
        requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "headers": dict(request.header_items()),
            }
        )
        return FakeStreamResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    output_stream = StringIO()
    progress_stream = StringIO()
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            stream_runs=True,
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        ),
        output_stream=output_stream,
        progress_stream=progress_stream,
    )

    reply, _metadata = client.run_thread()

    assert reply == "streamed reply"
    assert requests == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8000/threads/thread-123/run/stream",
            "headers": {
                "Accept": "application/x-ndjson",
                "X-minigent-user-id": "user-1",
                "X-minigent-tenant-id": "tenant-1",
                "X-minigent-admin": "false",
            },
        }
    ]
    assert output_stream.getvalue() == ""
    client.flush_pending_token_summary()
    assert progress_stream.getvalue() == (
        "● preparing\n"
        "🔧 echo() ...\n"
        "🔧 echo() done\n"
        "[peer] task created peer=pi task_id=task-1 status=queued\n"
        "[peer] task status peer=pi task_id=task-1 status=running\n"
        "[peer] message updating...\n"
        "[peer] tool start read\n"
        "● done\n"
    )


def test_minigent_client_notes_peer_token_usage_unavailable_for_live_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStreamResponse:
        def __iter__(self):
            lines = [
                {"type": "run.started"},
                {
                    "type": "peer.task.created",
                    "peer": "pi",
                    "task_id": "task-1",
                    "status": "queued",
                },
                {"type": "assistant.message", "content": "streamed reply"},
                {"type": "run.completed"},
            ]
            return iter((json.dumps(line) + "\n").encode("utf-8") for line in lines)

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda request: FakeStreamResponse())
    progress_stream = StringIO()
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            stream_runs=True,
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        ),
        progress_stream=progress_stream,
        token_mode="live",
    )

    assert client.run_thread() == ("streamed reply", None)
    assert "● done · tokens unavailable for peer backend" in progress_stream.getvalue()


def test_format_thread_context_summary() -> None:
    assert (
        format_thread_context_summary(
            {"type": "run.completed", "thread_context": {"estimated": True, "total_tokens": 42}}
        )
        == "thread context est.: 42"
    )
    assert format_thread_context_summary({"type": "run.completed"}) is None


def test_minigent_client_can_show_stream_tool_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStreamResponse:
        def __iter__(self):
            lines = [
                {"type": "tool.call", "name": "echo", "arguments": {"text": "hi"}},
                {
                    "type": "tool.result",
                    "name": "echo",
                    "is_error": False,
                    "result": {"text": "hi"},
                },
                {"type": "assistant.message", "content": "streamed reply"},
                {"type": "run.completed"},
            ]
            return iter((json.dumps(line) + "\n").encode("utf-8") for line in lines)

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda request: FakeStreamResponse())
    progress_stream = StringIO()
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            stream_runs=True,
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        ),
        progress_stream=progress_stream,
        show_tool_results=True,
    )

    assert client.run_thread() == ("streamed reply", None)
    progress = progress_stream.getvalue()
    assert '🔧 echo(text="hi") done' in progress
    assert "   result:" in progress
    assert '"text": "hi"' in progress


def test_minigent_client_shows_limited_peer_stream_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStreamResponse:
        def __iter__(self):
            lines = [
                {
                    "type": "peer.task.event",
                    "task_id": "task-1",
                    "event": {
                        "type": "tool_execution_start",
                        "tool_name": "read",
                        "toolCallId": "call-1",
                        "args_summary": 'path="README.md", limit=20',
                    },
                },
                {
                    "type": "peer.task.event",
                    "task_id": "task-1",
                    "event": {
                        "type": "tool_execution_end",
                        "tool_name": "read",
                        "toolCallId": "call-1",
                    },
                },
                {
                    "type": "peer.task.event",
                    "task_id": "task-1",
                    "event": {
                        "type": "tool_execution_start",
                        "toolCall": {"name": "grep"},
                        "args_summary": 'pattern="tool_execution", path="."',
                    },
                },
                {"type": "assistant.message", "content": "streamed reply"},
                {"type": "run.completed"},
            ]
            return iter((json.dumps(line) + "\n").encode("utf-8") for line in lines)

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda request: FakeStreamResponse())
    progress_stream = StringIO()
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            stream_runs=True,
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        ),
        progress_stream=progress_stream,
    )

    assert client.run_thread() == ("streamed reply", None)
    progress = progress_stream.getvalue()
    assert '[peer] tool start read(path="README.md", limit=20)' in progress
    assert '[peer] tool end read(path="README.md", limit=20)' in progress
    assert '[peer] tool start grep(pattern="tool_execution", path=".")' in progress


def test_minigent_client_can_show_peer_stream_tool_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStreamResponse:
        def __iter__(self):
            lines = [
                {
                    "type": "peer.task.event",
                    "task_id": "task-1",
                    "event": {
                        "type": "tool_execution_update",
                        "toolName": "temperature",
                        "toolCallId": "call-1",
                        "status": "completed",
                        "partialResult": {"indoor": "72 F"},
                        "result": {"indoor": "72 F", "outdoor": "84 F"},
                    },
                },
                {"type": "assistant.message", "content": "streamed reply"},
                {"type": "run.completed"},
            ]
            return iter((json.dumps(line) + "\n").encode("utf-8") for line in lines)

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda request: FakeStreamResponse())
    progress_stream = StringIO()
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            stream_runs=True,
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        ),
        progress_stream=progress_stream,
        show_tool_results=True,
    )

    assert client.run_thread() == ("streamed reply", None)
    progress = progress_stream.getvalue()
    assert "[peer] tool update temperature status=completed" in progress
    assert "   details:" in progress
    assert '"partialResult": {' in progress
    assert '"indoor": "72 F"' in progress
    assert '"outdoor": "84 F"' in progress
    assert "toolCallId" not in progress


def test_minigent_client_can_show_nested_peer_stream_tool_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStreamResponse:
        def __iter__(self):
            lines = [
                {
                    "type": "peer.task.event",
                    "task_id": "task-1",
                    "event": {
                        "type": "tool_execution_end",
                        "toolCall": {"name": "current_time", "arguments": {}},
                        "resultPayload": {"time": "9:55 PM", "timezone": "CDT"},
                    },
                },
                {"type": "assistant.message", "content": "streamed reply"},
                {"type": "run.completed"},
            ]
            return iter((json.dumps(line) + "\n").encode("utf-8") for line in lines)

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda request: FakeStreamResponse())
    progress_stream = StringIO()
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            stream_runs=True,
            principal=PrincipalConfig(user_id="user-1", tenant_id="tenant-1"),
        ),
        progress_stream=progress_stream,
        show_tool_results=True,
    )

    assert client.run_thread() == ("streamed reply", None)
    progress = progress_stream.getvalue()
    assert "[peer] tool end current_time" in progress
    assert "resultPayload" in progress
    assert '"time": "9:55 PM"' in progress


def test_minigent_client_stream_run_errors_raise_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStreamResponse:
        def __iter__(self):
            event = {"type": "run.error", "status_code": 502, "detail": "upstream"}
            return iter([(json.dumps(event) + "\n").encode("utf-8")])

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda request: FakeStreamResponse())
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            stream_runs=True,
        )
    )

    with pytest.raises(RuntimeError, match=r"Minigent server error \(502\). upstream"):
        client.run_thread()


def test_minigent_client_stream_run_structured_provider_errors_are_concise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStreamResponse:
        def __iter__(self):
            event = {
                "type": "run.error",
                "status_code": 429,
                "detail": {
                    "type": "provider_rate_limited",
                    "message": "Gemini quota exceeded. Retry in about 51s.",
                    "provider": "gemini",
                    "retry_after_seconds": 51,
                },
            }
            return iter([(json.dumps(event) + "\n").encode("utf-8")])

        def __enter__(self) -> "FakeStreamResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda request: FakeStreamResponse())
    progress_stream = StringIO()
    client = MinigentAPIClient(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-123",
            stream_runs=True,
        ),
        progress_stream=progress_stream,
    )

    with pytest.raises(MinigentAPIError) as exc_info:
        client.run_thread()

    assert exc_info.value.message == "Gemini quota exceeded. Retry in about 51s."
    assert exc_info.value.category == "provider_rate_limited"
    assert "✖ error 429: Gemini quota exceeded. Retry in about 51s." in progress_stream.getvalue()


def test_build_config_prefers_cli_stream_run_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_CLIENT_STREAM_RUNS", "false")

    args = build_parser().parse_args(["--stream-runs"])
    config = build_config(args)

    assert config.stream_runs is True


def test_build_config_prefers_cli_ducking_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_VOICE_DUCKING_MODE", "off")
    monkeypatch.setenv("MINIGENT_VOICE_DUCKED_OUTPUT_VOLUME", "20")

    args = build_parser().parse_args(
        ["--ducking-mode", "input-only", "--ducked-output-volume", "12"]
    )
    config = build_config(args)

    assert config.ducking_mode == "input-only"
    assert config.ducked_output_volume == 12


def test_build_config_prefers_cli_piper_pacing_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_VOICE_TTS_LENGTH_SCALE", "1.1")
    monkeypatch.setenv("MINIGENT_VOICE_TTS_SENTENCE_SILENCE", "0.4")

    args = build_parser().parse_args(
        ["--tts-length-scale", "1.25", "--tts-sentence-silence", "0.7"]
    )
    config = build_config(args)

    assert config.tts_length_scale == 1.25
    assert config.tts_sentence_silence == 0.7


def test_build_config_prefers_cli_capture_ended_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT", "bell")

    args = build_parser().parse_args(["--capture-ended-acknowledgement", "done"])
    config = build_config(args)

    assert config.capture_ended_acknowledgement == "done"


def test_build_config_accepts_none_tts_provider_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_VOICE_TTS_PROVIDER", "none")

    config = build_config(build_parser().parse_args([]))

    assert config.tts_provider == "none"


def test_parser_accepts_chat_backend() -> None:
    args = build_parser().parse_args(["--backend", "chat"])

    assert args.backend == "chat"


def test_backend_subcommand_skips_config_value() -> None:
    backend, argv = voice_cli._consume_backend_subcommand(["--config", "client.toml", "chat"])

    assert backend == "chat"
    assert argv == ["--config", "client.toml"]


def test_wrap_feedback_with_ambient_restore_uses_controller_hook() -> None:
    events: list[str] = []

    class FakeAmbientController:
        def temporarily_restore(self, callback, *, reduck_delay_seconds: float = 0.0) -> None:
            events.append(f"delay:{reduck_delay_seconds}")
            events.append("restore")
            callback()
            events.append("duck")

    feedback = voice_cli.wrap_feedback_with_ambient_restore(
        lambda: events.append("bell"),
        FakeAmbientController(),
        reduck_delay_seconds=0.5,
    )

    assert feedback is not None
    feedback()
    assert events == ["delay:0.5", "restore", "bell", "duck"]


def test_bounded_output_volume_rejects_out_of_range_values() -> None:
    assert bounded_output_volume("0") == 0
    assert bounded_output_volume("100") == 100
    with pytest.raises(argparse.ArgumentTypeError):
        bounded_output_volume("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        bounded_output_volume("101")


def test_should_duck_for_state_only_covers_input_states() -> None:
    assert should_duck_for_state(ClientState.LISTENING) is True
    assert should_duck_for_state(ClientState.FOLLOW_UP_LISTENING) is True
    assert should_duck_for_state(ClientState.IDLE) is False
    assert should_duck_for_state(ClientState.THINKING) is False
    assert should_duck_for_state(ClientState.SPEAKING) is False


def test_macos_ambient_volume_ducker_reads_sets_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    commands: list[list[str]] = []

    class Result:
        def __init__(self, stdout: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(command, text, capture_output, check):
        commands.append(command)
        if "output volume of (get volume settings)" in command[-1]:
            return Result(stdout="42\n")
        return Result()

    monkeypatch.setattr("minigent_client.ducking.subprocess.run", fake_run)

    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=15, output_stream=output_stream)
    ducker.sync_state(ClientState.LISTENING)
    ducker.sync_state(ClientState.FOLLOW_UP_LISTENING)
    ducker.sync_state(ClientState.THINKING)

    assert commands == [
        ["osascript", "-e", "output volume of (get volume settings)"],
        ["osascript", "-e", "set volume output volume 15"],
        ["osascript", "-e", "set volume output volume 42"],
    ]
    assert output_stream.getvalue() == ""


def test_macos_ambient_volume_ducker_close_without_ducking_is_noop() -> None:
    output_stream = StringIO()
    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=15, output_stream=output_stream)

    ducker.close()

    assert output_stream.getvalue() == ""


def test_macos_ambient_volume_ducker_restores_on_close(monkeypatch: pytest.MonkeyPatch) -> None:
    output_stream = StringIO()
    commands: list[list[str]] = []

    class Result:
        def __init__(self, stdout: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(command, text, capture_output, check):
        commands.append(command)
        if "output volume of (get volume settings)" in command[-1]:
            return Result(stdout="35\n")
        return Result()

    monkeypatch.setattr("minigent_client.ducking.subprocess.run", fake_run)

    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=10, output_stream=output_stream)
    ducker.sync_state(ClientState.LISTENING)
    ducker.close()

    assert commands[-1] == ["osascript", "-e", "set volume output volume 35"]


def test_macos_ambient_volume_ducker_temporarily_restores_for_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    commands: list[list[str]] = []
    events: list[str] = []
    background_targets = []

    class Result:
        def __init__(self, stdout: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def fake_run(command, text, capture_output, check):
        commands.append(command)
        if "output volume of (get volume settings)" in command[-1]:
            return Result(stdout="55\n")
        return Result()

    class FakeThread:
        def __init__(self, target, args=(), daemon: bool = False) -> None:
            assert daemon is True
            self.target = target
            self.args = args

        def start(self) -> None:
            background_targets.append((self.target, self.args))

    monkeypatch.setattr("minigent_client.ducking.subprocess.run", fake_run)
    monkeypatch.setattr("minigent_client.ducking.threading.Thread", FakeThread)

    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=10, output_stream=output_stream)
    ducker.sync_state(ClientState.LISTENING)
    ducker.temporarily_restore(lambda: events.append("bell"))

    assert events == ["bell"]
    assert commands == [
        ["osascript", "-e", "output volume of (get volume settings)"],
        ["osascript", "-e", "set volume output volume 10"],
        ["osascript", "-e", "set volume output volume 55"],
    ]
    assert len(background_targets) == 1

    background_targets[0][0](*background_targets[0][1])

    assert commands == [
        ["osascript", "-e", "output volume of (get volume settings)"],
        ["osascript", "-e", "set volume output volume 10"],
        ["osascript", "-e", "set volume output volume 55"],
        ["osascript", "-e", "output volume of (get volume settings)"],
        ["osascript", "-e", "set volume output volume 10"],
    ]


def test_macos_ambient_volume_ducker_warns_once_and_disables_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()

    def fake_run(command, text, capture_output, check):
        raise OSError("osascript missing")

    monkeypatch.setattr("minigent_client.ducking.subprocess.run", fake_run)

    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=10, output_stream=output_stream)
    ducker.sync_state(ClientState.LISTENING)
    ducker.sync_state(ClientState.FOLLOW_UP_LISTENING)
    ducker.close()

    assert output_stream.getvalue().count("ambient audio ducking disabled") == 1


def test_recorded_audio_to_wav_bytes_round_trips_header() -> None:
    audio = RecordedAudio(
        pcm_bytes=b"\x00\x00\xff\x7f",
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
    )

    wav_bytes = audio.to_wav_bytes()

    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"
    assert audio.duration_seconds == 0.000125
    assert audio.nonzero_samples == 1
    assert audio.peak_abs_sample == 32767
    assert audio.peak_dbfs == 0.0


def test_apply_gain_scales_audio_and_clamps() -> None:
    audio = RecordedAudio(
        pcm_bytes=b"\x00\x40\xff\x7f",
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
    )

    boosted = apply_gain(audio, 2.0)

    assert boosted.peak_abs_sample == 32767


def test_normalize_peak_returns_gain_and_adjusted_audio() -> None:
    audio = RecordedAudio(
        pcm_bytes=b"\x00\x10\x00\x20",
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
    )

    normalized, gain = normalize_peak(audio, target_peak=0.5)

    assert gain > 1.0
    assert normalized.peak_abs_sample == 16384


def test_load_recorded_audio_from_wav_round_trips(tmp_path) -> None:
    wav_path = tmp_path / "sample.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00\x01\x00")

    audio = load_recorded_audio_from_wav(wav_path)

    assert audio == RecordedAudio(
        pcm_bytes=b"\x00\x00\x01\x00",
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
    )


def test_pad_with_silence_adds_expected_duration() -> None:
    audio = RecordedAudio(
        pcm_bytes=b"\x01\x00" * 160,
        sample_rate=16_000,
        channels=1,
        sample_width_bytes=2,
    )

    padded = pad_with_silence(audio, leading_ms=250, trailing_ms=500)

    assert padded.duration_seconds == pytest.approx(audio.duration_seconds + 0.75)
    assert padded.pcm_bytes[: 16_000 // 2] == b"\x00" * (16_000 // 2)
    assert padded.pcm_bytes[-16_000:] == b"\x00" * 16_000


def test_record_after_speech_from_stream_includes_preroll() -> None:
    class FakeDetector:
        def __init__(self) -> None:
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

        def is_speech(self, samples: list[float], sample_rate: int) -> bool:
            del sample_rate
            return any(abs(sample) > 0.0 for sample in samples)

    class FakeStream:
        def __init__(self) -> None:
            self.chunks = iter(
                [
                    b"\x00\x00\x00\x00",
                    b"\x01\x00\x01\x00",
                    b"\x02\x00\x02\x00",
                    b"\x00\x00\x00\x00",
                    b"\x00\x00\x00\x00",
                ]
            )

        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            try:
                return next(self.chunks), False
            except StopIteration:
                return b"", False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    recorder = MicrophoneRecorder(
        AudioCaptureConfig(
            sample_rate=16_000,
            channels=1,
            sample_width_bytes=2,
            block_size=2,
            max_record_seconds=1.0,
            end_silence_ms=0,
        ),
        detector=FakeDetector(),
    )

    audio = recorder.record_after_speech_from_stream(
        FakeStream(),
        timeout_ms=100,
        preroll_ms=1,
    )

    assert audio is not None
    assert audio.pcm_bytes.startswith(b"\x00\x00\x00\x00\x01\x00\x01\x00")


def test_pcm16le_to_floats_normalizes_samples() -> None:
    assert pcm16le_to_floats(b"\x00\x80\x00\x00\xff\x7f") == [-1.0, 0.0, 32767 / 32768]


def test_manual_audio_activation_source_records_and_transcribes() -> None:
    events: list[str] = []

    class FakeRecorder:
        def record_until_silence(self) -> RecordedAudio:
            events.append("record")
            return RecordedAudio(
                pcm_bytes=b"\x00\x00" * 3200,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )

    class FakeTranscriber:
        def transcribe(self, audio: RecordedAudio) -> str:
            events.append("transcribe")
            assert audio.sample_rate == 16_000
            return "hello from microphone"

    output_stream = StringIO()
    source = ManualAudioActivationSource(
        input_stream=StringIO("\n"),
        output_stream=output_stream,
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        capture_ended_feedback=lambda: events.append("capture-ended"),
    )

    activation = source.wait_for_activation("ignored")
    transcript = source.capture_utterance()

    assert activation == Activation()
    assert transcript == "hello from microphone"
    assert events == ["record", "capture-ended", "transcribe"]
    assert "[listening] capture ended" in output_stream.getvalue()
    assert "[transcribing] captured 0.20s of audio" in output_stream.getvalue()
    assert "[transcript] hello from microphone" in output_stream.getvalue()


def test_manual_audio_activation_source_ignores_stt_error() -> None:
    class FakeRecorder:
        def record_until_silence(self) -> RecordedAudio:
            return RecordedAudio(
                pcm_bytes=b"\x00\x00" * 3200,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )

    class FakeTranscriber:
        def transcribe(self, audio: RecordedAudio) -> str:
            del audio
            raise SpeechToTextError(
                "STT provider returned assistant-style text instead of a transcript"
            )

    output_stream = StringIO()
    source = ManualAudioActivationSource(
        input_stream=StringIO("\n"),
        output_stream=output_stream,
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
    )

    transcript = source.capture_utterance()

    assert transcript == ""
    assert "[idle] transcription failed, ignoring capture:" in output_stream.getvalue()


def test_manual_audio_activation_source_does_not_support_follow_up_window() -> None:
    source = ManualAudioActivationSource(
        input_stream=StringIO("\n"),
        output_stream=StringIO(),
        recorder=object(),  # type: ignore[arg-type]
        transcriber=object(),  # type: ignore[arg-type]
    )

    assert source.capture_follow_up_utterance(5000) is None


def test_capture_debugger_logs_and_writes_capture(tmp_path) -> None:
    output_stream = StringIO()
    capture_path = tmp_path / "last-capture.wav"
    debugger = CaptureDebugger(
        CaptureDebugConfig(capture_path=str(capture_path)),
        output_stream=output_stream,
    )

    debugger.log_capture(
        RecordedAudio(
            pcm_bytes=b"\x00\x00\x01\x00\x00\x00",
            sample_rate=16_000,
            channels=1,
            sample_width_bytes=2,
        ),
        source="passive-audio",
    )

    assert "[capture] source=passive-audio" in output_stream.getvalue()
    assert "peak_dbfs=" in output_stream.getvalue()
    assert "rms_dbfs=" in output_stream.getvalue()
    assert "[capture] wrote" in output_stream.getvalue()
    assert capture_path.exists()
    assert capture_path.read_bytes()[:4] == b"RIFF"


def test_passive_audio_activation_source_records_on_existing_stream() -> None:
    events: list[str] = []

    class FakeStream:
        def __init__(self) -> None:
            self.chunks = iter([b"\x00\x00" * 4, b"\x01\x00" * 4])

        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            try:
                return next(self.chunks), False
            except StopIteration:
                return b"", False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def __init__(self) -> None:
            self.calls = 0
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

        def process_chunk(self, chunk: bytes) -> bool:
            self.calls += 1
            return self.calls >= 2

    class FakeRecorder:
        def __init__(self) -> None:
            self.record_calls = 0
            self.timeout_ms: int | None = None
            self.stream: object | None = None

        def record_after_speech_from_stream(
            self,
            stream,
            *,
            timeout_ms: int,
            preroll_ms: int = 250,
        ) -> RecordedAudio | None:
            events.append("record")
            self.record_calls += 1
            self.stream = stream
            self.timeout_ms = timeout_ms
            self.preroll_ms = preroll_ms
            return RecordedAudio(
                pcm_bytes=b"\x03\x00" * 8,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )

    class FakeTranscriber:
        def transcribe(self, audio: RecordedAudio) -> str:
            events.append("transcribe")
            leading_bytes = 16_000 // 2
            trailing_bytes = 16_000
            assert audio.pcm_bytes[:leading_bytes] == (b"\x00" * leading_bytes)
            assert audio.pcm_bytes[leading_bytes:-trailing_bytes] == (b"\x03\x00" * 8)
            assert audio.pcm_bytes[-trailing_bytes:] == (b"\x00" * trailing_bytes)
            return "wake word request"

    output_stream = StringIO()
    recorder = FakeRecorder()
    stream = FakeStream()
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=stream,
        recorder=recorder,
        transcriber=FakeTranscriber(),
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
        activation_feedback=lambda: events.append("wake"),
        capture_ended_feedback=lambda: events.append("capture-ended"),
        post_wake_speech_timeout_ms=100,
        post_wake_settle_ms=0,
        wakeword_cooldown_ms=100,
        stt_pad_leading_ms=250,
        stt_pad_trailing_ms=500,
    )

    activation = source.wait_for_activation("hey minigent")
    transcript = source.capture_utterance()

    assert activation == Activation()
    assert transcript == "wake word request"
    assert events == ["wake", "record", "capture-ended", "transcribe"]
    assert "[listening] capture ended" in output_stream.getvalue()
    assert recorder.record_calls == 1
    assert recorder.stream is stream
    assert recorder.timeout_ms == 100
    assert recorder.preroll_ms == 250
    assert source.wake_detector.reset_calls == 1
    assert (
        "[idle] passive listening for wake word openwakeword:okay_nabu" in output_stream.getvalue()
    )
    assert "[listening] wake word detected" in output_stream.getvalue()


def test_passive_audio_activation_source_settles_before_wake_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeStream:
        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            return b"", False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def reset(self) -> None:
            return None

        def process_chunk(self, chunk: bytes) -> bool:
            del chunk
            return False

    class FakeRecorder:
        def record_after_speech_from_stream(
            self,
            stream,
            *,
            timeout_ms: int,
            preroll_ms: int = 250,
        ) -> RecordedAudio | None:
            del stream, timeout_ms, preroll_ms
            events.append("record")
            return RecordedAudio(
                pcm_bytes=b"\x03\x00" * 8,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )

    class FakeTranscriber:
        def transcribe(self, audio: RecordedAudio) -> str:
            del audio
            events.append("transcribe")
            return "wake word request"

    monkeypatch.setattr(
        "minigent_client.backends.passive_audio.time.sleep",
        lambda seconds: events.append(f"sleep:{seconds}"),
    )

    source = PassiveAudioActivationSource(
        output_stream=StringIO(),
        stream=FakeStream(),
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
        activation_feedback=lambda: events.append("wake"),
        post_wake_settle_ms=250,
        stt_pad_leading_ms=0,
        stt_pad_trailing_ms=0,
    )

    transcript = source.capture_utterance()

    assert transcript == "wake word request"
    assert events == ["sleep:0.25", "wake", "record", "transcribe"]


def test_passive_audio_activation_source_captures_follow_up_without_wake_word() -> None:
    events: list[str] = []

    class FakeStream:
        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            return b"", False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def reset(self) -> None:
            return None

        def process_chunk(self, chunk: bytes) -> bool:
            del chunk
            return False

    class FakeRecorder:
        def __init__(self) -> None:
            self.timeout_ms: int | None = None

        def record_after_speech(
            self, timeout_ms: int, *, preroll_ms: int = 250
        ) -> RecordedAudio | None:
            events.append("record")
            self.timeout_ms = timeout_ms
            self.preroll_ms = preroll_ms
            return RecordedAudio(
                pcm_bytes=b"\x03\x00" * 8,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )

    class FakeTranscriber:
        def transcribe(self, audio: RecordedAudio) -> str:
            events.append("transcribe")
            del audio
            return "follow up request"

    output_stream = StringIO()
    recorder = FakeRecorder()
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=FakeStream(),
        recorder=recorder,
        transcriber=FakeTranscriber(),
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
        capture_ended_feedback=lambda: events.append("capture-ended"),
        post_wake_settle_ms=0,
    )

    transcript = source.capture_follow_up_utterance(5000)

    assert transcript == "follow up request"
    assert events == ["record", "capture-ended", "transcribe"]
    assert "[follow-up] capture ended" in output_stream.getvalue()
    assert recorder.timeout_ms == 5000
    assert "[follow-up] listening for a follow-up without the wake word" in output_stream.getvalue()


def test_passive_audio_activation_source_follow_up_window_expires() -> None:
    class FakeStream:
        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            return b"", False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def reset(self) -> None:
            return None

        def process_chunk(self, chunk: bytes) -> bool:
            del chunk
            return False

    class FakeRecorder:
        def record_after_speech(
            self, timeout_ms: int, *, preroll_ms: int = 250
        ) -> RecordedAudio | None:
            del timeout_ms, preroll_ms
            return None

    output_stream = StringIO()
    feedback_calls: list[str] = []
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=FakeStream(),
        recorder=FakeRecorder(),
        transcriber=object(),  # type: ignore[arg-type]
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
        capture_ended_feedback=lambda: feedback_calls.append("capture-ended"),
    )

    transcript = source.capture_follow_up_utterance(3000)

    assert transcript is None
    assert feedback_calls == ["capture-ended"]
    assert "[follow-up] capture ended" in output_stream.getvalue()
    assert (
        "[idle] follow-up window expired, returning to wake-word mode" in output_stream.getvalue()
    )


def test_passive_audio_activation_source_detects_barge_in() -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.chunks = iter([b"\x00\x00" * 4, b"\x01\x00" * 4])

        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            try:
                return next(self.chunks), False
            except StopIteration:
                return b"", False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def __init__(self) -> None:
            self.calls = 0
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

        def process_chunk(self, chunk: bytes) -> bool:
            del chunk
            self.calls += 1
            return self.calls >= 2

    output_stream = StringIO()
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=FakeStream(),
        recorder=object(),  # type: ignore[arg-type]
        transcriber=object(),  # type: ignore[arg-type]
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
    )

    activation = source.wait_for_barge_in("ignored", lambda: True)

    assert activation == Activation()
    assert source.wake_detector.reset_calls == 1
    assert "[listening] wake word detected, interrupting speech" in output_stream.getvalue()


def test_passive_audio_activation_source_ignores_wake_without_followup_speech() -> None:
    class FakeStream:
        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            return b"\x00\x00" * 4, False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def reset(self) -> None:
            return None

        def process_chunk(self, chunk: bytes) -> bool:
            del chunk
            return False

    class FakeRecorder:
        def __init__(self) -> None:
            self.timeout_ms: int | None = None
            self.stream: object | None = None

        def record_after_speech_from_stream(
            self,
            stream,
            *,
            timeout_ms: int,
            preroll_ms: int = 250,
        ) -> RecordedAudio | None:
            self.stream = stream
            self.timeout_ms = timeout_ms
            self.preroll_ms = preroll_ms
            return None

    class FakeTranscriber:
        def transcribe(self, audio: RecordedAudio) -> str:
            raise AssertionError(f"unexpected transcription for {audio}")

    output_stream = StringIO()
    recorder = FakeRecorder()
    feedback_calls: list[str] = []
    stream = FakeStream()
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=stream,
        recorder=recorder,
        transcriber=FakeTranscriber(),
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
        capture_ended_feedback=lambda: feedback_calls.append("capture-ended"),
        post_wake_speech_timeout_ms=250,
        post_wake_settle_ms=0,
        wakeword_cooldown_ms=100,
        stt_pad_leading_ms=0,
        stt_pad_trailing_ms=0,
    )

    transcript = source.capture_utterance()

    assert transcript == ""
    assert feedback_calls == []
    assert recorder.stream is stream
    assert recorder.timeout_ms == 250
    assert recorder.preroll_ms == 250
    assert "[idle] no speech after wake word, ignoring activation" in output_stream.getvalue()


def test_passive_audio_activation_source_ignores_stt_error() -> None:
    class FakeStream:
        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            return b"\x00\x00" * 4, False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def reset(self) -> None:
            return None

        def process_chunk(self, chunk: bytes) -> bool:
            del chunk
            return False

    class FakeRecorder:
        def record_after_speech_from_stream(
            self,
            stream,
            *,
            timeout_ms: int,
            preroll_ms: int = 250,
        ) -> RecordedAudio | None:
            del stream
            del timeout_ms
            del preroll_ms
            return RecordedAudio(
                pcm_bytes=b"\x03\x00" * 8,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )

    class FakeTranscriber:
        def transcribe(self, audio: RecordedAudio) -> str:
            del audio
            raise SpeechToTextError(
                "STT provider returned assistant-style text instead of a transcript"
            )

    output_stream = StringIO()
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=FakeStream(),
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
        post_wake_speech_timeout_ms=100,
        post_wake_settle_ms=0,
        wakeword_cooldown_ms=100,
        stt_pad_leading_ms=0,
        stt_pad_trailing_ms=0,
    )

    transcript = source.capture_utterance()

    assert transcript == ""
    assert "[idle] transcription failed, ignoring capture:" in output_stream.getvalue()


def test_passive_audio_activation_source_retries_after_empty_wake_capture() -> None:
    class FakeStream:
        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            return b"", False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def reset(self) -> None:
            return None

        def process_chunk(self, chunk: bytes) -> bool:
            del chunk
            return False

    class FakeRecorder:
        def __init__(self) -> None:
            self.calls = 0

        def record_after_speech_from_stream(
            self,
            stream,
            *,
            timeout_ms: int,
            preroll_ms: int = 250,
        ) -> RecordedAudio | None:
            del stream, timeout_ms, preroll_ms
            self.calls += 1
            sample = b"\x03\x00" if self.calls == 1 else b"\x04\x00"
            return RecordedAudio(
                pcm_bytes=sample * 8,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )

    class FakeTranscriber:
        def __init__(self) -> None:
            self.calls = 0

        def transcribe(self, audio: RecordedAudio) -> str:
            del audio
            self.calls += 1
            if self.calls == 1:
                raise SpeechToTextError(
                    "faster-whisper transcription response did not include text"
                )
            return "actual request"

    output_stream = StringIO()
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=FakeStream(),
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
        post_wake_speech_timeout_ms=100,
        post_wake_settle_ms=0,
        wakeword_cooldown_ms=100,
        stt_pad_leading_ms=0,
        stt_pad_trailing_ms=0,
    )

    transcript = source.capture_utterance()

    assert transcript == "actual request"
    assert "[idle] transcription failed, ignoring capture:" in output_stream.getvalue()
    assert (
        "[follow-up] initial wake capture was empty, listening briefly without the wake word"
        in output_stream.getvalue()
    )
    assert "[transcript] actual request" in output_stream.getvalue()


def test_passive_audio_activation_source_close_is_idempotent() -> None:
    class FakeStream:
        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            return b"", False

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeWakeDetector:
        frame_length = 4
        sample_rate = 16000
        label = "openwakeword:okay_nabu"

        def reset(self) -> None:
            return None

        def process_chunk(self, chunk: bytes) -> bool:
            del chunk
            return False

    class FakeContext:
        def __init__(self) -> None:
            self.exit_calls = 0

        def __exit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            self.exit_calls += 1

    output_stream = StringIO()
    stream_context = FakeContext()
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=FakeStream(),
        recorder=object(),  # type: ignore[arg-type]
        transcriber=object(),  # type: ignore[arg-type]
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
        stream_context=stream_context,
    )

    source.close()
    source.close()

    assert stream_context.exit_calls == 1


def test_minigent_client_cli_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeActivationSource:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class FakeVoiceDaemon:
        def __init__(self, **kwargs) -> None:
            del kwargs

        def run_forever(self) -> None:
            raise KeyboardInterrupt

        def run_once(self) -> str:
            raise AssertionError("run_once should not be called")

    activation_source = FakeActivationSource()
    monkeypatch.setattr(voice_cli, "load_environment", lambda: None)
    monkeypatch.setattr(
        voice_cli,
        "build_config",
        lambda args: ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"),
    )
    monkeypatch.setattr(
        voice_cli,
        "build_activation_source",
        lambda backend, config, **kwargs: activation_source,
    )
    monkeypatch.setattr(
        voice_cli,
        "MinigentAPIClient",
        lambda config, output_stream=None: object(),
    )
    monkeypatch.setattr(voice_cli, "build_speech_output", lambda config: object())
    monkeypatch.setattr(voice_cli, "MinigentClientRuntime", FakeVoiceDaemon)

    exit_code = voice_cli.main(["--backend", "stdin"])

    assert exit_code == 130
    assert capsys.readouterr().out == "[idle] shutting down\n"
    assert activation_source.close_calls == 1


def test_render_prompt_command_replaces_input_placeholder() -> None:
    command = PromptCommand(
        name="rewrite",
        prompt_template="Rewrite this in a friendly tone:\n\n{input}",
    )

    assert voice_cli._render_prompt_command(command, "Send it now.") == (
        "Rewrite this in a friendly tone:\n\nSend it now."
    )


def test_run_chat_loop_expands_custom_slash_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "cli-state.json"
    state = PersistentClientState.load(state_path)
    state.set_prompt_command("rewrite", "Rewrite this in a friendly tone:\n\n{input}")
    state.save()
    output_stream = StringIO()
    input_stream = StringIO("/rewrite Send it now.\n")
    events: list[tuple[str, str]] = []

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> dict[str, str]:
            events.append(("message", content))
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            events.append(("run", "rewritten"))
            return ("rewritten", None)

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)
    monkeypatch.setattr("minigent_client.state.state_file_path", lambda: state_path)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"),
        once=True,
    )

    assert exit_code == 0
    assert events == [
        ("message", "Rewrite this in a friendly tone:\n\nSend it now."),
        ("run", "rewritten"),
    ]


def test_run_chat_loop_reconciles_private_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/actions\n/discard-action consent-1\n")
    discarded: list[tuple[str, str | None]] = []

    class FakeChatClient:
        thread_id = "thread-1"

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def list_private_value_actions(self, thread_id: str) -> list[dict[str, object]]:
            assert thread_id == "thread-1"
            return [
                {
                    "consent_id": "consent-1",
                    "state": "executing",
                    "tool_name": "trusted.send",
                    "expires_at": 700.0,
                }
            ]

        def discard_private_value_action(
            self, consent_id: str, *, thread_id: str | None = None
        ) -> dict[str, object]:
            discarded.append((consent_id, thread_id))
            return {"consent_id": consent_id, "state": "executing", "discarded": True}

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    assert (
        run_chat_loop(ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"))
        == 0
    )
    assert discarded == [("consent-1", "thread-1")]
    output = output_stream.getvalue()
    assert "consent-1  executing  trusted.send  expires=700.0" in output
    assert "discarded executing private action consent-1" in output


def test_run_chat_loop_handles_multiple_turns_and_blank_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("\nfirst question\nsecond question\n")
    events: list[tuple[str, str]] = []

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream
            self.replies = iter(["first reply", "second reply"])

        def send_user_message(self, content: str) -> dict[str, str]:
            events.append(("message", content))
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            reply = next(self.replies)
            events.append(("run", reply))
            return (reply, None)

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert events == [
        ("message", "first question"),
        ("run", "first reply"),
        ("message", "second question"),
        ("run", "second reply"),
    ]
    assert output_stream.getvalue() == (
        "[user] [user] [assistant] first reply\n[user] [assistant] second reply\n"
        "[user] [idle] shutting down\n"
    )


def test_run_chat_loop_image_command_queues_next_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_stream = StringIO()
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(b"hi")
    input_stream = StringIO(f"/image {image_path}\nwhat is this?\n")
    sent_messages: list[tuple[str, list[dict[str, object]] | None]] = []

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(
            self, content: str, *, parts: list[dict[str, object]] | None = None
        ) -> dict[str, str]:
            sent_messages.append((content, parts))
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("it is tiny", None)

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert sent_messages == [
        (
            "what is this?",
            [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": "aGk=",
                    "detail": "auto",
                },
            ],
        )
    ]
    output = output_stream.getvalue()
    assert "[idle] queued 1 image(s) for the next message (1 total)\n" in output
    assert "[assistant] it is tiny\n" in output


def test_run_chat_loop_image_paste_command_queues_clipboard_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/image paste\nwhat is this?\n")
    sent_messages: list[tuple[str, list[dict[str, object]] | None]] = []

    def fake_clipboard_image() -> list[dict[str, object]]:
        return [
            {
                "type": "image",
                "mime_type": "image/png",
                "data": "aGk=",
                "detail": "auto",
                "source_path": "clipboard",
            }
        ]

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(
            self, content: str, *, parts: list[dict[str, object]] | None = None
        ) -> dict[str, str]:
            sent_messages.append((content, parts))
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("clipboard reply", None)

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli, "_image_parts_from_macos_clipboard", fake_clipboard_image)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert sent_messages == [
        (
            "what is this?",
            [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": "aGk=",
                    "detail": "auto",
                },
            ],
        )
    ]
    output = output_stream.getvalue()
    assert "[idle] queued clipboard image for the next message (1 total)\n" in output
    assert "[assistant] clipboard reply\n" in output


def test_build_chat_prompt_session_for_tty_streams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []

    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeFileHistory:
        def __init__(self, path: str) -> None:
            self.path = path
            calls.append(("history", path))

    class FakePromptSession:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            calls.append(("session", kwargs))

    class FakeKeyBindings:
        def __init__(self) -> None:
            calls.append(("key_bindings", self))

        def add(self, *keys: str):
            calls.append(("binding", keys))

            def decorator(handler):
                return handler

            return decorator

    prompt_toolkit_module = type(
        "FakePromptToolkitModule",
        (),
        {"PromptSession": FakePromptSession},
    )()
    history_module = type("FakeHistoryModule", (), {"FileHistory": FakeFileHistory})()
    key_binding_module = type("FakeKeyBindingModule", (), {"KeyBindings": FakeKeyBindings})()

    def fake_import(name: str) -> object:
        if name == "prompt_toolkit":
            return prompt_toolkit_module
        if name == "prompt_toolkit.history":
            return history_module
        if name == "prompt_toolkit.key_binding":
            return key_binding_module
        raise ImportError(name)

    monkeypatch.setattr(voice_cli, "import_module", fake_import)
    monkeypatch.setattr(voice_cli, "chat_history_file_path", lambda: tmp_path / "history")

    session = voice_cli._build_chat_prompt_session(
        input_stream=TtyStream(),
        output_stream=TtyStream(),
    )

    assert isinstance(session, FakePromptSession)
    assert ("history", str(tmp_path / "history")) in calls
    assert ("binding", ("enter",)) in calls
    assert ("binding", ("escape", "enter")) in calls
    assert ("binding", ("c-j",)) in calls
    session_call = calls[-1]
    assert isinstance(session_call, tuple)
    assert session_call[0] == "session"
    session_kwargs = session_call[1]
    assert isinstance(session_kwargs, dict)
    assert isinstance(session_kwargs["history"], FakeFileHistory)
    assert session_kwargs["history"].path == str(tmp_path / "history")
    assert isinstance(session_kwargs["key_bindings"], FakeKeyBindings)
    assert session_kwargs["multiline"] is True
    assert session_kwargs["prompt_continuation"] == ""
    assert "erase_when_done" not in session_kwargs


def test_build_chat_prompt_session_uses_thread_scoped_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []

    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeFileHistory:
        def __init__(self, path: str) -> None:
            self.path = path
            calls.append(("history", path))

    class FakePromptSession:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("session", kwargs))

    class FakeKeyBindings:
        def add(self, *keys: str):
            del keys

            def decorator(handler):
                return handler

            return decorator

    prompt_toolkit_module = type(
        "FakePromptToolkitModule",
        (),
        {"PromptSession": FakePromptSession},
    )()
    history_module = type("FakeHistoryModule", (), {"FileHistory": FakeFileHistory})()
    key_binding_module = type("FakeKeyBindingModule", (), {"KeyBindings": FakeKeyBindings})()

    def fake_import(name: str) -> object:
        if name == "prompt_toolkit":
            return prompt_toolkit_module
        if name == "prompt_toolkit.history":
            return history_module
        if name == "prompt_toolkit.key_binding":
            return key_binding_module
        raise ImportError(name)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))
    monkeypatch.setattr(voice_cli, "import_module", fake_import)

    session = voice_cli._build_chat_prompt_session(
        input_stream=TtyStream(),
        output_stream=TtyStream(),
        history_thread_id="thread/one",
        history_scope_key="http://server|tenant|user",
    )

    assert isinstance(session, FakePromptSession)
    history_calls = [call for call in calls if isinstance(call, tuple) and call[0] == "history"]
    assert len(history_calls) == 1
    history_path = history_calls[0][1]
    assert isinstance(history_path, str)
    assert history_path.startswith(
        str(tmp_path / ".local" / "state" / "minigent" / "client-chat-history.d")
    )
    assert history_path.endswith("thread_one")


def test_chat_history_file_path_migrates_legacy_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    legacy_path = tmp_path / ".minigent" / "client-chat-history"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("legacy prompt history\n", encoding="utf-8")

    path = voice_cli.chat_history_file_path()

    assert path == tmp_path / "xdg-state" / "minigent" / "client-chat-history"
    assert path.read_text(encoding="utf-8") == "legacy prompt history\n"


def test_append_missing_user_messages_to_chat_history_seeds_resumed_thread(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "history"
    messages = [
        {"role": "user", "content": "first prompt"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "multi\nline prompt"},
    ]

    voice_cli._append_missing_user_messages_to_chat_history(history_path, messages)

    entries = voice_cli._read_prompt_toolkit_history_entries(history_path)
    assert entries == ["first prompt", "multi\nline prompt"]


def test_append_missing_user_messages_to_chat_history_prefers_raw_user_prompt_metadata(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "history"
    messages = [
        {
            "role": "user",
            "content": "Client context format may change someday: raw prompt should win",
            "metadata": {"raw_user_prompt": "actual prompt"},
        },
    ]

    voice_cli._append_missing_user_messages_to_chat_history(history_path, messages)

    entries = voice_cli._read_prompt_toolkit_history_entries(history_path)
    assert entries == ["actual prompt"]


def test_append_missing_user_messages_to_chat_history_strips_client_context_preamble(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "history"
    messages = [
        {
            "role": "user",
            "content": (
                "Client context:\n"
                "location=Austin, TX, US; timezone=America/Chicago\n\n"
                "find coffee nearby"
            ),
        },
        {
            "role": "user",
            "content": (
                "Client context:\n"
                "timezone=America/Chicago\n"
                "note=prefer local context\n\n"
                "What's my location?"
            ),
        },
    ]

    voice_cli._append_missing_user_messages_to_chat_history(history_path, messages)

    entries = voice_cli._read_prompt_toolkit_history_entries(history_path)
    assert entries == ["find coffee nearby", "What's my location?"]


def test_append_missing_user_messages_to_chat_history_keeps_existing_entries(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "history"
    voice_cli._append_prompt_toolkit_history_entry(history_path, "local only")
    voice_cli._append_prompt_toolkit_history_entry(history_path, "remote prompt")

    voice_cli._append_missing_user_messages_to_chat_history(
        history_path,
        [
            {"role": "user", "content": "remote prompt"},
            {"role": "user", "content": "new remote prompt"},
        ],
    )

    entries = voice_cli._read_prompt_toolkit_history_entries(history_path)
    assert entries == ["local only", "remote prompt", "new remote prompt"]


def test_build_chat_prompt_session_uses_thread_history_dir_when_legacy_history_file_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []

    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeFileHistory:
        def __init__(self, path: str) -> None:
            self.path = path
            calls.append(("history", path))

    class FakePromptSession:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("session", kwargs))

    class FakeKeyBindings:
        def add(self, *keys: str):
            del keys

            def decorator(handler):
                return handler

            return decorator

    prompt_toolkit_module = type(
        "FakePromptToolkitModule",
        (),
        {"PromptSession": FakePromptSession},
    )()
    history_module = type("FakeHistoryModule", (), {"FileHistory": FakeFileHistory})()
    key_binding_module = type("FakeKeyBindingModule", (), {"KeyBindings": FakeKeyBindings})()

    def fake_import(name: str) -> object:
        if name == "prompt_toolkit":
            return prompt_toolkit_module
        if name == "prompt_toolkit.history":
            return history_module
        if name == "prompt_toolkit.key_binding":
            return key_binding_module
        raise ImportError(name)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))
    monkeypatch.setattr(voice_cli, "import_module", fake_import)
    legacy_history_path = tmp_path / ".minigent" / "client-chat-history"
    legacy_history_path.parent.mkdir(parents=True)
    legacy_history_path.write_text("old shared prompt history\n", encoding="utf-8")

    session = voice_cli._build_chat_prompt_session(
        input_stream=TtyStream(),
        output_stream=TtyStream(),
        history_thread_id="thread-one",
        history_scope_key="scope",
    )

    assert isinstance(session, FakePromptSession)
    history_calls = [call for call in calls if isinstance(call, tuple) and call[0] == "history"]
    assert len(history_calls) == 1
    history_path = history_calls[0][1]
    assert isinstance(history_path, str)
    assert history_path.startswith(
        str(tmp_path / ".local" / "state" / "minigent" / "client-chat-history.d")
    )
    assert history_path.endswith("thread-one")


def test_build_config_accepts_chat_submit_mode() -> None:
    args = build_parser().parse_args(["--chat-submit-mode", "alt-enter"])

    config = build_config(args)

    assert config.chat_submit_mode == "alt-enter"


def test_build_chat_prompt_session_skips_non_tty_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []

    def fake_import(name: str) -> object:
        imported.append(name)
        raise AssertionError("readline import should be skipped for non-tty streams")

    monkeypatch.setattr(voice_cli, "import_module", fake_import)

    session = voice_cli._build_chat_prompt_session(
        input_stream=StringIO(),
        output_stream=StringIO(),
    )

    assert session is None
    assert imported == []


def test_build_chat_prompt_session_falls_back_when_prompt_toolkit_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(
        voice_cli,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError("no prompt_toolkit")),
    )

    session = voice_cli._build_chat_prompt_session(
        input_stream=TtyStream(),
        output_stream=TtyStream(),
    )

    assert session is None


def test_build_chat_prompt_session_falls_back_when_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TtyStream(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeFileHistory:
        def __init__(self, path: str) -> None:
            del path

    class FakePromptSession:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            raise RuntimeError("boom")

    class FakeKeyBindings:
        def add(self, *keys: str):
            del keys

            def decorator(handler):
                return handler

            return decorator

    prompt_toolkit_module = type(
        "FakePromptToolkitModule",
        (),
        {"PromptSession": FakePromptSession},
    )()
    history_module = type("FakeHistoryModule", (), {"FileHistory": FakeFileHistory})()
    key_binding_module = type("FakeKeyBindingModule", (), {"KeyBindings": FakeKeyBindings})()

    def fake_import(name: str) -> object:
        if name == "prompt_toolkit":
            return prompt_toolkit_module
        if name == "prompt_toolkit.history":
            return history_module
        if name == "prompt_toolkit.key_binding":
            return key_binding_module
        raise ImportError(name)

    monkeypatch.setattr(voice_cli, "import_module", fake_import)

    session = voice_cli._build_chat_prompt_session(
        input_stream=TtyStream(),
        output_stream=TtyStream(),
    )

    assert session is None


def test_read_chat_line_uses_interactive_input_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    output_stream = StringIO()

    line = voice_cli._read_chat_line(
        input_stream=StringIO(),
        output_stream=output_stream,
        prompt_session=type(
            "FakePromptSession",
            (),
            {"prompt": lambda self, prompt: prompts.append(prompt) or "hello"},
        )(),
    )

    assert line == "hello"
    assert prompts == ["[user] "]
    assert output_stream.getvalue() == ""


def test_read_chat_line_wraps_colored_interactive_prompt_as_ansi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    prompts: list[object] = []

    class FakeAnsi:
        def __init__(self, value: str) -> None:
            self.value = value

    def fake_import(name: str) -> object:
        if name == "prompt_toolkit.formatted_text":
            return type("FakeFormattedTextModule", (), {"ANSI": FakeAnsi})()
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(voice_cli, "import_module", fake_import)

    output_stream = TtyStringIO()
    line = voice_cli._read_chat_line(
        input_stream=StringIO(),
        output_stream=output_stream,
        prompt_session=type(
            "FakePromptSession",
            (),
            {"prompt": lambda self, prompt: prompts.append(prompt) or "hello"},
        )(),
    )

    assert line == "hello"
    assert len(prompts) == 1
    assert isinstance(prompts[0], FakeAnsi)
    assert prompts[0].value == "\033[34m[user]\033[0m "
    assert output_stream.getvalue() == ""


def test_read_chat_line_uses_plain_readline_when_not_interactive() -> None:
    input_stream = StringIO("hello\n")
    output_stream = StringIO()

    line = voice_cli._read_chat_line(
        input_stream=input_stream,
        output_stream=output_stream,
        prompt_session=None,
    )

    assert line == "hello\n"
    assert output_stream.getvalue() == "[user] "


def test_run_chat_loop_continues_after_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("bad request\ngood request\n")

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream
            self.calls = 0

        def send_user_message(self, content: str) -> dict[str, str]:
            self.calls += 1
            if content == "bad request":
                raise RuntimeError("502 upstream")
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("good reply", None)

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert "[idle] request failed, staying in chat mode: 502 upstream\n" in output_stream.getvalue()
    assert "[assistant] good reply\n" in output_stream.getvalue()


def test_run_chat_loop_summarizes_stream_errors_already_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("bad request\n")

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> dict[str, str]:
            assert content == "bad request"
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            raise MinigentAPIError(
                "Minigent server error (502). upstream exploded",
                category="server_error",
                detail="run.error event: status_code=502 detail=upstream exploded",
                status_code=502,
            )

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent", stream_runs=True)
    )

    assert exit_code == 0
    output = output_stream.getvalue()
    assert "[idle] request failed, staying in chat mode: stream request failed (502)\n" in output
    assert "upstream exploded" not in output


def test_minigent_client_runtime_summarizes_stream_errors_already_rendered() -> None:
    class FailingMinigentClient(FakeMinigentClient):
        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            raise MinigentAPIError(
                "Minigent server error (502). upstream exploded",
                category="server_error",
                detail="run.error event: status_code=502 detail=upstream exploded",
                status_code=502,
            )

    activation_source = FakeActivationSource(Activation(), utterance="okay")
    minigent_client = FailingMinigentClient(reply="")
    speech_output = FakeSpeechOutput()
    output_stream = StringIO()
    daemon = MinigentClientRuntime(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        output_stream=output_stream,
    )

    reply = daemon.run_once()

    assert reply == ""
    output = output_stream.getvalue()
    assert (
        "[idle] request failed, returning to wake-word mode: stream request failed (502)\n"
        in output
    )
    assert "upstream exploded" not in output


def test_run_chat_loop_remembers_thread_after_successful_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("remember this\n")

    class FakeChatClient:
        thread_id = "thread-remembered"

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> dict[str, str]:
            assert content == "remember this"
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("reply", None)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"),
        once=True,
    )

    key = state_scope_key(
        "http://127.0.0.1:8000",
        api_token=None,
        user_id="demo-user",
        tenant_id="demo-tenant",
        is_admin=False,
    )
    state = PersistentClientState.load()
    assert exit_code == 0
    assert state.get_last_thread(key) == "thread-remembered"
    assert state.list_threads(key)[0].title == "Remember this"


def test_build_config_resolves_resume_last(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    key = state_scope_key(
        "http://127.0.0.1:8000",
        api_token=None,
        user_id="demo-user",
        tenant_id="demo-tenant",
        is_admin=False,
    )
    state = PersistentClientState.load()
    state.set_last_thread(key, "thread-last")
    state.save()

    args = build_parser().parse_args(["--resume-last"])
    config = build_config(args)

    assert config.thread_id == "thread-last"


def test_run_chat_loop_resume_last_forgets_missing_thread_and_reports_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("hello again\n")
    send_thread_ids: list[str | None] = []

    key = state_scope_key(
        "http://127.0.0.1:8000",
        api_token=None,
        user_id="demo-user",
        tenant_id="demo-tenant",
        is_admin=False,
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    state = PersistentClientState.load()
    state.set_last_thread(key, "thread-missing")
    state.save()

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del output_stream
            self.thread_id = config.thread_id

        def create_thread(self, *, skill_name=None, skills=None, capability_profile=None):
            raise AssertionError("missing resumed threads must not be silently replaced")

        def set_thread_id(self, thread_id: str | None) -> None:
            self.thread_id = thread_id

        def send_user_message(self, content: str) -> dict[str, str]:
            assert content == "hello again"
            send_thread_ids.append(self.thread_id)
            raise MinigentAPIError(
                "Minigent resource not found. Thread 'thread-missing' not found",
                category="not_found",
                status_code=404,
            )

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            raise AssertionError("the thread must not run after a missing resumed thread")

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            thread_id="thread-missing",
            resume_last=True,
        ),
        once=True,
    )

    state = PersistentClientState.load()
    assert exit_code == 0
    assert send_thread_ids == ["thread-missing"]
    assert state.get_last_thread(key) is None
    assert state.list_threads(key) == []
    assert (
        "[idle] request failed, staying in chat mode: Remembered thread 'thread-missing' "
        "was not found. The saved resume target was forgotten; start a new thread explicitly "
        "with /new.\n"
    ) in output_stream.getvalue()


def test_run_chat_loop_honors_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("first question\nsecond question\n")
    messages: list[str] = []

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> dict[str, str]:
            messages.append(content)
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("first reply", None)

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"),
        once=True,
    )

    assert exit_code == 0
    assert messages == ["first question"]
    assert output_stream.getvalue() == "[user] [assistant] first reply\n"


def test_run_chat_loop_ignores_blank_interactive_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    prompts: list[str] = []
    messages: list[str] = []

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> dict[str, str]:
            messages.append(content)
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("reply", None)

    responses = iter(["", "real question"])

    class FakePromptSession:
        def prompt(self, prompt: str) -> str:
            prompts.append(prompt)
            try:
                return next(responses)
            except StopIteration as exc:
                raise EOFError from exc

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(
        voice_cli,
        "_build_chat_prompt_session",
        lambda **kwargs: FakePromptSession(),
    )
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert messages == ["real question"]
    assert prompts == ["[user] ", "[user] ", "[user] "]
    assert output_stream.getvalue() == "[assistant] reply\n[idle] shutting down\n"


def test_run_chat_loop_rebuilds_prompt_history_after_thread_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_stream = StringIO()
    sessions: list[object] = []
    history_thread_ids: list[str | None] = []
    responses = iter(["/switch existing-thread", "hello"])
    messages: list[tuple[str | None, str]] = []

    class FakePromptSession:
        def __init__(self, history_thread_id: str | None) -> None:
            self.history_thread_id = history_thread_id
            sessions.append(self)

        def prompt(self, prompt: str) -> str:
            del prompt
            try:
                return next(responses)
            except StopIteration as exc:
                raise EOFError from exc

    class FakeChatClient:
        thread_id: str | None = None

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def get_thread(self, thread_id: str) -> dict[str, object]:
            return {"thread_id": thread_id, "messages": []}

        def set_thread_id(self, thread_id: str | None) -> None:
            self.thread_id = thread_id

        def send_user_message(self, content: str) -> dict[str, str]:
            messages.append((self.thread_id, content))
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("reply", None)

    def fake_build_chat_prompt_session(**kwargs: object) -> FakePromptSession:
        history_thread_id = kwargs.get("history_thread_id")
        assert history_thread_id is None or isinstance(history_thread_id, str)
        history_thread_ids.append(history_thread_id)
        return FakePromptSession(history_thread_id)

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli, "_build_chat_prompt_session", fake_build_chat_prompt_session)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert history_thread_ids[:2] == [None, "existing-thread"]
    assert messages == [("existing-thread", "hello")]
    assert Path.home() == tmp_path / "home"
    assert (tmp_path / "home" / ".local" / "state" / "minigent" / "cli-state.json").exists()


def test_run_chat_loop_handles_local_chat_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/help\n/exit\n")

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> dict[str, str]:
            raise AssertionError(f"local chat command should not be sent: {content}")

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert output_stream.getvalue() == (
        "[user] [idle] chat commands: /help, /new, /agent [current|preset], "
        "/llm [current|profile], /options, /skills, /profiles, /threads, /switch <id>, "
        "/rename <title>, /copy-id, /cancel, "
        "/compact, /actions, /discard-action <consent-id>, /export [markdown|json], /tokens, "
        "/debug, /editor, /image <path...>|paste|list|clear, /commands, "
        "/command set|show|delete, "
        "/exit, /quit. Default: Enter submits; Esc+Enter or Ctrl+J inserts a newline. "
        "Set MINIGENT_CLIENT_CHAT_SUBMIT_MODE=alt-enter to make Esc+Enter submit.\n"
        "[user] [idle] shutting down\n"
    )


def test_run_chat_loop_handles_llm_profile_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/llm\n/llm backup\n/llm current\n/exit\n")
    create_calls: list[str | None] = []

    class FakeChatClient:
        thread_id: str | None = None

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def execution_options(self) -> dict[str, object]:
            return {
                "llm_profiles": {
                    "default": "primary",
                    "items": [{"name": "primary"}, {"name": "backup"}],
                }
            }

        def create_thread(
            self,
            *,
            skill_name=None,
            skills=None,
            capability_profile=None,
            llm_profile=None,
        ) -> dict[str, str]:
            del skill_name, skills, capability_profile
            create_calls.append(llm_profile)
            self.thread_id = "thread-backup"
            return {"thread_id": "thread-backup"}

        def send_user_message(self, content: str) -> dict[str, str]:
            raise AssertionError(f"local chat command should not be sent: {content}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    assert (
        run_chat_loop(ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"))
        == 0
    )
    assert create_calls == ["backup"]
    output = output_stream.getvalue()
    assert "available LLM profiles: primary, backup" in output
    assert "switched to LLM profile backup; created thread thread-backup" in output
    assert "current LLM profile: backup" in output


def test_run_chat_loop_handles_agent_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/agent\n/agent coding-inspect\n/new\n/agent current\n/exit\n")
    create_calls: list[dict[str, object]] = []

    class FakeChatClient:
        thread_id: str | None = None

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def create_thread(
            self,
            *,
            skill_name: str | None = None,
            skills: list[str] | None = None,
            capability_profile: str | None = None,
        ) -> dict[str, str]:
            create_calls.append(
                {
                    "skill_name": skill_name,
                    "skills": skills,
                    "capability_profile": capability_profile,
                }
            )
            self.thread_id = "thread-agent"
            return {"thread_id": "thread-agent"}

        def send_user_message(self, content: str) -> dict[str, str]:
            raise AssertionError(f"local chat command should not be sent: {content}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            agent_presets=(
                AgentPreset(
                    name="coding-inspect",
                    skills=("coding-workspace",),
                    capability_profile="inspect",
                    description="Read-only coding agent",
                ),
            ),
        )
    )

    assert exit_code == 0
    assert create_calls == [
        {
            "skill_name": None,
            "skills": ["coding-workspace"],
            "capability_profile": "inspect",
        },
        {
            "skill_name": None,
            "skills": ["coding-workspace"],
            "capability_profile": "inspect",
        },
    ]
    output = output_stream.getvalue()
    assert "[idle] available agents:\n" in output
    assert "[idle] - coding-inspect  skills=coding-workspace profile=inspect" in output
    assert "[idle] switched to agent coding-inspect; created thread thread-agent\n" in output
    assert "[idle] created thread thread-agent with agent coding-inspect\n" in output
    assert "[idle] current agent: coding-inspect\n" in output
    assert "[idle] current thread: thread-agent\n" in output


def test_run_chat_loop_handles_server_agent_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/agent\n/agent plain-qa\n/exit\n")
    create_calls: list[dict[str, object]] = []

    class FakeChatClient:
        thread_id: str | None = None

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def execution_options(self) -> dict[str, object]:
            return {
                "agents": {
                    "items": [
                        {
                            "name": "plain-qa",
                            "skill_name": "plain-qa",
                            "capability_profile": "plain-qa",
                            "description": "Plain Q&A",
                        }
                    ]
                }
            }

        def create_thread(
            self,
            *,
            skill_name: str | None = None,
            skills: list[str] | None = None,
            capability_profile: str | None = None,
        ) -> dict[str, str]:
            create_calls.append(
                {
                    "skill_name": skill_name,
                    "skills": skills,
                    "capability_profile": capability_profile,
                }
            )
            self.thread_id = "thread-plain"
            return {"thread_id": "thread-plain"}

        def send_user_message(self, content: str) -> dict[str, str]:
            raise AssertionError(f"local chat command should not be sent: {content}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert create_calls == [
        {"skill_name": "plain-qa", "skills": None, "capability_profile": "plain-qa"}
    ]
    output = output_stream.getvalue()
    assert "[idle] - plain-qa  skill=plain-qa profile=plain-qa - Plain Q&A" in output
    assert "[idle] switched to agent plain-qa; created thread thread-plain\n" in output


def test_run_chat_loop_handles_cancel_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/cancel\n/exit\n")
    calls: list[str] = []

    class FakeChatClient:
        thread_id = "thread-running"

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def cancel_current_run(self, thread_id: str | None = None) -> dict[str, object]:
            calls.append(thread_id or "")
            return {"cancelled": False, "thread_id": "thread-running"}

        def send_user_message(self, content: str) -> dict[str, str]:
            raise AssertionError(f"local chat command should not be sent: {content}")

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 0
    assert calls == ["thread-running"]
    assert "[idle] cleared run state for thread-running\n" in output_stream.getvalue()


def test_thread_history_selection_resolves_number_id_and_unique_search() -> None:
    threads = [
        voice_cli.ThreadHistoryItem(
            thread_id="thread-a",
            title="Austin weather",
            updated_at="2026-05-20T01:00:00Z",
            message_count=4,
        ),
        voice_cli.ThreadHistoryItem(
            thread_id="thread-b",
            title="NBA status",
            updated_at="2026-05-20T02:00:00Z",
            message_count=2,
        ),
    ]

    assert voice_cli._resolve_thread_selection("2", threads) == "thread-b"
    assert voice_cli._resolve_thread_selection("thread-a", threads) == "thread-a"
    assert voice_cli._resolve_thread_selection("/threads thread-a", threads) == "thread-a"
    assert voice_cli._resolve_thread_selection("nba", threads) == "thread-b"
    assert voice_cli._resolve_thread_selection("missing", threads) is None


def test_run_chat_loop_handles_thread_shell_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO(
        "/new\n/threads\n/rename renamed thread\n/copy-id\n/export\n/threads existing-thread\n/debug\n/exit\n"
    )

    class FakeChatClient:
        thread_id: str | None = None

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def create_thread(self, **kwargs: object) -> dict[str, str]:
            del kwargs
            self.thread_id = "new-thread"
            return {"thread_id": "new-thread"}

        def get_thread(self, thread_id: str) -> dict[str, object]:
            return {
                "thread_id": thread_id,
                "messages": [
                    {"role": "user", "content": f"question for {thread_id}"},
                    {"role": "assistant", "content": "answer"},
                ],
            }

        def set_thread_id(self, thread_id: str | None) -> None:
            self.thread_id = thread_id

        def set_debug_enabled(self, enabled: bool) -> None:
            self.debug_enabled = enabled

        def send_user_message(self, content: str) -> dict[str, str]:
            raise AssertionError(f"local chat command should not be sent: {content}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(voice_cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    output = output_stream.getvalue()
    assert exit_code == 0
    assert "[idle] created thread new-thread\n" in output
    assert "[idle] * new-thread  Question for new-thread" in output
    assert '[idle] renamed new-thread to "renamed thread"\n' in output
    assert "[idle] new-thread (clipboard unavailable)\n" in output
    assert "# Minigent transcript\n\nThread: `new-thread`" in output
    assert "[idle] switched to existing-thread\n" in output
    assert "[idle] debug on\n" in output


def test_run_chat_loop_handles_editor_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/editor\n")
    messages: list[str] = []

    class FakeChatClient:
        thread_id = "thread-1"

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> dict[str, str]:
            messages.append(content)
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("reply", None)

    def fake_run(argv: list[str], check: bool = False) -> object:
        del check
        Path(argv[-1]).write_text("first line\n# ignored\nsecond line\n", encoding="utf-8")
        return object()

    monkeypatch.setenv("EDITOR", "fake-editor --wait")
    monkeypatch.setattr(voice_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"),
        once=True,
    )

    assert exit_code == 0
    assert messages == ["first line\nsecond line"]
    assert output_stream.getvalue() == "[user] [assistant] reply\n"


def test_run_chat_loop_handles_editor_atomic_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("/editor\n")
    messages: list[str] = []

    class FakeChatClient:
        thread_id = "thread-1"

        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> dict[str, str]:
            messages.append(content)
            return {"id": "message-1"}

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            return ("reply", None)

    def fake_run(argv: list[str], check: bool = False) -> object:
        del check
        prompt_path = Path(argv[-1])
        replacement_path = prompt_path.with_suffix(".md.tmp")
        replacement_path.write_text("saved through rename\n", encoding="utf-8")
        replacement_path.replace(prompt_path)
        return object()

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr(voice_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"),
        once=True,
    )

    assert exit_code == 0
    assert messages == ["saved through rename"]
    assert output_stream.getvalue() == "[user] [assistant] reply\n"


def test_run_chat_loop_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()

    class InterruptingInput:
        def readline(self) -> str:
            raise KeyboardInterrupt

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", InterruptingInput())
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
    )

    assert exit_code == 130
    assert output_stream.getvalue() == "[user] \n[idle] shutting down\n"


def test_run_chat_loop_interrupt_during_run_aborts_turn_and_stays_in_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_stream = StringIO()
    input_stream = StringIO("hello\n")
    calls: list[str] = []

    class FakeChatClient:
        def __init__(self, config: ClientConfig, output_stream=None) -> None:
            del config, output_stream

        def send_user_message(self, content: str) -> None:
            calls.append(content)

        def run_thread(self) -> tuple[str, dict[str, object] | None]:
            raise KeyboardInterrupt

    monkeypatch.setattr(voice_cli, "MinigentAPIClient", FakeChatClient)
    monkeypatch.setattr(voice_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(voice_cli.sys, "stdout", output_stream)

    exit_code = run_chat_loop(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            stream_runs=True,
        )
    )

    assert exit_code == 0
    assert calls == ["hello"]
    output = output_stream.getvalue()
    assert "locally aborted current run" in output
    assert "server cancellation requested" in output
    assert output.endswith("[user] [idle] shutting down\n")


def test_minigent_client_cli_routes_chat_subcommand_without_minigent_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(voice_cli, "load_environment", lambda: None)

    def fake_run_chat_loop(config: ClientConfig, *, once: bool = False) -> int:
        calls.append(("chat", config))
        calls.append(("once", once))
        return 7

    monkeypatch.setattr(voice_cli, "run_chat_loop", fake_run_chat_loop)
    monkeypatch.setattr(
        voice_cli,
        "MinigentClientRuntime",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("MinigentClientRuntime should not be built for chat")
        ),
    )

    exit_code = voice_cli.main(["chat", "--once"])

    assert exit_code == 7
    assert calls[0][0] == "chat"
    assert isinstance(calls[0][1], ClientConfig)
    assert calls[1] == ("once", True)


def test_minigent_client_cli_routes_chat_backend_without_minigent_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(voice_cli, "load_environment", lambda: None)

    def fake_run_chat_loop(config: ClientConfig, *, once: bool = False) -> int:
        calls.append(("chat", config))
        calls.append(("once", once))
        return 7

    monkeypatch.setattr(voice_cli, "run_chat_loop", fake_run_chat_loop)
    monkeypatch.setattr(
        voice_cli,
        "MinigentClientRuntime",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("MinigentClientRuntime should not be built for chat")
        ),
    )

    exit_code = voice_cli.main(["--backend", "chat", "--once"])

    assert exit_code == 7
    assert calls[0][0] == "chat"
    assert isinstance(calls[0][1], ClientConfig)
    assert calls[1] == ("once", True)


def test_minigent_client_cli_delegates_one_shot_commands_to_one_shot_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(voice_cli, "load_environment", lambda: None)

    import minigent_client.one_shot_cli as one_shot_cli

    def fake_one_shot_main(argv: list[str]) -> int:
        calls.append(argv)
        return 9

    monkeypatch.setattr(one_shot_cli, "main", fake_one_shot_main)

    thread_exit_code = voice_cli.main(["--base-url", "http://example.test", "threads", "create"])
    resume_exit_code = voice_cli.main(["resume"])
    run_exit_code = voice_cli.main(["run", "hello"])
    options_exit_code = voice_cli.main(["options"])
    skills_exit_code = voice_cli.main(["skills"])
    capabilities_exit_code = voice_cli.main(["capabilities"])
    admin_exit_code = voice_cli.main(
        [
            "--admin",
            "admin",
            "threads",
            "list",
            "--tenant",
            "tenant-a",
        ]
    )

    assert thread_exit_code == 9
    assert resume_exit_code == 9
    assert run_exit_code == 9
    assert options_exit_code == 9
    assert skills_exit_code == 9
    assert capabilities_exit_code == 9
    assert admin_exit_code == 9
    assert calls == [
        ["--base-url", "http://example.test", "threads", "create"],
        ["resume"],
        ["run", "hello"],
        ["options"],
        ["skills"],
        ["capabilities"],
        ["--admin", "admin", "threads", "list", "--tenant", "tenant-a"],
    ]


def test_audio_ring_buffer_keeps_recent_audio_only() -> None:
    buffer = AudioRingBuffer(max_bytes=6)

    buffer.append(b"12")
    buffer.append(b"345")
    buffer.append(b"67")

    assert buffer.snapshot() == [b"345", b"67"]


def test_split_pcm_chunk_uses_detector_frame_size() -> None:
    chunk = b"\x00\x00\x01\x00\x02\x00\x03\x00\x04\x00\x05\x00"

    frames = split_pcm_chunk(chunk, 2)

    assert frames == [b"\x00\x00\x01\x00", b"\x02\x00\x03\x00", b"\x04\x00\x05\x00"]


def test_openai_transcription_adapter_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"text": " transcribed text "}

    def fake_post(url: str, *, files: object, headers: object, timeout: object) -> FakeResponse:
        captured["url"] = url
        captured["files"] = files
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("minigent_client.stt.httpx.post", fake_post)
    adapter = OpenAITranscriptionAdapter(
        OpenAITranscriptionConfig(
            api_key="openai-key",
            model="gpt-4o-mini-transcribe",
            base_url="https://api.openai.com/v1",
            timeout_seconds=30.0,
        )
    )

    text = adapter.transcribe(
        RecordedAudio(
            pcm_bytes=b"\x00\x00\xff\x7f",
            sample_rate=16_000,
            channels=1,
            sample_width_bytes=2,
        )
    )

    assert text == "transcribed text"
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["headers"] == {"Authorization": "Bearer openai-key"}
    assert captured["timeout"] == 30.0


def test_openai_transcription_adapter_requires_text_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"unexpected": "payload"}

    monkeypatch.setattr("minigent_client.stt.httpx.post", lambda *_, **__: FakeResponse())
    adapter = OpenAITranscriptionAdapter(OpenAITranscriptionConfig(api_key="openai-key"))

    with pytest.raises(SpeechToTextError, match="did not include text"):
        adapter.transcribe(
            RecordedAudio(
                pcm_bytes=b"\x00\x00",
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )
        )


def test_openrouter_transcription_adapter_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        text = ""
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": '{"transcript":"hello from openrouter"}'}}]}

    def fake_post(url: str, *, json: object, headers: object, timeout: object) -> FakeResponse:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("minigent_client.stt.httpx.post", fake_post)
    adapter = OpenRouterTranscriptionAdapter(
        OpenRouterTranscriptionConfig(
            api_key="openrouter-key",
            model="openai/gpt-audio",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=45.0,
            app_name="minigent",
            http_referer="https://minigent.example",
        )
    )

    text = adapter.transcribe(
        RecordedAudio(
            pcm_bytes=b"\x00\x00\xff\x7f",
            sample_rate=16_000,
            channels=1,
            sample_width_bytes=2,
        )
    )

    assert text == "hello from openrouter"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["timeout"] == 45.0
    assert captured["headers"] == {
        "Authorization": "Bearer openrouter-key",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://minigent.example",
        "X-Title": "minigent",
    }
    payload = captured["json"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert "response_format" not in payload
    assert messages[0]["role"] == "system"
    assert "verbatim transcript text" in messages[0]["content"]
    assert messages[1]["content"][1]["input_audio"]["format"] == "wav"


def test_openrouter_transcription_adapter_rejects_assistant_style_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        text = ""
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Please provide the audio file and I will transcribe it."
                        }
                    }
                ]
            }

    monkeypatch.setattr("minigent_client.stt.httpx.post", lambda *_, **__: FakeResponse())
    adapter = OpenRouterTranscriptionAdapter(
        OpenRouterTranscriptionConfig(
            api_key="openrouter-key",
            model="openai/gpt-audio",
        )
    )

    with pytest.raises(SpeechToTextError, match="assistant-style text"):
        adapter.transcribe(
            RecordedAudio(
                pcm_bytes=b"\x00\x00\xff\x7f",
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )
        )


def test_openrouter_transcription_adapter_rejects_missing_attachment_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        text = ""
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "I currently don't have an audio attached. Could you please try "
                                "uploading it again?"
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr("minigent_client.stt.httpx.post", lambda *_, **__: FakeResponse())
    adapter = OpenRouterTranscriptionAdapter(
        OpenRouterTranscriptionConfig(
            api_key="openrouter-key",
            model="openai/gpt-audio",
        )
    )

    with pytest.raises(SpeechToTextError, match="assistant-style text"):
        adapter.transcribe(
            RecordedAudio(
                pcm_bytes=b"\x00\x00\xff\x7f",
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )
        )


def test_faster_whisper_transcription_adapter_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSegment:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeModel:
        def __init__(self, model: str, *, device: str, compute_type: str) -> None:
            self.model = model
            self.device = device
            self.compute_type = compute_type

        def transcribe(self, audio_samples, **kwargs):
            assert len(audio_samples) == 2
            assert kwargs["task"] == "transcribe"
            assert kwargs["language"] == "en"
            return iter([FakeSegment("hello "), FakeSegment("world")]), object()

    monkeypatch.setattr("minigent_client.stt._load_faster_whisper_model", FakeModel)
    adapter = FasterWhisperTranscriptionAdapter(
        FasterWhisperTranscriptionConfig(
            model="base",
            device="cpu",
            compute_type="int8",
            language="en",
        )
    )

    text = adapter.transcribe(
        RecordedAudio(
            pcm_bytes=b"\x00\x00\xff\x7f",
            sample_rate=16_000,
            channels=1,
            sample_width_bytes=2,
        )
    )

    assert text == "hello world"


def test_build_transcription_adapter_supports_all_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "minigent_client.stt._load_faster_whisper_model",
        lambda model, *, device, compute_type: object(),
    )
    assert isinstance(
        build_transcription_adapter(
            build_speech_provider_config(
                ClientConfig(
                    base_url="http://127.0.0.1:8000",
                    wake_phrase="hey minigent",
                    stt_provider="openai",
                    openai_api_key="openai-key",
                )
            )
        ),
        OpenAITranscriptionAdapter,
    )
    assert isinstance(
        build_transcription_adapter(
            build_speech_provider_config(
                ClientConfig(
                    base_url="http://127.0.0.1:8000",
                    wake_phrase="hey minigent",
                    stt_provider="openrouter",
                    stt_model="openai/gpt-audio",
                    openrouter_api_key="openrouter-key",
                )
            )
        ),
        OpenRouterTranscriptionAdapter,
    )
    assert isinstance(
        build_transcription_adapter(
            build_speech_provider_config(
                ClientConfig(
                    base_url="http://127.0.0.1:8000",
                    wake_phrase="hey minigent",
                    stt_provider="faster-whisper",
                    stt_model="base",
                    stt_device="cpu",
                    stt_compute_type="int8",
                    stt_language="en",
                )
            )
        ),
        FasterWhisperTranscriptionAdapter,
    )


def test_minigent_client_config_defaults_openrouter_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIGENT_VOICE_STT_MODEL", raising=False)
    monkeypatch.setenv("MINIGENT_VOICE_STT_PROVIDER", "openrouter")

    config = ClientConfig.from_env()

    assert config.stt_model == "openai/gpt-audio"


def test_minigent_client_config_defaults_faster_whisper_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINIGENT_VOICE_STT_MODEL", raising=False)
    monkeypatch.setenv("MINIGENT_VOICE_STT_PROVIDER", "faster-whisper")

    config = ClientConfig.from_env()

    assert config.stt_model == "base"


def test_build_wake_word_detector_requires_access_key_and_keyword_path() -> None:
    with pytest.raises(SystemExit, match="PICOVOICE_ACCESS_KEY is required"):
        build_wake_word_detector(
            ClientConfig(
                base_url="http://127.0.0.1:8000",
                wake_phrase="hey minigent",
            )
        )


def test_build_wake_word_detector_supports_openwakeword() -> None:
    class FakeOpenWakeWordDetector:
        def __init__(self, model_name: str, threshold: float) -> None:
            self.model_name = model_name
            self.threshold = threshold

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("minigent_client.cli.OpenWakeWordDetector", FakeOpenWakeWordDetector)
    detector = build_wake_word_detector(
        ClientConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            wakeword_provider="openwakeword",
            openwakeword_model="okay_nabu",
            openwakeword_threshold=0.7,
        )
    )
    monkeypatch.undo()

    assert isinstance(detector, FakeOpenWakeWordDetector)
    assert detector.model_name == "okay_nabu"
    assert detector.threshold == 0.7


def test_coding_mcp_timeout_checks_warn_when_runtime_timeout_is_short() -> None:
    from minigent_client.config_commands import _coding_mcp_timeout_checks

    checks = _coding_mcp_timeout_checks(
        {
            "mcp_server_specs": [
                {
                    "name": "shell-workspace",
                    "request_timeout": 180,
                    "timeout_seconds": 180,
                    "restart_on_timeout": True,
                }
            ]
        },
        {"MINIGENT_TOOL_TIMEOUT_SECONDS": "60"},
    )

    assert any(
        check.status == "warning"
        and check.label == "Runtime/MCP timeout alignment"
        and "shell-workspace=180s" in (check.detail or "")
        for check in checks
    )
    assert any(
        check.status == "ok"
        and check.label == "MCP stdio timeout recovery"
        and "shell-workspace" in (check.detail or "")
        for check in checks
    )


def test_coding_mcp_timeout_checks_ok_when_runtime_timeout_covers_mcp() -> None:
    from minigent_client.config_commands import _coding_mcp_timeout_checks

    checks = _coding_mcp_timeout_checks(
        {"mcp_server_specs": [{"name": "shell-workspace", "request_timeout": 180}]},
        {"MINIGENT_TOOL_TIMEOUT_SECONDS": "240"},
    )

    assert any(
        check.status == "ok" and check.label == "Runtime/MCP timeout alignment" for check in checks
    )
    assert not any(check.status == "warning" for check in checks)
