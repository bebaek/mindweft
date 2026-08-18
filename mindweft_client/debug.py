from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from mindweft_client.audio import RecordedAudio


@dataclass(frozen=True)
class CaptureDebugConfig:
    capture_path: str | None = None


class CaptureDebugger:
    def __init__(self, config: CaptureDebugConfig, output_stream: TextIO) -> None:
        self._config = config
        self._output_stream = output_stream

    def log_capture(self, audio: RecordedAudio, *, source: str) -> None:
        nonzero_samples = _count_nonzero_samples(audio.pcm_bytes)
        self._output_stream.write(
            "[capture] "
            f"source={source} "
            f"duration_s={audio.duration_seconds:.2f} "
            f"bytes={len(audio.pcm_bytes)} "
            f"sample_rate={audio.sample_rate} "
            f"channels={audio.channels} "
            f"nonzero_samples={nonzero_samples} "
            f"peak_dbfs={audio.peak_dbfs:.2f} "
            f"rms_dbfs={audio.rms_dbfs:.2f}\n"
        )
        self._output_stream.flush()
        if self._config.capture_path:
            path = Path(self._config.capture_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(audio.to_wav_bytes())
            self._output_stream.write(f"[capture] wrote {path}\n")
            self._output_stream.flush()


def _count_nonzero_samples(pcm_bytes: bytes) -> int:
    if len(pcm_bytes) % 2 != 0:
        return 0
    return sum(1 for sample in memoryview(pcm_bytes).cast("h") if sample != 0)
