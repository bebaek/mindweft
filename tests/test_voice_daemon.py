from __future__ import annotations

from io import StringIO

import pytest

from voice_daemon.audio import AudioCaptureConfig, RecordedAudio, pcm16le_to_floats
from voice_daemon.backends.manual_audio import ManualAudioActivationSource
from voice_daemon.backends.stdin_loop import ConsoleSpeechOutput, StdinActivationSource
from voice_daemon.config import PrincipalConfig, VoiceDaemonConfig
from voice_daemon.stt import OpenAITranscriptionAdapter, OpenAITranscriptionConfig, SpeechToTextError
from voice_daemon.service import Activation, VoiceDaemon


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

    def wait_for_activation(self, wake_phrase: str) -> Activation:
        self.wait_calls.append(wake_phrase)
        return self.activation

    def capture_utterance(self) -> str:
        self.capture_calls += 1
        return self.utterance


class FakeSpeechOutput:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


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
    monkeypatch.setenv("MINIGENT_VOICE_SKILL", "support")
    monkeypatch.setenv("MINIGENT_VOICE_THREAD_ID", "thread-123")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_DEVICE", "Built-in Mic")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_SAMPLE_RATE", "22050")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_BLOCK_SIZE", "1024")
    monkeypatch.setenv("MINIGENT_VOICE_END_SILENCE_MS", "600")
    monkeypatch.setenv("MINIGENT_VOICE_MAX_RECORD_SECONDS", "9.5")
    monkeypatch.setenv("MINIGENT_VOICE_VAD_THRESHOLD", "0.65")
    monkeypatch.setenv("MINIGENT_VOICE_STT_MODEL", "gpt-4o-transcribe")
    monkeypatch.setenv("MINIGENT_VOICE_USER_ID", "voice-user")
    monkeypatch.setenv("MINIGENT_VOICE_TENANT_ID", "voice-tenant")
    monkeypatch.setenv("MINIGENT_VOICE_ADMIN", "true")
    monkeypatch.setenv("MINIGENT_VOICE_API_TOKEN", "voice-token")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com/v1/")

    config = VoiceDaemonConfig.from_env()

    assert config == VoiceDaemonConfig(
        base_url="http://127.0.0.1:9000",
        wake_phrase="computer",
        skill_name="support",
        thread_id="thread-123",
        audio_device="Built-in Mic",
        audio_sample_rate=22050,
        audio_block_size=1024,
        speech_silence_ms=600,
        speech_max_seconds=9.5,
        vad_threshold=0.65,
        stt_model="gpt-4o-transcribe",
        openai_api_key="openai-key",
        openai_base_url="https://api.example.com/v1",
        principal=PrincipalConfig(
            user_id="voice-user",
            tenant_id="voice-tenant",
            is_admin=True,
            api_token="voice-token",
        ),
    )


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


def test_pcm16le_to_floats_normalizes_samples() -> None:
    assert pcm16le_to_floats(b"\x00\x80\x00\x00\xff\x7f") == [-1.0, 0.0, 32767 / 32768]


def test_manual_audio_activation_source_records_and_transcribes() -> None:
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
            assert audio.sample_rate == 16_000
            return "hello from microphone"

    output_stream = StringIO()
    source = ManualAudioActivationSource(
        input_stream=StringIO("\n"),
        output_stream=output_stream,
        recorder=FakeRecorder(),
        transcriber=FakeTranscriber(),
    )

    activation = source.wait_for_activation("ignored")
    transcript = source.capture_utterance()

    assert activation == Activation()
    assert transcript == "hello from microphone"
    assert "[transcribing] captured 0.20s of audio" in output_stream.getvalue()
    assert "[transcript] hello from microphone" in output_stream.getvalue()


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
