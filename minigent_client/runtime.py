from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, TextIO

from minigent_client.api_client import MinigentAPIClient


class ClientState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    FOLLOW_UP_LISTENING = "follow-up-listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass(frozen=True)
class Activation:
    transcript_hint: str | None = None


class ActivationSource(Protocol):
    def wait_for_activation(self, wake_phrase: str) -> Activation: ...

    def capture_utterance(self) -> str: ...

    def capture_follow_up_utterance(self, timeout_ms: int) -> str | None: ...

    def wait_for_barge_in(
        self,
        wake_phrase: str,
        should_continue: Callable[[], bool],
    ) -> Activation | None: ...


class SpeechOutput(Protocol):
    def speak(self, text: str) -> None: ...

    def start(self, text: str) -> None: ...

    def stop(self) -> None: ...

    def is_speaking(self) -> bool: ...

    def wait(self) -> None: ...


class AmbientVolumeController(Protocol):
    def sync_state(self, state: ClientState) -> None: ...

    def close(self) -> None: ...


class MinigentClientRuntime:
    def __init__(
        self,
        *,
        wake_phrase: str,
        activation_source: ActivationSource,
        minigent_client: MinigentAPIClient,
        speech_output: SpeechOutput,
        activation_feedback: Callable[[], None] | None = None,
        follow_up_timeout_ms: int = 0,
        ambient_volume_controller: AmbientVolumeController | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self._wake_phrase = wake_phrase
        self._activation_source = activation_source
        self._minigent_client = minigent_client
        self._speech_output = speech_output
        self._activation_feedback = activation_feedback
        self._follow_up_timeout_ms = max(follow_up_timeout_ms, 0)
        self._ambient_volume_controller = ambient_volume_controller
        self._output_stream = output_stream or sys.stdout
        self.state = ClientState.IDLE

    def run_forever(self) -> None:
        try:
            while True:
                self.run_once()
        finally:
            self.close()

    def run_once(self) -> str:
        self._set_state(ClientState.IDLE)
        activation = self._activation_source.wait_for_activation(self._wake_phrase)
        reply = ""
        while True:
            self._set_state(ClientState.LISTENING)
            if activation.transcript_hint is None and self._activation_feedback is not None:
                self._activation_feedback()
            utterance = activation.transcript_hint or self._activation_source.capture_utterance()
            if not utterance.strip():
                self._set_state(ClientState.IDLE)
                return reply
            self._set_state(ClientState.THINKING)
            try:
                self._minigent_client.send_user_message(utterance)
                reply, _metadata = self._minigent_client.run_thread()
            except RuntimeError as exc:
                self._handle_backend_error(exc)
                self._set_state(ClientState.IDLE)
                return reply
            self._set_state(ClientState.SPEAKING)
            activation = self._speak_with_optional_barge_in(reply)
            self._minigent_client.flush_pending_token_summary()
            if activation is None:
                activation = self._capture_follow_up_activation()
            if activation is None:
                self._set_state(ClientState.IDLE)
                return reply

    def _handle_backend_error(self, exc: RuntimeError) -> None:
        self._output_stream.write(f"[idle] request failed, returning to wake-word mode: {exc}\n")
        self._output_stream.flush()
        try:
            self._speech_output.speak("I hit an upstream error.")
        except Exception:
            return

    def _speak_with_optional_barge_in(self, reply: str) -> Activation | None:
        self._speech_output.start(reply)
        wait_for_barge_in = getattr(self._activation_source, "wait_for_barge_in", None)
        if not callable(wait_for_barge_in):
            self._speech_output.wait()
            return None
        activation = wait_for_barge_in(self._wake_phrase, self._speech_output.is_speaking)
        if activation is not None:
            self._speech_output.stop()
        self._speech_output.wait()
        return activation

    def _capture_follow_up_activation(self) -> Activation | None:
        if self._follow_up_timeout_ms <= 0:
            return None
        capture_follow_up = getattr(self._activation_source, "capture_follow_up_utterance", None)
        if not callable(capture_follow_up):
            return None
        self._set_state(ClientState.FOLLOW_UP_LISTENING)
        utterance = capture_follow_up(self._follow_up_timeout_ms)
        if utterance is None or not utterance.strip():
            return None
        return Activation(transcript_hint=utterance)

    def _set_state(self, state: ClientState) -> None:
        self.state = state
        if self._ambient_volume_controller is not None:
            self._ambient_volume_controller.sync_state(state)

    def close(self) -> None:
        if self._ambient_volume_controller is not None:
            self._ambient_volume_controller.close()
