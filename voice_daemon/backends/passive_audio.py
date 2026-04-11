from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, TextIO

from voice_daemon.audio import (
    MicrophoneRecorder,
    RawAudioInputStream,
    pad_with_silence,
    read_chunk,
)
from voice_daemon.debug import CaptureDebugger
from voice_daemon.ring_buffer import AudioRingBuffer
from voice_daemon.service import Activation
from voice_daemon.stt import SpeechToTextAdapter, SpeechToTextError
from voice_daemon.wakeword import WakeWordDetector


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
    capture_debugger: CaptureDebugger | None = None
    stream_context: StreamContext | None = None
    post_wake_speech_timeout_ms: int = 2500
    post_wake_settle_ms: int = 250
    wakeword_cooldown_ms: int = 1500
    stt_pad_leading_ms: int = 250
    stt_pad_trailing_ms: int = 500
    _cooldown_until: float = 0.0
    _closed: bool = False

    def wait_for_activation(self, wake_phrase: str) -> Activation:
        self.output_stream.write(
            f"[idle] passive listening with {self.wake_detector.label} "
            f"(configured wake phrase '{wake_phrase}')\n"
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

    def capture_utterance(self) -> str:
        self.output_stream.write(
            "[listening] wake word detected, recording on a fresh microphone stream until silence\n"
        )
        self.output_stream.flush()
        if self.post_wake_settle_ms > 0:
            time.sleep(self.post_wake_settle_ms / 1000.0)
        audio = self.recorder.record_after_speech(self.post_wake_speech_timeout_ms)
        if audio is None:
            self.output_stream.write("[idle] no speech after wake word, ignoring activation\n")
            self.output_stream.flush()
            self._cooldown_until = time.monotonic() + (self.wakeword_cooldown_ms / 1000.0)
            return ""
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
            self.capture_debugger.log_capture(audio, source="passive-audio")
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
