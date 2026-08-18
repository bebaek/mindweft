from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TextIO

from mindweft_client.runtime import Activation


@dataclass
class StdinActivationSource:
    input_stream: TextIO
    output_stream: TextIO

    def wait_for_activation(self, wake_phrase: str) -> Activation:
        normalized_phrase = wake_phrase.casefold()
        while True:
            self.output_stream.write(f"[idle] waiting for wake phrase '{wake_phrase}'\n")
            self.output_stream.flush()
            line = self.input_stream.readline()
            if line == "":
                raise SystemExit(0)
            normalized_line = line.strip()
            if not normalized_line:
                continue
            lowered_line = normalized_line.casefold()
            if not lowered_line.startswith(normalized_phrase):
                continue
            transcript_hint = normalized_line[len(wake_phrase) :].strip()
            return Activation(transcript_hint=transcript_hint or None)

    def capture_utterance(self) -> str:
        self.output_stream.write("[listening] ")
        self.output_stream.flush()
        line = self.input_stream.readline()
        if line == "":
            raise SystemExit(0)
        return line.strip()

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
