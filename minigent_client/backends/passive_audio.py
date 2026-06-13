from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol, TextIO

from minigent_client.audio import (
    MicrophoneRecorder,
    RawAudioInputStream,
    pad_with_silence,
    read_chunk,
)
from minigent_client.debug import CaptureDebugger
from minigent_client.ring_buffer import AudioRingBuffer
from minigent_client.runtime import Activation
from minigent_client.stt import SpeechToTextAdapter, SpeechToTextError
from minigent_client.wakeword import WakeWordDetector


class StreamContext(Protocol):
    def __exit__(self, exc_type, exc, tb) -> None: ...


@dataclass
class PassiveAudioActivationSource:
    output_stream: TextIO
    stream: RawAudioInputStream
    recorder: MicrophoneRecorder
    transcriber: SpeechToTextAdapter
    wake_detector: WakeWordDetector
    preroll_buffer: AudioRingBuffer
    activation_feedback: Callable[[], None] | None = None
    capture_debugger: CaptureDebugger | None = None
    capture_ended_feedback: Callable[[], None] | None = None
    stream_context: StreamContext | None = None
    post_wake_speech_timeout_ms: int = 2500
    post_wake_settle_ms: int = 250
    wakeword_cooldown_ms: int = 1500
    stt_pad_leading_ms: int = 250
    stt_pad_trailing_ms: int = 500
    _cooldown_until: float = 0.0
    _closed: bool = False

    def wait_for_activation(self, wake_phrase: str) -> Activation:
        del wake_phrase
        self.output_stream.write(
            f"[idle] passive listening for wake word {self.wake_detector.label}\n"
        )
        self.output_stream.flush()
        while True:
            chunk = read_chunk(self.stream, self.wake_detector.frame_length)
            if not chunk:
                continue
            self.preroll_buffer.append(chunk)
            if time.monotonic() < self._cooldown_until:
                continue
            if self.wake_detector.process_chunk(chunk):
                self.wake_detector.reset()
                self.preroll_buffer = AudioRingBuffer(max_bytes=self.preroll_buffer.max_bytes)
                self._cooldown_until = time.monotonic() + (self.wakeword_cooldown_ms / 1000.0)
                return Activation()

    def wait_for_barge_in(
        self,
        wake_phrase: str,
        should_continue: Callable[[], bool],
    ) -> Activation | None:
        del wake_phrase
        while should_continue():
            chunk = read_chunk(self.stream, self.wake_detector.frame_length)
            if not chunk:
                continue
            self.preroll_buffer.append(chunk)
            if self.wake_detector.process_chunk(chunk):
                self.wake_detector.reset()
                self.preroll_buffer = AudioRingBuffer(max_bytes=self.preroll_buffer.max_bytes)
                self.output_stream.write("[listening] wake word detected, interrupting speech\n")
                self.output_stream.flush()
                return Activation()
        return None

    def capture_utterance(self) -> str:
        self.output_stream.write("[listening] wake word detected, recording until silence\n")
        self.output_stream.flush()
        if self.post_wake_settle_ms > 0:
            time.sleep(self.post_wake_settle_ms / 1000.0)
        if self.activation_feedback is not None:
            self.activation_feedback()
        audio = self.recorder.record_after_speech_from_stream(
            self.stream,
            timeout_ms=self.post_wake_speech_timeout_ms,
        )
        if audio is None:
            self.output_stream.write("[idle] no speech after wake word, ignoring activation\n")
            self.output_stream.flush()
            self._cooldown_until = time.monotonic() + (self.wakeword_cooldown_ms / 1000.0)
            return ""
        self.output_stream.write("[listening] capture ended\n")
        self.output_stream.flush()
        if self.capture_ended_feedback is not None:
            self.capture_ended_feedback()
        transcript = self._transcribe_recorded_audio(audio, source="passive-audio")
        if transcript:
            return transcript
        return self._capture_follow_up_after_empty_wake_capture()

    def capture_follow_up_utterance(self, timeout_ms: int) -> str | None:
        self.output_stream.write("[follow-up] listening for a follow-up without the wake word\n")
        self.output_stream.flush()
        audio = self.recorder.record_after_speech(timeout_ms)
        if audio is None:
            self.output_stream.write("[follow-up] capture ended\n")
            self.output_stream.flush()
            if self.capture_ended_feedback is not None:
                self.capture_ended_feedback()
            self.output_stream.write(
                "[idle] follow-up window expired, returning to wake-word mode\n"
            )
            self.output_stream.flush()
            return None
        self.output_stream.write("[follow-up] capture ended\n")
        self.output_stream.flush()
        if self.capture_ended_feedback is not None:
            self.capture_ended_feedback()
        return self._transcribe_recorded_audio(audio, source="passive-audio-follow-up")

    def _transcribe_recorded_audio(self, audio, *, source: str) -> str:
        audio = pad_with_silence(
            audio,
            leading_ms=self.stt_pad_leading_ms,
            trailing_ms=self.stt_pad_trailing_ms,
        )
        self.output_stream.write(
            f"[transcribing] captured {audio.duration_seconds:.2f}s of audio\n"
        )
        self.output_stream.flush()
        if self.capture_debugger is not None:
            self.capture_debugger.log_capture(audio, source=source)
        try:
            transcript = self.transcriber.transcribe(audio).strip()
        except SpeechToTextError as exc:
            self.output_stream.write(f"[idle] transcription failed, ignoring capture: {exc}\n")
            self.output_stream.flush()
            self._cooldown_until = time.monotonic() + (self.wakeword_cooldown_ms / 1000.0)
            return ""
        if transcript:
            self.output_stream.write(f"[transcript] {transcript}\n")
            self.output_stream.flush()
        self._cooldown_until = time.monotonic() + (self.wakeword_cooldown_ms / 1000.0)
        return transcript

    def _capture_follow_up_after_empty_wake_capture(self) -> str:
        if self.post_wake_speech_timeout_ms <= 0:
            return ""
        self.output_stream.write(
            "[follow-up] initial wake capture was empty, listening briefly without the wake word\n"
        )
        self.output_stream.flush()
        audio = self.recorder.record_after_speech_from_stream(
            self.stream,
            timeout_ms=self.post_wake_speech_timeout_ms,
        )
        if audio is None:
            self.output_stream.write(
                "[idle] no follow-up speech after wake word, ignoring activation\n"
            )
            self.output_stream.flush()
            self._cooldown_until = time.monotonic() + (self.wakeword_cooldown_ms / 1000.0)
            return ""
        self.output_stream.write("[follow-up] capture ended\n")
        self.output_stream.flush()
        if self.capture_ended_feedback is not None:
            self.capture_ended_feedback()
        return self._transcribe_recorded_audio(audio, source="passive-audio-follow-up-after-empty")

    def close(self) -> None:
        if self._closed or self.stream_context is None:
            self._closed = True
            return
        try:
            self.stream_context.__exit__(None, None, None)
        finally:
            self.stream_context = None
            self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            return None
