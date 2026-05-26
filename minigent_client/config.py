from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class AgentPreset:
    name: str
    skill_name: str | None = None
    skills: tuple[str, ...] | None = None
    capability_profile: str | None = None
    description: str | None = None


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
class ClientConfig:
    base_url: str
    wake_phrase: str
    prompt_preamble: str | None = None
    location: str | None = None
    debug_show_prompt: bool = False
    stream_runs: bool = False
    show_tool_results: bool = False
    token_mode: str = "auto"
    chat_submit_mode: str = "enter"
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
    tts_length_scale: float | None = None
    tts_sentence_silence: float | None = 0.35
    wakeword_provider: str = "porcupine"
    skill_name: str | None = None
    agent_presets: tuple[AgentPreset, ...] = ()
    thread_id: str | None = None
    resume_last: bool = False
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
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ClientConfig":
        return cls(
            base_url=os.getenv("MINIGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            wake_phrase=os.getenv("MINIGENT_VOICE_WAKE_PHRASE", "hey minigent").strip(),
            prompt_preamble=_clean_optional(os.getenv("MINIGENT_VOICE_PROMPT_PREAMBLE")),
            location=_clean_optional(os.getenv("MINIGENT_VOICE_LOCATION")),
            debug_show_prompt=_bool_from_env("MINIGENT_VOICE_DEBUG_SHOW_PROMPT", False),
            stream_runs=_bool_from_env("MINIGENT_CLIENT_STREAM_RUNS", False),
            show_tool_results=_bool_from_env("MINIGENT_CLIENT_SHOW_TOOL_RESULTS", False),
            token_mode=os.getenv("MINIGENT_CLIENT_TOKENS", "auto").strip().lower(),
            chat_submit_mode=os.getenv("MINIGENT_CLIENT_CHAT_SUBMIT_MODE", "enter").strip().lower(),
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
            tts_length_scale=_optional_float_from_env("MINIGENT_VOICE_TTS_LENGTH_SCALE"),
            tts_sentence_silence=_float_from_env("MINIGENT_VOICE_TTS_SENTENCE_SILENCE", 0.35),
            wakeword_provider=os.getenv(
                "MINIGENT_VOICE_WAKEWORD_PROVIDER", "porcupine"
            ).strip().lower(),
            skill_name=_clean_optional(os.getenv("MINIGENT_VOICE_SKILL")),
            agent_presets=parse_agent_presets_env(os.getenv("MINIGENT_CLIENT_AGENT_PRESETS")),
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


def build_client_config(
    *,
    base_url: str | None = None,
    api_token: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    admin: bool | None = None,
    thread_id: str | None = None,
    skill_name: str | None = None,
    stream_runs: bool | None = None,
    extra_headers: dict[str, str] | None = None,
    wake_phrase: str | None = None,
    env_config: ClientConfig | None = None,
) -> ClientConfig:
    base = env_config or ClientConfig.from_env()
    principal = base.principal
    if any(value is not None for value in (api_token, user_id, tenant_id, admin)):
        principal = PrincipalConfig(
            user_id=user_id or principal.user_id,
            tenant_id=tenant_id or principal.tenant_id,
            is_admin=principal.is_admin if admin is None else admin,
            api_token=api_token if api_token is not None else principal.api_token,
        )
    return replace(
        base,
        base_url=(base_url or base.base_url).rstrip("/"),
        wake_phrase=(wake_phrase or base.wake_phrase).strip(),
        thread_id=thread_id if thread_id is not None else base.thread_id,
        skill_name=skill_name if skill_name is not None else base.skill_name,
        agent_presets=base.agent_presets,
        stream_runs=stream_runs if stream_runs is not None else base.stream_runs,
        principal=principal,
        extra_headers=dict(extra_headers or base.extra_headers),
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


def _optional_float_from_env(name: str) -> float | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return float(stripped)


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


def parse_agent_presets_env(value: str | None) -> tuple[AgentPreset, ...]:
    if value is None or not value.strip():
        return ()
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("MINIGENT_CLIENT_AGENT_PRESETS must be valid JSON") from exc
    return parse_agent_presets(raw)


def parse_agent_presets(raw: object) -> tuple[AgentPreset, ...]:
    if isinstance(raw, dict):
        entries = []
        for name, payload in raw.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("MINIGENT_CLIENT_AGENT_PRESETS object keys must be non-empty names")
            if not isinstance(payload, dict):
                raise ValueError(f"Agent preset '{name}' must be an object")
            entries.append({"name": name, **payload})
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("MINIGENT_CLIENT_AGENT_PRESETS must be a JSON object or array")

    presets: list[AgentPreset] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Agent preset entries must be objects")
        preset = _parse_agent_preset(entry)
        normalized_name = preset.name.casefold()
        if normalized_name in seen:
            raise ValueError(f"Duplicate agent preset '{preset.name}'")
        seen.add(normalized_name)
        presets.append(preset)
    return tuple(presets)


def _parse_agent_preset(entry: dict[Any, Any]) -> AgentPreset:
    name = _required_preset_str(entry.get("name"), "name")
    skill_name = _optional_preset_str(entry.get("skill_name", entry.get("skillName")), "skill_name")
    raw_skills = entry.get("skill_names", entry.get("skillNames", entry.get("skills")))
    skills = _optional_preset_str_tuple(raw_skills, "skill_names")
    if skill_name is not None and skills is not None:
        raise ValueError(f"Agent preset '{name}' cannot set both skill_name and skill_names")
    capability_profile = _optional_preset_str(
        entry.get("capability_profile", entry.get("capabilityProfile")),
        "capability_profile",
    )
    if skill_name is None and skills is None and capability_profile is None:
        raise ValueError(
            f"Agent preset '{name}' must set skill_name, skill_names, or capability_profile"
        )
    description = _optional_preset_str(entry.get("description"), "description")
    return AgentPreset(
        name=name,
        skill_name=skill_name,
        skills=skills,
        capability_profile=capability_profile,
        description=description,
    )


def _required_preset_str(value: object, label: str) -> str:
    parsed = _optional_preset_str(value, label)
    if parsed is None:
        raise ValueError(f"Agent preset {label} must be a non-empty string")
    return parsed


def _optional_preset_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Agent preset {label} must be a string")
    stripped = value.strip()
    return stripped or None


def _optional_preset_str_tuple(value: object, label: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Agent preset {label} must be an array of strings")
    parsed = tuple(item.strip() for item in value if item.strip())
    return parsed or None


def _default_stt_model(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "openrouter":
        return "openai/gpt-audio"
    if normalized == "faster-whisper":
        return "base"
    return "gpt-4o-mini-transcribe"
