from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Literal

AUDIO_WAV_MIME_ALIASES = frozenset({"audio/wav", "audio/x-wav", "audio/wave"})

AudioValidationReason = Literal[
    "unsupported",
    "invalid_wav",
    "empty_audio",
    "unsupported_codec",
    "audio_too_long",
]

AUDIO_VALIDATION_MESSAGES: dict[AudioValidationReason, str] = {
    "unsupported": "unsupported audio MIME type",
    "invalid_wav": "Audio data is not a valid WAV file",
    "empty_audio": "Audio file must not be empty",
    "unsupported_codec": "WAV audio must use uncompressed PCM encoding",
    "audio_too_long": "Audio exceeds the maximum allowed duration",
}


class AudioValidationError(ValueError):
    def __init__(self, reason: AudioValidationReason) -> None:
        self.reason = reason
        super().__init__(AUDIO_VALIDATION_MESSAGES[reason])


@dataclass(frozen=True)
class AudioMetadata:
    mime_type: str
    duration_seconds: float
    channels: int
    sample_rate: int
    sample_width_bytes: int


def canonical_audio_mime_type(mime_type: str) -> str:
    normalized = mime_type.split(";", 1)[0].strip().lower()
    if normalized in AUDIO_WAV_MIME_ALIASES:
        return "audio/wav"
    return normalized


def validate_audio(data: bytes, mime_type: str, *, max_duration_seconds: float) -> AudioMetadata:
    canonical_mime_type = canonical_audio_mime_type(mime_type)
    if canonical_mime_type != "audio/wav":
        raise AudioValidationError("unsupported")
    if len(data) < 12 or data[:4] not in {b"RIFF", b"RIFX"} or data[8:12] != b"WAVE":
        raise AudioValidationError("invalid_wav")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise AudioValidationError("unsupported_codec")
            frame_count = wav_file.getnframes()
            channels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            if frame_count <= 0:
                raise AudioValidationError("empty_audio")
            if channels <= 0 or sample_rate <= 0 or sample_width <= 0:
                raise AudioValidationError("invalid_wav")
            duration = frame_count / sample_rate
            if duration > max_duration_seconds:
                raise AudioValidationError("audio_too_long")
            # Reading the declared frames catches truncated data chunks that wave.open accepts.
            expected_bytes = frame_count * channels * sample_width
            if len(wav_file.readframes(frame_count)) != expected_bytes:
                raise AudioValidationError("invalid_wav")
    except AudioValidationError:
        raise
    except (EOFError, wave.Error) as exc:
        raise AudioValidationError("invalid_wav") from exc
    return AudioMetadata(
        mime_type=canonical_mime_type,
        duration_seconds=duration,
        channels=channels,
        sample_rate=sample_rate,
        sample_width_bytes=sample_width,
    )
