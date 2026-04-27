from __future__ import annotations

import io
import math
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from minigent_client.ring_buffer import AudioRingBuffer


class AudioDependencyError(RuntimeError):
    """Raised when optional voice audio dependencies are unavailable."""


class ChunkSpeechDetector(Protocol):
    def reset(self) -> None: ...

    def is_speech(self, samples: list[float], sample_rate: int) -> bool: ...


class RawAudioInputStream(Protocol):
    def read(self, frames: int) -> tuple[object, bool]: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


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

    @property
    def nonzero_samples(self) -> int:
        if len(self.pcm_bytes) % self.sample_width_bytes != 0:
            return 0
        return sum(1 for sample in memoryview(self.pcm_bytes).cast("h") if sample != 0)

    @property
    def peak_abs_sample(self) -> int:
        if len(self.pcm_bytes) % self.sample_width_bytes != 0 or not self.pcm_bytes:
            return 0
        return max(abs(sample) for sample in memoryview(self.pcm_bytes).cast("h"))

    @property
    def rms_sample(self) -> float:
        if len(self.pcm_bytes) % self.sample_width_bytes != 0 or not self.pcm_bytes:
            return 0.0
        samples = memoryview(self.pcm_bytes).cast("h")
        mean_square = sum(sample * sample for sample in samples) / len(samples)
        return math.sqrt(mean_square)

    @property
    def peak_dbfs(self) -> float:
        peak = self.peak_abs_sample
        if peak <= 0:
            return float("-inf")
        return 20.0 * math.log10(peak / 32767.0)

    @property
    def rms_dbfs(self) -> float:
        rms = self.rms_sample
        if rms <= 0:
            return float("-inf")
        return 20.0 * math.log10(rms / 32767.0)


class MicrophoneRecorder:
    def __init__(self, config: AudioCaptureConfig, detector: ChunkSpeechDetector) -> None:
        self._config = config
        self._detector = detector

    def record_until_silence(self) -> RecordedAudio:
        with open_microphone_stream(self._config) as stream:
            return self.record_until_silence_from_stream(stream)

    def record_after_speech(self, timeout_ms: int, *, preroll_ms: int = 250) -> RecordedAudio | None:
        with open_microphone_stream(self._config) as stream:
            return self.record_after_speech_from_stream(
                stream,
                timeout_ms=timeout_ms,
                preroll_ms=preroll_ms,
            )

    @property
    def block_size(self) -> int:
        return self._config.block_size

    @property
    def sample_rate(self) -> int:
        return self._config.sample_rate

    def chunk_has_speech(self, chunk: bytes) -> bool:
        if not chunk:
            return False
        for frame in split_pcm_chunk(chunk, self._config.block_size):
            if self._detector.is_speech(pcm16le_to_floats(frame), self._config.sample_rate):
                return True
        return False

    def record_until_silence_from_stream(
        self,
        stream: RawAudioInputStream,
        *,
        initial_chunks: list[bytes] | None = None,
    ) -> RecordedAudio:
        self._detector.reset()
        pcm_chunks: list[bytes] = list(initial_chunks or [])
        speech_started = any(
            self._detector.is_speech(pcm16le_to_floats(frame), self._config.sample_rate)
            for chunk in pcm_chunks
            for frame in split_pcm_chunk(chunk, self._config.block_size)
        )
        silence_started_at: float | None = None
        started_at = time.monotonic()

        while True:
            if time.monotonic() - started_at >= self._config.max_record_seconds:
                break
            chunk = read_chunk(stream, self._config.block_size)
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

    def record_after_speech_from_stream(
        self,
        stream: RawAudioInputStream,
        *,
        timeout_ms: int,
        preroll_ms: int = 250,
    ) -> RecordedAudio | None:
        if timeout_ms <= 0:
            return self.record_until_silence_from_stream(stream)
        self._detector.reset()
        preroll_buffer = AudioRingBuffer(
            max_bytes=max(
                0,
                int(
                    self._config.sample_rate
                    * self._config.channels
                    * self._config.sample_width_bytes
                    * (max(preroll_ms, 0) / 1000.0)
                ),
            )
        )
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            chunk = read_chunk(stream, self._config.block_size)
            if not chunk:
                continue
            preroll_buffer.append(chunk)
            if self.chunk_has_speech(chunk):
                return self.record_until_silence_from_stream(
                    stream,
                    initial_chunks=preroll_buffer.snapshot(),
                )
        return None


def pcm16le_to_floats(chunk: bytes) -> list[float]:
    if len(chunk) % 2 != 0:
        raise ValueError("PCM16 chunk length must be even")
    samples = memoryview(chunk).cast("h")
    return [sample / 32768.0 for sample in samples]


