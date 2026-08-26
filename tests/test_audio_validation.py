import io
import wave

import pytest

from app.audio_validation import AudioValidationError, canonical_audio_mime_type, validate_audio


def wav_bytes(*, frames: int = 1600, sample_rate: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x01\x00" * frames)
    return output.getvalue()


def test_validates_pcm_wav_and_reports_metadata() -> None:
    metadata = validate_audio(wav_bytes(), "audio/x-wav; charset=binary", max_duration_seconds=1)
    assert metadata.mime_type == "audio/wav"
    assert metadata.duration_seconds == pytest.approx(0.1)
    assert metadata.channels == 1
    assert metadata.sample_rate == 16000


def test_canonical_audio_mime_type_normalizes_wav_aliases() -> None:
    assert canonical_audio_mime_type("Audio/Wave") == "audio/wav"
    assert canonical_audio_mime_type("audio/x-wav") == "audio/wav"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "invalid_wav"),
        (b"RIFF\x00\x00\x00\x00WAVE", "invalid_wav"),
        (wav_bytes(frames=0), "empty_audio"),
    ],
)
def test_rejects_invalid_or_empty_wav(data: bytes, reason: str) -> None:
    with pytest.raises(AudioValidationError) as caught:
        validate_audio(data, "audio/wav", max_duration_seconds=10)
    assert caught.value.reason == reason


def test_rejects_over_duration_wav() -> None:
    with pytest.raises(AudioValidationError) as caught:
        validate_audio(wav_bytes(frames=32000), "audio/wav", max_duration_seconds=1)
    assert caught.value.reason == "audio_too_long"


def test_rejects_unsupported_mime_type() -> None:
    with pytest.raises(AudioValidationError) as caught:
        validate_audio(wav_bytes(), "audio/mpeg", max_duration_seconds=10)
    assert caught.value.reason == "unsupported"
