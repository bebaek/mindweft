from __future__ import annotations

from dataclasses import dataclass
from typing import TextIO

from voice_daemon.audio import MicrophoneRecorder
from voice_daemon.service import Activation
from voice_daemon.stt import SpeechToTextAdapter


@dataclass
class ManualAudioActivationSource:
    input_stream: TextIO
    output_stream: TextIO
    recorder: MicrophoneRecorder
    transcriber: SpeechToTextAdapter

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
        self.output_stream.write(
            f"[transcribing] captured {audio.duration_seconds:.2f}s of audio\n"
        )
        self.output_stream.flush()
        transcript = self.transcriber.transcribe(audio).strip()
        if transcript:
            self.output_stream.write(f"[transcript] {transcript}\n")
            self.output_stream.flush()
        return transcript
