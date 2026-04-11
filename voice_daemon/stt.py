from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from voice_daemon.audio import RecordedAudio


class SpeechToTextError(RuntimeError):
    """Raised when transcription fails."""


class SpeechToTextAdapter(Protocol):
    def transcribe(self, audio: RecordedAudio) -> str: ...


@dataclass(frozen=True)
class SpeechProviderConfig:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: float = 60.0
    app_name: str | None = None
    http_referer: str | None = None


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


@dataclass(frozen=True)
class OpenRouterTranscriptionConfig:
    api_key: str
    model: str
    base_url: str = "https://openrouter.ai/api/v1"
    timeout_seconds: float = 60.0
    app_name: str | None = None
    http_referer: str | None = None


class OpenRouterTranscriptionAdapter:
    def __init__(self, config: OpenRouterTranscriptionConfig) -> None:
        self._config = config

    def transcribe(self, audio: RecordedAudio) -> str:
        encoded_audio = base64.b64encode(audio.to_wav_bytes()).decode("ascii")
        payload = {
            "model": self._config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a speech transcription engine. Return only the verbatim "
                        "transcript text. Do not answer questions, summarize, or acknowledge "
                        "the request."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Transcribe the attached audio verbatim. Return only the "
                                "transcript."
                            ),
                        },
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": encoded_audio,
                                "format": "wav",
                            },
                        },
                    ],
                }
            ],
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        if self._config.http_referer:
            headers["HTTP-Referer"] = self._config.http_referer
        if self._config.app_name:
            headers["X-Title"] = self._config.app_name
        try:
            response = httpx.post(
                f"{self._config.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text or str(exc)
            raise SpeechToTextError(f"OpenRouter transcription failed: {detail}") from exc
        except httpx.HTTPError as exc:
            raise SpeechToTextError(f"OpenRouter transcription request failed: {exc}") from exc

        text = _parse_openrouter_transcription_response(response.json())
        if not text:
            raise SpeechToTextError("OpenRouter transcription response did not include text")
        return text.strip()


def build_transcription_adapter(config: SpeechProviderConfig) -> SpeechToTextAdapter:
    provider = config.provider.lower()
    if provider == "openai":
        return OpenAITranscriptionAdapter(
            OpenAITranscriptionConfig(
                api_key=config.api_key,
                model=config.model,
                base_url=config.base_url,
                timeout_seconds=config.timeout_seconds,
            )
        )
    if provider == "openrouter":
        return OpenRouterTranscriptionAdapter(
            OpenRouterTranscriptionConfig(
                api_key=config.api_key,
                model=config.model,
                base_url=config.base_url,
                timeout_seconds=config.timeout_seconds,
                app_name=config.app_name,
                http_referer=config.http_referer,
            )
        )
    raise SpeechToTextError(f"Unsupported speech provider '{config.provider}'")


def _parse_openrouter_transcription_response(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return _extract_transcript_from_string(content)
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        return _extract_transcript_from_string("".join(text_parts))
    return ""


def _extract_transcript_from_string(content: str) -> str:
    stripped = content.strip()
    if not stripped:
        return ""
    try:
        parsed = httpx.Response(200, text=stripped).json()
    except Exception:
        return stripped
    if isinstance(parsed, dict) and isinstance(parsed.get("transcript"), str):
        return parsed["transcript"]
    return stripped
