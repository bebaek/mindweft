import io
import wave

import pytest

from mindweft_client.audio_files import read_audio_file


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x01\x00" * 160)
    return output.getvalue()


def test_read_audio_file_accepts_pcm_wav(tmp_path) -> None:
    path = tmp_path / "note.wav"
    path.write_bytes(_wav_bytes())
    resolved, mime_type, data = read_audio_file(str(path))
    assert resolved == path
    assert mime_type == "audio/wav"
    assert data == path.read_bytes()


@pytest.mark.parametrize("name", ["missing.wav", "note.mp3"])
def test_read_audio_file_rejects_missing_or_unsupported_files(tmp_path, name: str) -> None:
    path = tmp_path / name
    if name.endswith(".mp3"):
        path.write_bytes(b"not audio")
    with pytest.raises(ValueError):
        read_audio_file(str(path))
