from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TextIO

from voice_daemon.service import SpeechOutput


@dataclass
class ConsoleSpeechOutput(SpeechOutput):
    output_stream: TextIO
    _speaking: bool = False

    def speak(self, text: str) -> None:
        self.start(text)
        self.wait()

    def start(self, text: str) -> None:
        self._speaking = True
        self.output_stream.write(f"[assistant] {text}\n")
        self.output_stream.flush()
        self._speaking = False

    def stop(self) -> None:
        self._speaking = False

    def is_speaking(self) -> bool:
        return self._speaking

    def wait(self) -> None:
        self._speaking = False


@dataclass
class MacOsSaySpeechOutput(SpeechOutput):
    output_stream: TextIO
    voice: str | None = None
    _process: subprocess.Popen[str] | None = field(default=None, init=False)
    _interrupted: bool = field(default=False, init=False)

    def speak(self, text: str) -> None:
        self.start(text)
        self.wait()

    def start(self, text: str) -> None:
        self.output_stream.write(f"[assistant] {text}\n")
        self.output_stream.flush()
        command = ["say"]
        if self.voice:
            command.extend(["-v", self.voice])
        command.append(text)
        try:
            self._interrupted = False
            self._process = subprocess.Popen(command, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("`say` is not available on this system") from exc

    def stop(self) -> None:
        if not self.is_speaking():
            return
        self._interrupted = True
        assert self._process is not None
        self._process.terminate()

    def is_speaking(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def wait(self) -> None:
        if self._process is None:
            return
        return_code = self._process.wait()
        interrupted = self._interrupted
        self._process = None
        self._interrupted = False
        if interrupted:
            return
        if return_code != 0:
            raise RuntimeError(f"`say` failed with exit code {return_code}")
