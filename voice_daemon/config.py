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
    wake_acknowledgement: str | None = None
    wake_acknowledgement_sound: str | None = None
    capture_ended_acknowledgement: str | None = None
    capture_ended_acknowledgement_sound: str | None = None
    stt_provider: str = "openai"
    stt_device: str | None = None
    stt_compute_type: str | None = None
    stt_language: str | None = None
    tts_provider: str = "console"
    tts_voice: str | None = None
    tts_model: str | None = None
    tts_model_dir: str | None = None
    tts_speaker: int | None = None
    wakeword_provider: str = "porcupine"
    skill_name: str | None = None
    thread_id: str | None = None
    audio_device: str | None = None
    debug_capture_path: str | None = None
    stt_debug_path: str | None = None
    ducking_mode: str = "off"
    ducked_output_volume: int = 20
    stt_gain: float = 1.0
    stt_normalize_peak: bool = False
    stt_normalize_target_peak: float = 0.8
    audio_sample_rate: int = 16_000
    audio_block_size: int = 512
    speech_silence_ms: int = 800
    speech_max_seconds: float = 15.0
    wakeword_cooldown_ms: int = 1500
    post_wake_speech_timeout_ms: int = 2500
    follow_up_timeout_ms: int = 0
    post_wake_settle_ms: int = 250
    wakeword_preroll_ms: int = 750
    stt_pad_leading_ms: int = 250
    stt_pad_trailing_ms: int = 500
    vad_threshold: float = 0.5
    stt_model: str = "gpt-4o-mini-transcribe"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_http_referer: str | None = None
    openrouter_app_name: str | None = None
    picovoice_access_key: str | None = None
    porcupine_keyword_path: str | None = None
    openwakeword_model: str = "okay_nabu"
    openwakeword_threshold: float = 0.5
    principal: PrincipalConfig = PrincipalConfig(user_id="demo-user", tenant_id="demo-tenant")

    @classmethod
    def from_env(cls) -> "VoiceDaemonConfig":
        return cls(
            base_url=os.getenv("MINIGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            wake_phrase=os.getenv("MINIGENT_VOICE_WAKE_PHRASE", "hey minigent").strip(),
            wake_acknowledgement=_clean_optional(os.getenv("MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT")),
            wake_acknowledgement_sound=_clean_optional(
                os.getenv("MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT_SOUND")
            ),
            capture_ended_acknowledgement=_clean_optional(
                os.getenv("MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT")
            ),
            capture_ended_acknowledgement_sound=_clean_optional(
                os.getenv("MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT_SOUND")
            ),
            stt_provider=os.getenv("MINIGENT_VOICE_STT_PROVIDER", "openai").strip().lower(),
            stt_device=_clean_optional(os.getenv("MINIGENT_VOICE_STT_DEVICE")),
            stt_compute_type=_clean_optional(os.getenv("MINIGENT_VOICE_STT_COMPUTE_TYPE")),
            stt_language=_clean_optional(os.getenv("MINIGENT_VOICE_STT_LANGUAGE")),
            tts_provider=os.getenv("MINIGENT_VOICE_TTS_PROVIDER", "console").strip().lower(),
            tts_voice=_clean_optional(os.getenv("MINIGENT_VOICE_TTS_VOICE")),
            tts_model=_clean_optional(os.getenv("MINIGENT_VOICE_TTS_MODEL")),
            tts_model_dir=_clean_optional(os.getenv("MINIGENT_VOICE_TTS_MODEL_DIR")),
            tts_speaker=_optional_int_from_env("MINIGENT_VOICE_TTS_SPEAKER"),
            wakeword_provider=os.getenv(
                "MINIGENT_VOICE_WAKEWORD_PROVIDER", "porcupine"
            ).strip().lower(),
            skill_name=_clean_optional(os.getenv("MINIGENT_VOICE_SKILL")),
            thread_id=_clean_optional(os.getenv("MINIGENT_VOICE_THREAD_ID")),
            audio_device=_clean_optional(os.getenv("MINIGENT_VOICE_AUDIO_DEVICE")),
            debug_capture_path=_clean_optional(os.getenv("MINIGENT_VOICE_DEBUG_CAPTURE_PATH")),
            stt_debug_path=_clean_optional(os.getenv("MINIGENT_VOICE_STT_DEBUG_PATH")),
            ducking_mode=os.getenv("MINIGENT_VOICE_DUCKING_MODE", "off").strip().lower(),
            ducked_output_volume=_bounded_int_from_env(
                "MINIGENT_VOICE_DUCKED_OUTPUT_VOLUME", 20, minimum=0, maximum=100
            ),
            stt_gain=_float_from_env("MINIGENT_VOICE_STT_GAIN", 1.0),
            stt_normalize_peak=_bool_from_env("MINIGENT_VOICE_STT_NORMALIZE_PEAK", False),
            stt_normalize_target_peak=_float_from_env(
                "MINIGENT_VOICE_STT_NORMALIZE_TARGET_PEAK", 0.8
            ),
            audio_sample_rate=_int_from_env("MINIGENT_VOICE_AUDIO_SAMPLE_RATE", 16_000),
            audio_block_size=_int_from_env("MINIGENT_VOICE_AUDIO_BLOCK_SIZE", 512),
            speech_silence_ms=_int_from_env("MINIGENT_VOICE_END_SILENCE_MS", 800),
            speech_max_seconds=_float_from_env("MINIGENT_VOICE_MAX_RECORD_SECONDS", 15.0),
            wakeword_cooldown_ms=_int_from_env("MINIGENT_VOICE_WAKEWORD_COOLDOWN_MS", 1500),
            post_wake_speech_timeout_ms=_int_from_env(
                "MINIGENT_VOICE_POST_WAKE_SPEECH_TIMEOUT_MS", 2500
            ),
            follow_up_timeout_ms=_int_from_env("MINIGENT_VOICE_FOLLOW_UP_TIMEOUT_MS", 0),
            post_wake_settle_ms=_int_from_env("MINIGENT_VOICE_POST_WAKE_SETTLE_MS", 250),
            wakeword_preroll_ms=_int_from_env("MINIGENT_VOICE_WAKEWORD_PREROLL_MS", 750),
            stt_pad_leading_ms=_int_from_env("MINIGENT_VOICE_STT_PAD_LEADING_MS", 250),
            stt_pad_trailing_ms=_int_from_env("MINIGENT_VOICE_STT_PAD_TRAILING_MS", 500),
            vad_threshold=_float_from_env("MINIGENT_VOICE_VAD_THRESHOLD", 0.5),
            stt_model=os.getenv(
                "MINIGENT_VOICE_STT_MODEL",
                _default_stt_model(os.getenv("MINIGENT_VOICE_STT_PROVIDER", "openai")),
            ).strip(),
            openai_api_key=_clean_optional(os.getenv("OPENAI_API_KEY")),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            openrouter_api_key=_clean_optional(os.getenv("OPENROUTER_API_KEY")),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            openrouter_http_referer=_clean_optional(os.getenv("OPENROUTER_HTTP_REFERER")),
            openrouter_app_name=_clean_optional(os.getenv("OPENROUTER_APP_NAME")),
            picovoice_access_key=_clean_optional(os.getenv("PICOVOICE_ACCESS_KEY")),
            porcupine_keyword_path=_clean_optional(os.getenv("MINIGENT_VOICE_KEYWORD_PATH")),
            openwakeword_model=os.getenv("MINIGENT_VOICE_OWW_MODEL", "okay_nabu").strip(),
            openwakeword_threshold=_float_from_env("MINIGENT_VOICE_OWW_THRESHOLD", 0.5),
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


def _optional_int_from_env(name: str) -> int | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return int(stripped)


def _bool_from_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int_from_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = _int_from_env(name, default)
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _default_stt_model(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "openrouter":
        return "openai/gpt-audio"
    if normalized == "faster-whisper":
        return "base"
    return "gpt-4o-mini-transcribe"
