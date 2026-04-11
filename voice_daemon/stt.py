from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from voice_daemon.audio import RecordedAudio


class SpeechToTextError(RuntimeError):
    """Raised when transcription fails."""


class SpeechToTextAdapter(Protocol):
    def transcribe(self, audio: RecordedAudio) -> str: ...


@dataclass(frozen=True)
class OpenAITranscriptionConfig:
    api_key: str
    model: str = "gpt-4o-mini-transcribe"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0


class OpenAITranscriptionAdapter:
    def __init__(self, config: OpenAITranscriptionConfig) -> None:
        self._config = config

    def transcribe(self, audio: RecordedAudio) -> str:
        files = {
            "file": ("utterance.wav", audio.to_wav_bytes(), "audio/wav"),
            "model": (None, self._config.model),
        }
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        try:
            response = httpx.post(
                f"{self._config.base_url.rstrip('/')}/audio/transcriptions",
                files=files,
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text or str(exc)
            raise SpeechToTextError(f"OpenAI transcription failed: {detail}") from exc
        except httpx.HTTPError as exc:
            raise SpeechToTextError(f"OpenAI transcription request failed: {exc}") from exc

        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise SpeechToTextError("OpenAI transcription response did not include text")
        return payload["text"].strip()
