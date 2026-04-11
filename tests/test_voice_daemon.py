from __future__ import annotations

from io import StringIO
import wave

import pytest

from voice_daemon.audio import (
    AudioCaptureConfig,
    RecordedAudio,
    apply_gain,
    split_pcm_chunk,
    load_recorded_audio_from_wav,
    normalize_peak,
    pad_with_silence,
    pcm16le_to_floats,
)
from voice_daemon.backends.manual_audio import ManualAudioActivationSource
from voice_daemon.backends.passive_audio import PassiveAudioActivationSource
from voice_daemon.backends.stdin_loop import ConsoleSpeechOutput, StdinActivationSource
from voice_daemon.debug import CaptureDebugConfig, CaptureDebugger
from voice_daemon.config import PrincipalConfig, VoiceDaemonConfig
from voice_daemon.ring_buffer import AudioRingBuffer
from voice_daemon.stt import (
    OpenAITranscriptionAdapter,
    OpenAITranscriptionConfig,
    OpenRouterTranscriptionAdapter,
    OpenRouterTranscriptionConfig,
    SpeechToTextError,
    build_transcription_adapter,
)
from voice_daemon.cli import build_speech_provider_config, build_wake_word_detector
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
    monkeypatch.setenv("MINIGENT_VOICE_STT_PROVIDER", "openrouter")
    monkeypatch.setenv("MINIGENT_VOICE_WAKEWORD_PROVIDER", "porcupine")
    monkeypatch.setenv("MINIGENT_VOICE_SKILL", "support")
    monkeypatch.setenv("MINIGENT_VOICE_THREAD_ID", "thread-123")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_DEVICE", "Built-in Mic")
    monkeypatch.setenv("MINIGENT_VOICE_DEBUG_CAPTURE_PATH", "/tmp/minigent-last-capture.wav")
    monkeypatch.setenv("MINIGENT_VOICE_STT_DEBUG_PATH", "/tmp/minigent-stt-debug")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_SAMPLE_RATE", "22050")
    monkeypatch.setenv("MINIGENT_VOICE_AUDIO_BLOCK_SIZE", "1024")
    monkeypatch.setenv("MINIGENT_VOICE_END_SILENCE_MS", "600")
    monkeypatch.setenv("MINIGENT_VOICE_MAX_RECORD_SECONDS", "9.5")
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
        stt_provider="openrouter",
        wakeword_provider="porcupine",
        skill_name="support",
        thread_id="thread-123",
        audio_device="Built-in Mic",
        debug_capture_path="/tmp/minigent-last-capture.wav",
        stt_debug_path="/tmp/minigent-stt-debug",
        audio_sample_rate=22050,
        audio_block_size=1024,
        speech_silence_ms=600,
        speech_max_seconds=9.5,
        wakeword_cooldown_ms=1500,
        post_wake_speech_timeout_ms=2500,
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


def test_passive_audio_activation_source_records_on_fresh_stream() -> None:
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

        def record_until_silence(self) -> RecordedAudio:
            self.record_calls += 1
            return RecordedAudio(
                pcm_bytes=b"\x03\x00" * 8,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )

    class FakeTranscriber:
        def transcribe(self, audio: RecordedAudio) -> str:
            leading_bytes = 16_000 // 2
            trailing_bytes = 16_000
            assert audio.pcm_bytes[:leading_bytes] == (b"\x00" * leading_bytes)
            assert audio.pcm_bytes[leading_bytes:-trailing_bytes] == (b"\x03\x00" * 8)
            assert audio.pcm_bytes[-trailing_bytes:] == (b"\x00" * trailing_bytes)
            return "wake word request"

    output_stream = StringIO()
    recorder = FakeRecorder()
    source = PassiveAudioActivationSource(
        output_stream=output_stream,
        stream=FakeStream(),
        recorder=recorder,
        transcriber=FakeTranscriber(),
        wake_detector=FakeWakeDetector(),
        preroll_buffer=AudioRingBuffer(max_bytes=32),
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
    assert recorder.record_calls == 1
    assert source.wake_detector.reset_calls == 1
    assert "openwakeword:okay_nabu" in output_stream.getvalue()
    assert "[listening] wake word detected" in output_stream.getvalue()


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
        def record_until_silence(self) -> RecordedAudio:
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


def test_build_transcription_adapter_supports_both_providers() -> None:
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


def test_voice_daemon_config_defaults_openrouter_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIGENT_VOICE_STT_MODEL", raising=False)
    monkeypatch.setenv("MINIGENT_VOICE_STT_PROVIDER", "openrouter")

    config = VoiceDaemonConfig.from_env()

    assert config.stt_model == "openai/gpt-audio"


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
