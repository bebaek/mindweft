from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TextIO

from minigent_client.audio import MicrophoneRecorder
from minigent_client.debug import CaptureDebugger
from minigent_client.runtime import Activation
from minigent_client.stt import SpeechToTextAdapter, SpeechToTextError


@dataclass
class ManualAudioActivationSource:
    input_stream: TextIO
    output_stream: TextIO
    recorder: MicrophoneRecorder
    transcriber: SpeechToTextAdapter
    capture_debugger: CaptureDebugger | None = None
    capture_ended_feedback: Callable[[], None] | None = None

    def wait_for_activation(self, wake_phrase: str) -> Activation:
        del wake_phrase
        self.output_stream.write("[idle] press Enter to record, or Ctrl-D to exit\n")
        self.output_stream.flush()
        line = self.input_stream.readline()
        if line == "":
            raise SystemExit(0)
        return Activation()

    def capture_utterance(self) -> str:
        self.output_stream.write("[listening] recording from microphone until silence\n")
        self.output_stream.flush()
        audio = self.recorder.record_until_silence()
        self.output_stream.write("[listening] capture ended\n")
        self.output_stream.flush()
        if self.capture_ended_feedback is not None:
            self.capture_ended_feedback()
        return self._transcribe_audio(audio, source="manual-audio")

    def capture_follow_up_utterance(self, timeout_ms: int) -> str | None:
        del timeout_ms
        return None

    def wait_for_barge_in(
        self,
        wake_phrase: str,
        should_continue: Callable[[], bool],
    ) -> Activation | None:
        del wake_phrase, should_continue
        return None

    def _transcribe_audio(self, audio, *, source: str) -> str:
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
            return ""
        if transcript:
            self.output_stream.write(f"[transcript] {transcript}\n")
            self.output_stream.flush()
        return transcript
