from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from minigent_client.audio import RecordedAudio


class SpeechToTextError(RuntimeError):
    """Raised when transcription fails."""


class SpeechToTextAdapter(Protocol):
    def transcribe(self, audio: RecordedAudio) -> str: ...


@dataclass(frozen=True)
class SpeechProviderConfig:
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = 60.0
    app_name: str | None = None
    http_referer: str | None = None
    debug_path: str | None = None
    device: str | None = None
    compute_type: str | None = None
    language: str | None = None


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
    debug_path: str | None = None


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
        self._write_debug_artifact(
            "request.json",
            {
                "url": f"{self._config.base_url.rstrip('/')}/chat/completions",
                "headers": _redact_headers(headers),
                "payload": payload,
            },
        )
        try:
            response = httpx.post(
                f"{self._config.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
            self._write_debug_artifact(
                "response.json",
                {
                    "status_code": response.status_code,
                    "body": _safe_json_or_text(response),
                },
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
        _raise_for_invalid_transcript(text)
        return text.strip()

    def _write_debug_artifact(self, filename: str, payload: dict[str, Any]) -> None:
        if not self._config.debug_path:
            return
        path = Path(self._config.debug_path)
        path.mkdir(parents=True, exist_ok=True)
        (path / filename).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class FasterWhisperTranscriptionConfig:
    model: str = "base"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = None


class FasterWhisperTranscriptionAdapter:
    def __init__(self, config: FasterWhisperTranscriptionConfig) -> None:
        self._config = config
        self._model = _load_faster_whisper_model(
            config.model,
            device=config.device,
            compute_type=config.compute_type,
        )

    def transcribe(self, audio: RecordedAudio) -> str:
        audio_samples = _recorded_audio_to_float32_mono(audio)
        try:
            segments, _ = self._model.transcribe(
                audio_samples,
                beam_size=5,
                task="transcribe",
                vad_filter=False,
                language=self._config.language,
            )
        except Exception as exc:
            raise SpeechToTextError(f"faster-whisper transcription failed: {exc}") from exc
        text = "".join(segment.text for segment in segments).strip()
        if not text:
            raise SpeechToTextError("faster-whisper transcription response did not include text")
        return text


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
                debug_path=config.debug_path,
            )
        )
    if provider == "faster-whisper":
        return FasterWhisperTranscriptionAdapter(
            FasterWhisperTranscriptionConfig(
                model=config.model,
                device=config.device or "cpu",
                compute_type=config.compute_type or "int8",
                language=config.language,
            )
        )
    raise SpeechToTextError(f"Unsupported speech provider '{config.provider}'")


def _load_faster_whisper_model(model: str, *, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SpeechToTextError(
            "faster-whisper is required for local transcription. Install with `uv sync --extra voice`."
        ) from exc
    return WhisperModel(model, device=device, compute_type=compute_type)


def _recorded_audio_to_float32_mono(audio: RecordedAudio):
    try:
        import numpy as np
    except ImportError as exc:
        raise SpeechToTextError(
            "numpy is required for local transcription. Install with `uv sync --extra voice`."
        ) from exc
    samples = np.frombuffer(audio.pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if audio.channels > 1:
        samples = samples.reshape(-1, audio.channels).mean(axis=1)
    return samples


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


def _safe_json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = dict(headers)
    if "Authorization" in redacted:
        redacted["Authorization"] = "Bearer ***"
    return redacted


def _raise_for_invalid_transcript(text: str) -> None:
    lowered = text.strip().lower()
    suspicious_phrases = (
        "please provide the audio",
        "upload the audio",
        "upload the audio file",
        "unable to process audio",
        "cannot process audio",
        "can't process audio",
        "i'm sorry",
        "i can’t assist with that",
        "i can't assist with that",
        "audio attachment",
        "play the audio aloud",
        "if you upload the audio",
        "don't have an audio attached",
        "do not have an audio attached",
        "no audio attached",
        "uploading it again",
        "once it's available",
    )
    if any(phrase in lowered for phrase in suspicious_phrases):
        raise SpeechToTextError(
            "STT provider returned assistant-style text instead of a transcript"
        )
