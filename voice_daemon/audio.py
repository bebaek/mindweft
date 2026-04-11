from __future__ import annotations

import io
import time
import wave
from dataclasses import dataclass
from typing import Protocol


class AudioDependencyError(RuntimeError):
    """Raised when optional voice audio dependencies are unavailable."""


class ChunkSpeechDetector(Protocol):
    def reset(self) -> None: ...

    def is_speech(self, samples: list[float], sample_rate: int) -> bool: ...


@dataclass(frozen=True)
class AudioCaptureConfig:
    sample_rate: int = 16_000
    channels: int = 1
    sample_width_bytes: int = 2
    block_size: int = 512
    max_record_seconds: float = 15.0
    end_silence_ms: int = 800
    device: str | int | None = None


@dataclass(frozen=True)
class RecordedAudio:
    pcm_bytes: bytes
    sample_rate: int
    channels: int
    sample_width_bytes: int

    def to_wav_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(self.sample_width_bytes)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(self.pcm_bytes)
        return buffer.getvalue()

    @property
    def duration_seconds(self) -> float:
        frame_size = self.channels * self.sample_width_bytes
        if frame_size <= 0:
            return 0.0
        return len(self.pcm_bytes) / frame_size / self.sample_rate


class MicrophoneRecorder:
    def __init__(self, config: AudioCaptureConfig, detector: ChunkSpeechDetector) -> None:
        self._config = config
        self._detector = detector

    def record_until_silence(self) -> RecordedAudio:
        sounddevice = _load_sounddevice()
        self._detector.reset()
        pcm_chunks: list[bytes] = []
        speech_started = False
        silence_started_at: float | None = None
        started_at = time.monotonic()

        stream_kwargs: dict[str, object] = {
            "samplerate": self._config.sample_rate,
            "channels": self._config.channels,
            "dtype": "int16",
            "blocksize": self._config.block_size,
        }
        if self._config.device is not None:
            stream_kwargs["device"] = self._config.device

        with sounddevice.RawInputStream(**stream_kwargs) as stream:
            while True:
                if time.monotonic() - started_at >= self._config.max_record_seconds:
                    break
                chunk_bytes, overflowed = stream.read(self._config.block_size)
                if overflowed:
                    continue
                chunk = bytes(chunk_bytes)
                if not chunk:
                    continue
                pcm_chunks.append(chunk)
                chunk_samples = pcm16le_to_floats(chunk)
                is_speech = self._detector.is_speech(chunk_samples, self._config.sample_rate)
                if is_speech:
                    speech_started = True
                    silence_started_at = None
                    continue
                if not speech_started:
                    continue
                if silence_started_at is None:
                    silence_started_at = time.monotonic()
                    continue
                silence_ms = (time.monotonic() - silence_started_at) * 1000.0
                if silence_ms >= self._config.end_silence_ms:
                    break

        return RecordedAudio(
            pcm_bytes=b"".join(pcm_chunks),
            sample_rate=self._config.sample_rate,
            channels=self._config.channels,
            sample_width_bytes=self._config.sample_width_bytes,
        )


def pcm16le_to_floats(chunk: bytes) -> list[float]:
    if len(chunk) % 2 != 0:
        raise ValueError("PCM16 chunk length must be even")
    samples = memoryview(chunk).cast("h")
    return [sample / 32768.0 for sample in samples]


def _load_sounddevice():
    try:
        import sounddevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AudioDependencyError(
            "sounddevice is required for microphone capture. Install with `uv sync --extra voice`."
        ) from exc
    return sounddevice
