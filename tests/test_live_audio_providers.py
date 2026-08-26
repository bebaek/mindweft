from __future__ import annotations

import base64
import math
import os
import struct
import wave
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app.execution import FixedTenantExecutionResolver, parse_tenant_execution_config
from app.llm import GoogleGeminiAdapter, LLMAdapter, OpenAICompatibleAdapter
from app.main import create_app
from app.models import AudioPart, LLMResponse, Message, MessageRole, ToolSpec
from app.tools import ToolRegistry

RUN_LIVE_AUDIO_ENV = "MINDWEFT_RUN_LIVE_AUDIO_PROVIDER_TESTS"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_MODEL_ENVS = ("MINDWEFT_LIVE_AUDIO_OPENAI_MODEL", "OPENAI_MODEL")
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_MODEL_ENVS = ("MINDWEFT_LIVE_AUDIO_OPENROUTER_MODEL", "OPENROUTER_MODEL")
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_MODEL_ENVS = ("MINDWEFT_LIVE_AUDIO_GEMINI_MODEL", "GEMINI_MODEL")
AUTH_HEADERS = {
    "X-Minigent-User-Id": "live-audio-provider-user",
    "X-Minigent-Tenant-Id": "live-audio-provider-tenant",
}

pytestmark = pytest.mark.integration


class _CapturingAudioAdapter(LLMAdapter):
    def __init__(self) -> None:
        self.audio_bytes: bytes | None = None

    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        del tools
        for message in messages:
            for part in message.parts or []:
                if isinstance(part, AudioPart) and part.data:
                    self.audio_bytes = base64.b64decode(part.data, validate=True)
        return LLMResponse(content="The attached audio contains a tone.")

    def describe(self) -> dict[str, object]:
        return {
            "provider": "openai",
            "model": "local-audio-contract",
            "base_url": None,
            "headers": [],
            "adapter": type(self).__name__,
        }


def _truthy_env(name: str) -> bool:
    value = os.getenv(name)
    if value is None and name.startswith("MINDWEFT_"):
        value = os.getenv(name.replace("MINDWEFT_", "MINIGENT_", 1))
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _first_env(names: tuple[str, ...]) -> str | None:
    return next((value for name in names if (value := os.getenv(name))), None)


def _provider_enabled(api_key_env: str, model_envs: tuple[str, ...]) -> bool:
    return (
        _truthy_env(RUN_LIVE_AUDIO_ENV)
        and bool(os.getenv(api_key_env))
        and _first_env(model_envs) is not None
    )


def _tone_wav_bytes(*, duration_seconds: float = 0.25, sample_rate: int = 16_000) -> bytes:
    frame_count = int(duration_seconds * sample_rate)
    frames = bytearray()
    for index in range(frame_count):
        sample = int(0.2 * 32_767 * math.sin(2 * math.pi * 440 * index / sample_rate))
        frames.extend(struct.pack("<h", sample))
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frames)
    return output.getvalue()


