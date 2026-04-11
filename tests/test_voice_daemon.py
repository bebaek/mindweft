from __future__ import annotations

from io import StringIO

from voice_daemon.backends.stdin_loop import ConsoleSpeechOutput, StdinActivationSource
from voice_daemon.config import PrincipalConfig, VoiceDaemonConfig
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
    monkeypatch.setenv("MINIGENT_VOICE_USER_ID", "voice-user")
    monkeypatch.setenv("MINIGENT_VOICE_TENANT_ID", "voice-tenant")
    monkeypatch.setenv("MINIGENT_VOICE_ADMIN", "true")
    monkeypatch.setenv("MINIGENT_VOICE_API_TOKEN", "voice-token")

    config = VoiceDaemonConfig.from_env()

    assert config == VoiceDaemonConfig(
        base_url="http://127.0.0.1:9000",
        wake_phrase="computer",
        skill_name="support",
        thread_id="thread-123",
        principal=PrincipalConfig(
            user_id="voice-user",
            tenant_id="voice-tenant",
            is_admin=True,
            api_token="voice-token",
        ),
    )
