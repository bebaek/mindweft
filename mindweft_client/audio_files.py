from __future__ import annotations

import io
import wave
from pathlib import Path


def read_audio_file(raw_path: str) -> tuple[Path, str, bytes]:
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ValueError(f"audio file not found: {raw_path}")
    if path.suffix.lower() != ".wav":
        raise ValueError(f"unsupported audio file type: {raw_path}; expected .wav")
    data = path.read_bytes()
    try:
        with wave.open(io.BytesIO(data), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise ValueError(f"WAV audio must use uncompressed PCM encoding: {raw_path}")
            if wav_file.getnframes() <= 0:
                raise ValueError(f"audio file must not be empty: {raw_path}")
            expected = wav_file.getnframes() * wav_file.getnchannels() * wav_file.getsampwidth()
            if len(wav_file.readframes(wav_file.getnframes())) != expected:
                raise ValueError(f"invalid or truncated WAV audio: {raw_path}")
    except (EOFError, wave.Error) as exc:
        raise ValueError(f"invalid WAV audio: {raw_path}") from exc
    return path, "audio/wav", data
