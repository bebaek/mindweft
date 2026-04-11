from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PrincipalConfig:
    user_id: str
    tenant_id: str
    is_admin: bool = False
    api_token: str | None = None

    def build_headers(self) -> dict[str, str]:
        if self.api_token:
            return {"Authorization": f"Bearer {self.api_token}"}
        return {
            "X-Minigent-User-Id": self.user_id,
            "X-Minigent-Tenant-Id": self.tenant_id,
            "X-Minigent-Admin": "true" if self.is_admin else "false",
        }


@dataclass(frozen=True)
class VoiceDaemonConfig:
    base_url: str
    wake_phrase: str
    skill_name: str | None = None
    thread_id: str | None = None
    audio_device: str | None = None
    audio_sample_rate: int = 16_000
    audio_block_size: int = 512
    speech_silence_ms: int = 800
    speech_max_seconds: float = 15.0
    vad_threshold: float = 0.5
    stt_model: str = "gpt-4o-mini-transcribe"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    principal: PrincipalConfig = PrincipalConfig(user_id="demo-user", tenant_id="demo-tenant")

    @classmethod
    def from_env(cls) -> "VoiceDaemonConfig":
        return cls(
            base_url=os.getenv("MINIGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            wake_phrase=os.getenv("MINIGENT_VOICE_WAKE_PHRASE", "hey minigent").strip(),
            skill_name=_clean_optional(os.getenv("MINIGENT_VOICE_SKILL")),
            thread_id=_clean_optional(os.getenv("MINIGENT_VOICE_THREAD_ID")),
            audio_device=_clean_optional(os.getenv("MINIGENT_VOICE_AUDIO_DEVICE")),
            audio_sample_rate=_int_from_env("MINIGENT_VOICE_AUDIO_SAMPLE_RATE", 16_000),
            audio_block_size=_int_from_env("MINIGENT_VOICE_AUDIO_BLOCK_SIZE", 512),
            speech_silence_ms=_int_from_env("MINIGENT_VOICE_END_SILENCE_MS", 800),
            speech_max_seconds=_float_from_env("MINIGENT_VOICE_MAX_RECORD_SECONDS", 15.0),
            vad_threshold=_float_from_env("MINIGENT_VOICE_VAD_THRESHOLD", 0.5),
            stt_model=os.getenv("MINIGENT_VOICE_STT_MODEL", "gpt-4o-mini-transcribe").strip(),
            openai_api_key=_clean_optional(os.getenv("OPENAI_API_KEY")),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            principal=PrincipalConfig(
                user_id=os.getenv("MINIGENT_VOICE_USER_ID", "demo-user"),
                tenant_id=os.getenv("MINIGENT_VOICE_TENANT_ID", "demo-tenant"),
                is_admin=os.getenv("MINIGENT_VOICE_ADMIN", "").lower() in {"1", "true", "yes"},
                api_token=_clean_optional(os.getenv("MINIGENT_VOICE_API_TOKEN")),
            ),
        )


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _int_from_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _float_from_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    return float(value)
