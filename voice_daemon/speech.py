from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TextIO

from voice_daemon.service import SpeechOutput


@dataclass
class ConsoleSpeechOutput(SpeechOutput):
    output_stream: TextIO

    def speak(self, text: str) -> None:
        self.output_stream.write(f"[assistant] {text}\n")
        self.output_stream.flush()


@dataclass
class MacOsSaySpeechOutput(SpeechOutput):
    output_stream: TextIO
    voice: str | None = None

    def speak(self, text: str) -> None:
        self.output_stream.write(f"[assistant] {text}\n")
        self.output_stream.flush()
        command = ["say"]
        if self.voice:
            command.extend(["-v", self.voice])
        command.append(text)
        try:
            subprocess.run(command, check=True)
        except FileNotFoundError as exc:
            raise RuntimeError("`say` is not available on this system") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"`say` failed with exit code {exc.returncode}") from exc
