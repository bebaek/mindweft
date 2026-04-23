from __future__ import annotations

import argparse
import json
import subprocess
import wave
from io import StringIO
from pathlib import Path

import pytest

import voice_daemon.cli as voice_cli
from voice_daemon.audio import (
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
from voice_daemon.backends.manual_audio import ManualAudioActivationSource
from voice_daemon.backends.passive_audio import PassiveAudioActivationSource
from voice_daemon.backends.stdin_loop import StdinActivationSource
from voice_daemon.cli import (
    bounded_output_volume,
    build_ambient_volume_controller,
    build_config,
    build_parser,
    build_speech_output,
    build_speech_provider_config,
    build_wake_word_detector,
)
from voice_daemon.config import PrincipalConfig, VoiceDaemonConfig
from voice_daemon.debug import CaptureDebugConfig, CaptureDebugger
from voice_daemon.ducking import MacOsAmbientVolumeDucker, should_duck_for_state
from voice_daemon.minigent_client import MinigentClient
from voice_daemon.ring_buffer import AudioRingBuffer
from voice_daemon.service import Activation, DaemonState, VoiceDaemon
from voice_daemon.speech import (
    ConsoleSpeechOutput,
    MacOsSaySpeechOutput,
    PiperSpeechOutput,
    _sanitize_text_for_tts,
)
from voice_daemon.stt import (
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

    def run_thread(self) -> str:
        return self.reply


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
        self.states: list[DaemonState] = []
        self.close_calls = 0

    def sync_state(self, state: DaemonState) -> None:
        self.states.append(state)

    def close(self) -> None:
        self.close_calls += 1


def test_voice_daemon_uses_transcript_hint_without_capture() -> None:
    activation_source = FakeActivationSource(Activation(transcript_hint="what time is it"))
    minigent_client = FakeMinigentClient(reply="Tool result: 3:00 PM")
    speech_output = FakeSpeechOutput()
    daemon = VoiceDaemon(
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


def test_voice_daemon_captures_utterance_after_activation() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="continue")
    minigent_client = FakeMinigentClient(reply="continued")
    speech_output = FakeSpeechOutput()
    daemon = VoiceDaemon(
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


def test_voice_daemon_emits_activation_feedback_before_capture() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="continue")
    minigent_client = FakeMinigentClient(reply="continued")
    speech_output = FakeSpeechOutput()
    feedback_calls: list[str] = []
    daemon = VoiceDaemon(
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


def test_voice_daemon_skips_activation_feedback_for_transcript_hint() -> None:
    activation_source = FakeActivationSource(Activation(transcript_hint="continue"))
    minigent_client = FakeMinigentClient(reply="continued")
    speech_output = FakeSpeechOutput()
    feedback_calls: list[str] = []
    daemon = VoiceDaemon(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        activation_feedback=lambda: feedback_calls.append("ack"),
    )

    reply = daemon.run_once()

    assert reply == "continued"
    assert feedback_calls == []


def test_voice_daemon_supports_barge_in() -> None:
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
    daemon = VoiceDaemon(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
    )
    replies = iter(["first reply", "second reply"])
    minigent_client.run_thread = lambda: next(replies)

    reply = daemon.run_once()

    assert reply == "second reply"
    assert minigent_client.messages == ["first request", "second request"]
    assert speech_output.started == ["first reply", "second reply"]
    assert speech_output.stops == 1
    assert speech_output.waits == 2


def test_voice_daemon_uses_follow_up_window_without_wake_word() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="first request")
    activation_source.follow_up_utterance = "second request"
    minigent_client = FakeMinigentClient(reply="first reply")
    speech_output = FakeSpeechOutput()
    daemon = VoiceDaemon(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        follow_up_timeout_ms=6000,
    )
    replies = iter(["first reply", "second reply"])
    minigent_client.run_thread = lambda: next(replies)

    reply = daemon.run_once()

    assert reply == "second reply"
    assert activation_source.wait_calls == ["hey minigent"]
    assert activation_source.follow_up_timeout_ms == [6000, 6000]
    assert minigent_client.messages == ["first request", "second request"]


def test_voice_daemon_returns_to_idle_when_follow_up_window_expires() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="first request")
    activation_source.follow_up_utterance = None
    minigent_client = FakeMinigentClient(reply="first reply")
    speech_output = FakeSpeechOutput()
    daemon = VoiceDaemon(
        wake_phrase="hey minigent",
        activation_source=activation_source,
        minigent_client=minigent_client,
        speech_output=speech_output,
        follow_up_timeout_ms=4000,
    )

    reply = daemon.run_once()

    assert reply == "first reply"
    assert daemon.state == DaemonState.IDLE
    assert activation_source.follow_up_timeout_ms == [4000]


def test_voice_daemon_updates_ambient_volume_for_listening_states() -> None:
    activation_source = FakeActivationSource(Activation(), utterance="first request")
    activation_source.follow_up_utterance = None
    minigent_client = FakeMinigentClient(reply="first reply")
    speech_output = FakeSpeechOutput()
    ambient_volume = FakeAmbientVolumeController()
    daemon = VoiceDaemon(
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
        DaemonState.IDLE,
        DaemonState.LISTENING,
        DaemonState.THINKING,
        DaemonState.SPEAKING,
        DaemonState.FOLLOW_UP_LISTENING,
        DaemonState.IDLE,
    ]


def test_voice_daemon_closes_ambient_volume_controller() -> None:
    ambient_volume = FakeAmbientVolumeController()
    daemon = VoiceDaemon(
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

    monkeypatch.setattr("voice_daemon.speech.subprocess.Popen", fake_popen)

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

    monkeypatch.setattr("voice_daemon.speech.subprocess.run", fake_run)
    monkeypatch.setattr("voice_daemon.speech._resolve_piper_executable", lambda: "piper")
    monkeypatch.setattr("voice_daemon.speech._load_sounddevice_for_output", lambda: sounddevice)
    monkeypatch.setattr(
        "voice_daemon.speech._resolve_piper_model_path",
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


def test_build_speech_output_supports_console_say_and_piper() -> None:
    console = build_speech_output(
        VoiceDaemonConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            tts_provider="console",
        )
    )
    say = build_speech_output(
        VoiceDaemonConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            tts_provider="say",
            tts_voice="Samantha",
        )
    )
    piper = build_speech_output(
        VoiceDaemonConfig(
            base_url="http://127.0.0.1:8000",
            wake_phrase="hey minigent",
            tts_provider="piper",
            tts_model="/tmp/en_US-lessac-medium.onnx",
            tts_speaker=5,
            tts_length_scale=1.1,
            tts_sentence_silence=0.5,
        )
    )

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
            VoiceDaemonConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent")
        )
        is None
    )
    monkeypatch.setattr(
        "voice_daemon.cli.MacOsAmbientVolumeDucker.validate_platform", lambda: None
    )

    controller = build_ambient_volume_controller(
        VoiceDaemonConfig(
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
        "voice_daemon.cli.MacOsAmbientVolumeDucker.validate_platform",
        lambda: (_ for _ in ()).throw(RuntimeError("ambient audio ducking is currently supported only on macOS")),
    )

    controller = build_ambient_volume_controller(
        VoiceDaemonConfig(
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
        "voice_daemon.cli._resolve_acknowledgement_sound",
        lambda configured_sound_path: Path("/tmp/glass.aiff"),
    )
    monkeypatch.setattr(
        "voice_daemon.cli._resolve_acknowledgement_players",
        lambda sound_path: [["/usr/bin/afplay"]] if sound_path == Path("/tmp/glass.aiff") else [],
    )
    monkeypatch.setattr("voice_daemon.cli.subprocess.run", fake_run)

    bell = voice_cli.build_activation_feedback(
        VoiceDaemonConfig(
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


def test_build_activation_feedback_falls_back_to_terminal_bell(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        "voice_daemon.cli._resolve_acknowledgement_sound",
        lambda configured_sound_path: None,
    )
    monkeypatch.setattr("voice_daemon.cli._resolve_acknowledgement_players", lambda sound_path: [])

    bell = voice_cli.build_activation_feedback(
        VoiceDaemonConfig(
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
        VoiceDaemonConfig(
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
        "voice_daemon.cli._resolve_acknowledgement_sound",
        lambda configured_sound_path: Path("/tmp/wake.wav"),
    )
    monkeypatch.setattr(
        "voice_daemon.cli._resolve_acknowledgement_players",
        lambda sound_path: [["/usr/bin/paplay"], ["/usr/bin/aplay"]],
    )
    monkeypatch.setattr("voice_daemon.cli.subprocess.run", fake_run)

    bell = voice_cli.build_activation_feedback(
        VoiceDaemonConfig(
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

    monkeypatch.setattr("voice_daemon.cli.threading.Thread", FakeThread)

    bell = voice_cli.build_acknowledgement_feedback(
        "bell",
        "/tmp/wake.aiff",
        FakeSpeechOutput(),
        async_bell=True,
    )
    assert bell is not None
    bell()

    assert started == [(voice_cli._emit_terminal_bell, ("/tmp/wake.aiff",))]


def test_resolve_wake_acknowledgement_sound_prefers_configured_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    sound_path = tmp_path / "wake.wav"
    sound_path.write_bytes(b"sound")
    monkeypatch.setenv("MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT_SOUND", str(sound_path))
    monkeypatch.setattr("voice_daemon.cli._default_wake_acknowledgement_sounds", lambda: [])

    assert voice_cli._resolve_wake_acknowledgement_sound() == sound_path


def test_default_wake_acknowledgement_sounds_supports_macos_and_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("voice_daemon.cli.platform.system", lambda: "Darwin")
    assert voice_cli._default_wake_acknowledgement_sounds() == [
        Path("/System/Library/Sounds/Glass.aiff")
    ]

    monkeypatch.setattr("voice_daemon.cli.platform.system", lambda: "Linux")
    assert voice_cli._default_wake_acknowledgement_sounds() == [
        Path("/usr/share/sounds/alsa/Front_Center.wav"),
        Path("/usr/share/sounds/sound-icons/glass-water-1.wav"),
        Path("/usr/share/sounds/freedesktop/stereo/complete.oga"),
        Path("/usr/share/sounds/freedesktop/stereo/bell.oga"),
    ]


def test_resolve_wake_acknowledgement_player_supports_macos_and_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("voice_daemon.cli.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "voice_daemon.cli.shutil.which",
        lambda name: "/usr/bin/afplay" if name == "afplay" else None,
    )
    assert voice_cli._resolve_wake_acknowledgement_player(Path("/tmp/test.aiff")) == [
        "/usr/bin/afplay"
    ]

    monkeypatch.setattr("voice_daemon.cli.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "voice_daemon.cli.shutil.which",
        lambda name: "/usr/bin/paplay" if name == "paplay" else None,
    )
    assert voice_cli._resolve_wake_acknowledgement_player(Path("/tmp/test.oga")) == [
        "/usr/bin/paplay"
    ]

    monkeypatch.setattr(
        "voice_daemon.cli.shutil.which",
        lambda name: "/usr/bin/aplay" if name == "aplay" else None,
    )
    assert voice_cli._resolve_wake_acknowledgement_player(Path("/tmp/test.oga")) is None
    assert voice_cli._resolve_wake_acknowledgement_player(Path("/tmp/test.wav")) == [
        "/usr/bin/aplay"
    ]

    monkeypatch.setattr(
        "voice_daemon.cli.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"aplay", "paplay"} else None,
    )
    assert voice_cli._resolve_acknowledgement_players(Path("/tmp/test.wav")) == [
        ["/usr/bin/aplay"],
        ["/usr/bin/paplay"],
    ]


def test_sanitize_text_for_tts_strips_common_markdown() -> None:
    text = "# Heading\n- **bold** item with [link](https://example.com) and `code`"

    assert _sanitize_text_for_tts(text) == "Heading. bold item with link and code."


def test_sanitize_text_for_tts_preserves_boundary_around_headers() -> None:
    text = "Before\n## Details\nAfter"

    assert _sanitize_text_for_tts(text) == "Before Details. After"


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

    monkeypatch.setattr("voice_daemon.speech.subprocess.Popen", lambda command, text: FakeProcess())
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

    monkeypatch.setattr("voice_daemon.speech.subprocess.run", fake_run)
    monkeypatch.setattr("voice_daemon.speech._resolve_piper_executable", lambda: "piper")
    monkeypatch.setattr("voice_daemon.speech._load_sounddevice_for_output", lambda: sounddevice)
    monkeypatch.setattr(
        "voice_daemon.speech._resolve_piper_model_path",
        lambda model, model_dir: tmp_path / "voice.onnx",
    )

    speech = PiperSpeechOutput(output_stream=output_stream, model=str(tmp_path / "voice.onnx"))
    speech.start("done")
    assert speech.is_speaking()
    speech.stop()
    speech.wait()

    assert sounddevice.stop_calls == 1
    assert not speech.is_speaking()


def test_piper_speech_output_reports_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    output_stream = StringIO()

    monkeypatch.setattr("voice_daemon.speech.shutil.which", lambda name: None)
    monkeypatch.setattr("voice_daemon.speech.os.environ", {"PATH": "/tmp/does-not-exist"})
    monkeypatch.setattr("voice_daemon.speech.sys.executable", "/tmp/does-not-exist/python")

    speech = PiperSpeechOutput(output_stream=output_stream, model="/tmp/voice.onnx")

    with pytest.raises(RuntimeError, match="not available on PATH"):
        speech.start("done")


def test_resolve_piper_executable_finds_sibling_of_active_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from voice_daemon.speech import _resolve_piper_executable

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python_path = bin_dir / "python"
    python_path.write_text("", encoding="utf-8")
    piper_path = bin_dir / "piper"
    piper_path.write_text("#!/bin/sh\n", encoding="utf-8")
    piper_path.chmod(0o755)

    monkeypatch.setattr("voice_daemon.speech.shutil.which", lambda name: None)
    monkeypatch.setattr("voice_daemon.speech.os.environ", {"PATH": "/tmp/does-not-exist"})
    monkeypatch.setattr("voice_daemon.speech.sys.executable", str(python_path))

    assert _resolve_piper_executable() == str(piper_path)


def test_resolve_piper_model_path_uses_existing_onnx_path(tmp_path) -> None:
    from voice_daemon.speech import _resolve_piper_model_path

    model_path = tmp_path / "voice.onnx"
    model_path.write_bytes(b"model")

    assert _resolve_piper_model_path(str(model_path), None) == model_path


def test_resolve_piper_model_path_downloads_named_voice(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from voice_daemon import speech as speech_module

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


def test_voice_daemon_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_BASE_URL", "http://127.0.0.1:9000/")
    monkeypatch.setenv("MINIGENT_VOICE_WAKE_PHRASE", "computer")
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

    config = VoiceDaemonConfig.from_env()

    assert config == VoiceDaemonConfig(
        base_url="http://127.0.0.1:9000",
        wake_phrase="computer",
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

    client = MinigentClient(
        VoiceDaemonConfig(
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
            "payload": {"content": "what time is it"},
            "headers": {
                "X-minigent-user-id": "user-1",
                "X-minigent-tenant-id": "tenant-1",
                "X-minigent-admin": "false",
                "Content-type": "application/json",
            },
        }
    ]


def test_minigent_client_prepends_location_context_when_configured(
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

    client = MinigentClient(
        VoiceDaemonConfig(
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
                "Context: location=Austin, TX, US; timezone=America/Chicago\n\n"
                "find coffee nearby"
            )
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

    client = MinigentClient(
        VoiceDaemonConfig(
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
                "Context: location=Austin, TX, US; timezone=America/Chicago\n\n"
                "What's my location?"
            )
        }
    ]
    assert output_stream.getvalue() == (
        "[prompt]\n"
        "Context: location=Austin, TX, US; timezone=America/Chicago\n\n"
        "What's my location?\n"
    )


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
    assert should_duck_for_state(DaemonState.LISTENING) is True
    assert should_duck_for_state(DaemonState.FOLLOW_UP_LISTENING) is True
    assert should_duck_for_state(DaemonState.IDLE) is False
    assert should_duck_for_state(DaemonState.THINKING) is False
    assert should_duck_for_state(DaemonState.SPEAKING) is False


def test_macos_ambient_volume_ducker_reads_sets_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr("voice_daemon.ducking.subprocess.run", fake_run)

    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=15, output_stream=output_stream)
    ducker.sync_state(DaemonState.LISTENING)
    ducker.sync_state(DaemonState.FOLLOW_UP_LISTENING)
    ducker.sync_state(DaemonState.THINKING)

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

    monkeypatch.setattr("voice_daemon.ducking.subprocess.run", fake_run)

    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=10, output_stream=output_stream)
    ducker.sync_state(DaemonState.LISTENING)
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

    monkeypatch.setattr("voice_daemon.ducking.subprocess.run", fake_run)
    monkeypatch.setattr("voice_daemon.ducking.threading.Thread", FakeThread)

    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=10, output_stream=output_stream)
    ducker.sync_state(DaemonState.LISTENING)
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

    monkeypatch.setattr("voice_daemon.ducking.subprocess.run", fake_run)

    ducker = MacOsAmbientVolumeDucker(ducked_output_volume=10, output_stream=output_stream)
    ducker.sync_state(DaemonState.LISTENING)
    ducker.sync_state(DaemonState.FOLLOW_UP_LISTENING)
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
            raise SpeechToTextError("STT provider returned assistant-style text instead of a transcript")

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
    assert "[idle] passive listening for wake word openwakeword:okay_nabu" in output_stream.getvalue()
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

    monkeypatch.setattr("voice_daemon.backends.passive_audio.time.sleep", lambda seconds: events.append(f"sleep:{seconds}"))

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

        def record_after_speech(self, timeout_ms: int, *, preroll_ms: int = 250) -> RecordedAudio | None:
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
        def record_after_speech(self, timeout_ms: int, *, preroll_ms: int = 250) -> RecordedAudio | None:
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
    assert "[idle] follow-up window expired, returning to wake-word mode" in output_stream.getvalue()


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
            raise SpeechToTextError("STT provider returned assistant-style text instead of a transcript")

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


def test_voice_daemon_cli_handles_keyboard_interrupt(
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
        lambda args: VoiceDaemonConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey minigent"),
    )
    monkeypatch.setattr(
        voice_cli,
        "build_activation_source",
        lambda backend, config, **kwargs: activation_source,
    )
    monkeypatch.setattr(voice_cli, "MinigentClient", lambda config: object())
    monkeypatch.setattr(voice_cli, "build_speech_output", lambda config: object())
    monkeypatch.setattr(voice_cli, "VoiceDaemon", FakeVoiceDaemon)

    exit_code = voice_cli.main(["--backend", "stdin"])

    assert exit_code == 130
    assert capsys.readouterr().out == "[idle] shutting down\n"
    assert activation_source.close_calls == 1


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

    monkeypatch.setattr("voice_daemon.stt.httpx.post", fake_post)
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


def test_openai_transcription_adapter_requires_text_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"unexpected": "payload"}

    monkeypatch.setattr("voice_daemon.stt.httpx.post", lambda *_, **__: FakeResponse())
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
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"transcript":"hello from openrouter"}'
                        }
                    }
                ]
            }

    def fake_post(url: str, *, json: object, headers: object, timeout: object) -> FakeResponse:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("voice_daemon.stt.httpx.post", fake_post)
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


def test_openrouter_transcription_adapter_rejects_assistant_style_text(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr("voice_daemon.stt.httpx.post", lambda *_, **__: FakeResponse())
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

    monkeypatch.setattr("voice_daemon.stt.httpx.post", lambda *_, **__: FakeResponse())
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

    monkeypatch.setattr("voice_daemon.stt._load_faster_whisper_model", FakeModel)
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


def test_build_transcription_adapter_supports_all_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "voice_daemon.stt._load_faster_whisper_model",
        lambda model, *, device, compute_type: object(),
    )
    assert isinstance(
        build_transcription_adapter(
            build_speech_provider_config(
                VoiceDaemonConfig(
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
                VoiceDaemonConfig(
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
                VoiceDaemonConfig(
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


def test_voice_daemon_config_defaults_openrouter_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIGENT_VOICE_STT_MODEL", raising=False)
    monkeypatch.setenv("MINIGENT_VOICE_STT_PROVIDER", "openrouter")

    config = VoiceDaemonConfig.from_env()

    assert config.stt_model == "openai/gpt-audio"


def test_voice_daemon_config_defaults_faster_whisper_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIGENT_VOICE_STT_MODEL", raising=False)
    monkeypatch.setenv("MINIGENT_VOICE_STT_PROVIDER", "faster-whisper")

    config = VoiceDaemonConfig.from_env()

    assert config.stt_model == "base"


def test_build_wake_word_detector_requires_access_key_and_keyword_path() -> None:
    with pytest.raises(SystemExit, match="PICOVOICE_ACCESS_KEY is required"):
        build_wake_word_detector(
            VoiceDaemonConfig(
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
    monkeypatch.setattr("voice_daemon.cli.OpenWakeWordDetector", FakeOpenWakeWordDetector)
    detector = build_wake_word_detector(
        VoiceDaemonConfig(
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