def _run_live_audio_smoke(
    monkeypatch: pytest.MonkeyPatch,
    *,
    adapter: LLMAdapter,
    provider: str,
) -> None:
    monkeypatch.setenv("MINDWEFT_AUDIO_INPUT_ENABLED", "true")
    registry = ToolRegistry()
    execution_config = parse_tenant_execution_config(
        AUTH_HEADERS["X-Minigent-Tenant-Id"],
        {
            "llm": {
                "provider": provider,
                "model": adapter.describe().get("model"),
                "input_modalities": ["text", "audio"],
            }
        },
    )
    client = TestClient(
        create_app(
            execution_resolver=FixedTenantExecutionResolver(
                adapter,
                registry,
                config=execution_config,
            )
        )
    )

    create_response = client.post("/threads", headers=AUTH_HEADERS)
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]
    title_response = client.patch(
        f"/threads/{thread_id}/title",
        headers=AUTH_HEADERS,
        json={"title": "Live audio provider smoke"},
    )
    assert title_response.status_code == 200

    wav_bytes = _tone_wav_bytes()
    upload_response = client.post(
        f"/threads/{thread_id}/attachments/binary",
        headers={**AUTH_HEADERS, "Content-Type": "audio/wav"},
        content=wav_bytes,
    )
    assert upload_response.status_code == 200, upload_response.text
    uploaded = upload_response.json()
    assert uploaded["mime_type"] == "audio/wav"
    assert uploaded["size_bytes"] == len(wav_bytes)

    message_response = client.post(
        f"/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
        json={
            "parts": [
                {
                    "type": "text",
                    "text": (
                        "Briefly describe whether the attached audio contains a tone. "
                        "Reply in one short sentence."
                    ),
                },
                {
                    "type": "audio",
                    "mime_type": "audio/wav",
                    "attachment_id": uploaded["attachment_id"],
                    "filename": "integration-tone.wav",
                },
            ]
        },
    )
    assert message_response.status_code == 200
    message = message_response.json()
    assert message["parts"][1]["attachment_id"] == uploaded["attachment_id"]

    run_response = client.post(f"/threads/{thread_id}/run", headers=AUTH_HEADERS)
    assert run_response.status_code == 200, run_response.text
    reply = run_response.json()["reply"]
    assert isinstance(reply, str)
    assert reply.strip()

    messages_response = client.get(f"/threads/{thread_id}/messages", headers=AUTH_HEADERS)
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert [message["role"] for message in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert messages[0]["parts"][1]["attachment_id"] == uploaded["attachment_id"]
    assert messages[1]["content"].strip()


def test_audio_runtime_smoke_harness_hydrates_validated_wav(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _CapturingAudioAdapter()

    _run_live_audio_smoke(monkeypatch, adapter=adapter, provider="openai")

    assert adapter.audio_bytes == _tone_wav_bytes()
    assert adapter.audio_bytes is not None
    assert adapter.audio_bytes[:4] == b"RIFF"
    assert adapter.audio_bytes[8:12] == b"WAVE"


@pytest.mark.skipif(
    not _provider_enabled(OPENAI_API_KEY_ENV, OPENAI_MODEL_ENVS),
    reason=(
        f"Set {RUN_LIVE_AUDIO_ENV}=true, {OPENAI_API_KEY_ENV}, and either "
        f"{OPENAI_MODEL_ENVS[0]} or {OPENAI_MODEL_ENVS[1]} to run the live OpenAI audio smoke test"
    ),
)
def test_live_openai_audio_runtime_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _first_env(OPENAI_MODEL_ENVS)
    assert model is not None
    adapter = OpenAICompatibleAdapter(
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ[OPENAI_API_KEY_ENV],
        model=model,
        timeout=60.0,
    )
    _run_live_audio_smoke(monkeypatch, adapter=adapter, provider="openai")


@pytest.mark.skipif(
    not _provider_enabled(OPENROUTER_API_KEY_ENV, OPENROUTER_MODEL_ENVS),
    reason=(
        f"Set {RUN_LIVE_AUDIO_ENV}=true, {OPENROUTER_API_KEY_ENV}, and either "
        f"{OPENROUTER_MODEL_ENVS[0]} or {OPENROUTER_MODEL_ENVS[1]} to run the live OpenRouter "
        "audio smoke test"
    ),
)
def test_live_openrouter_audio_runtime_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _first_env(OPENROUTER_MODEL_ENVS)
    assert model is not None
    extra_headers = {
        name: value
        for name, value in {
            "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER"),
            "X-Title": os.getenv("OPENROUTER_APP_NAME"),
        }.items()
        if value
    }
    adapter = OpenAICompatibleAdapter(
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=os.environ[OPENROUTER_API_KEY_ENV],
        model=model,
        extra_headers=extra_headers,
        timeout=60.0,
    )
    _run_live_audio_smoke(monkeypatch, adapter=adapter, provider="openrouter")


@pytest.mark.skipif(
    not _provider_enabled(GEMINI_API_KEY_ENV, GEMINI_MODEL_ENVS),
    reason=(
        f"Set {RUN_LIVE_AUDIO_ENV}=true, {GEMINI_API_KEY_ENV}, and either "
        f"{GEMINI_MODEL_ENVS[0]} or {GEMINI_MODEL_ENVS[1]} to run the live Gemini audio smoke test"
    ),
)
def test_live_gemini_audio_runtime_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _first_env(GEMINI_MODEL_ENVS)
    assert model is not None
    adapter = GoogleGeminiAdapter(
        api_key=os.environ[GEMINI_API_KEY_ENV],
        model=model,
        base_url=os.getenv("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
        timeout=60.0,
    )
    _run_live_audio_smoke(monkeypatch, adapter=adapter, provider="google")
