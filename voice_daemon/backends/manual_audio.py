from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from voice_daemon.audio import MicrophoneRecorder
from voice_daemon.debug import CaptureDebugger
from voice_daemon.service import Activation
from voice_daemon.stt import SpeechToTextAdapter, SpeechToTextError


@dataclass
class ManualAudioActivationSource:
    input_stream: TextIO
    output_stream: TextIO
    recorder: MicrophoneRecorder
    transcriber: SpeechToTextAdapter
    capture_debugger: CaptureDebugger | None = None

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
        return self._transcribe_audio(audio, source="manual-audio")

    def capture_follow_up_utterance(self, timeout_ms: int) -> str | None:
        del timeout_ms
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
