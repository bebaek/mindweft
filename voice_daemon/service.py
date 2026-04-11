from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from voice_daemon.minigent_client import MinigentClient


class DaemonState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass(frozen=True)
class Activation:
    transcript_hint: str | None = None


class ActivationSource(Protocol):
    def wait_for_activation(self, wake_phrase: str) -> Activation: ...

    def capture_utterance(self) -> str: ...


class SpeechOutput(Protocol):
    def speak(self, text: str) -> None: ...


class VoiceDaemon:
    def __init__(
        self,
        *,
        wake_phrase: str,
        activation_source: ActivationSource,
        minigent_client: MinigentClient,
        speech_output: SpeechOutput,
    ) -> None:
        self._wake_phrase = wake_phrase
        self._activation_source = activation_source
        self._minigent_client = minigent_client
        self._speech_output = speech_output
        self.state = DaemonState.IDLE

    def run_forever(self) -> None:
        while True:
            self.run_once()

    def run_once(self) -> str:
        self.state = DaemonState.IDLE
        activation = self._activation_source.wait_for_activation(self._wake_phrase)
        self.state = DaemonState.LISTENING
        utterance = activation.transcript_hint or self._activation_source.capture_utterance()
        if not utterance.strip():
            self.state = DaemonState.IDLE
            return ""
        self.state = DaemonState.THINKING
        self._minigent_client.send_user_message(utterance)
        reply = self._minigent_client.run_thread()
        self.state = DaemonState.SPEAKING
        self._speech_output.speak(reply)
        self.state = DaemonState.IDLE
        return reply