def pcm16le_to_ints(chunk: bytes) -> list[int]:
    if len(chunk) % 2 != 0:
        raise ValueError("PCM16 chunk length must be even")
    return list(memoryview(chunk).cast("h"))


def split_pcm_chunk(chunk: bytes, frames_per_chunk: int) -> list[bytes]:
    if frames_per_chunk <= 0:
        raise ValueError("frames_per_chunk must be positive")
    bytes_per_frame = 2
    chunk_size = frames_per_chunk * bytes_per_frame
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [
        chunk[index : index + chunk_size]
        for index in range(0, len(chunk), chunk_size)
        if len(chunk[index : index + chunk_size]) == chunk_size
    ]


@dataclass
class ManagedRawInputStream:
    stream: RawAudioInputStream

    def __enter__(self) -> RawAudioInputStream:
        self.stream.start()
        return self.stream

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stream.stop()
        self.stream.close()


def open_microphone_stream(config: AudioCaptureConfig) -> ManagedRawInputStream:
    sounddevice = _load_sounddevice()
    stream_kwargs: dict[str, object] = {
        "samplerate": config.sample_rate,
        "channels": config.channels,
        "dtype": "int16",
        "blocksize": config.block_size,
    }
    if config.device is not None:
        stream_kwargs["device"] = config.device
    return ManagedRawInputStream(sounddevice.RawInputStream(**stream_kwargs))


def read_chunk(stream: RawAudioInputStream, frames: int) -> bytes:
    chunk_bytes, overflowed = stream.read(frames)
    if overflowed:
        return b""
    return bytes(chunk_bytes)


def load_recorded_audio_from_wav(path: str | Path) -> RecordedAudio:
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width_bytes = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        pcm_bytes = wav_file.readframes(wav_file.getnframes())
    if sample_width_bytes != 2:
        raise ValueError("Only 16-bit PCM WAV files are supported")
    return RecordedAudio(
        pcm_bytes=pcm_bytes,
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width_bytes,
    )


def apply_gain(audio: RecordedAudio, gain_multiplier: float) -> RecordedAudio:
    if gain_multiplier <= 0:
        raise ValueError("gain_multiplier must be positive")
    if abs(gain_multiplier - 1.0) < 1e-9:
        return audio
    samples = memoryview(audio.pcm_bytes).cast("h")
    amplified = bytearray()
    for sample in samples:
        boosted = int(round(sample * gain_multiplier))
        boosted = max(-32768, min(32767, boosted))
        amplified.extend(int(boosted).to_bytes(2, byteorder="little", signed=True))
    return RecordedAudio(
        pcm_bytes=bytes(amplified),
        sample_rate=audio.sample_rate,
        channels=audio.channels,
        sample_width_bytes=audio.sample_width_bytes,
    )


def normalize_peak(audio: RecordedAudio, target_peak: float = 0.8) -> tuple[RecordedAudio, float]:
    if not 0 < target_peak <= 1.0:
        raise ValueError("target_peak must be between 0 and 1")
    peak = audio.peak_abs_sample
    if peak <= 0:
        return audio, 1.0
    target_sample = target_peak * 32767.0
    gain = target_sample / peak
    return apply_gain(audio, gain), gain


def pad_with_silence(
    audio: RecordedAudio,
    *,
    leading_ms: int = 0,
    trailing_ms: int = 0,
) -> RecordedAudio:
    if leading_ms < 0 or trailing_ms < 0:
        raise ValueError("padding must be non-negative")
    if leading_ms == 0 and trailing_ms == 0:
        return audio
    bytes_per_frame = audio.channels * audio.sample_width_bytes
    if bytes_per_frame <= 0:
        raise ValueError("bytes_per_frame must be positive")
    leading_frames = int(round(audio.sample_rate * (leading_ms / 1000.0)))
    trailing_frames = int(round(audio.sample_rate * (trailing_ms / 1000.0)))
    leading = b"\x00" * (leading_frames * bytes_per_frame)
    trailing = b"\x00" * (trailing_frames * bytes_per_frame)
    return RecordedAudio(
        pcm_bytes=leading + audio.pcm_bytes + trailing,
        sample_rate=audio.sample_rate,
        channels=audio.channels,
        sample_width_bytes=audio.sample_width_bytes,
    )


def _load_sounddevice():
    try:
        import sounddevice  # type: ignore[import-not-found]
    except ImportError as exc:
        raise AudioDependencyError(
            "sounddevice is required for microphone capture. Install with `uv sync --extra voice`."
        ) from exc
    return sounddevice
