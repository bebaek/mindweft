from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
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
            "X-Mindweft-User-Id": self.user_id,
            "X-Mindweft-Tenant-Id": self.tenant_id,
            "X-Mindweft-Admin": "true" if self.is_admin else "false",
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
    show_reasoning: bool = False
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
    config_path: str | None = None

    @classmethod
    def from_env(cls, config_path: str | os.PathLike[str] | None = None) -> "ClientConfig":
        file_overrides, loaded_config_path = load_client_config_overrides(config_path)
        config = cls(
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
            wakeword_provider=os.getenv("MINIGENT_VOICE_WAKEWORD_PROVIDER", "porcupine")
            .strip()
            .lower(),
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
            config_path=loaded_config_path,
        )
        return _apply_file_overrides(config, file_overrides)


CLIENT_CONFIG_ENV_BY_FIELD: dict[str, tuple[str, ...]] = {
    "base_url": ("MINIGENT_BASE_URL",),
    "wake_phrase": ("MINIGENT_VOICE_WAKE_PHRASE",),
    "prompt_preamble": ("MINIGENT_VOICE_PROMPT_PREAMBLE",),
    "location": ("MINIGENT_VOICE_LOCATION",),
    "debug_show_prompt": ("MINIGENT_VOICE_DEBUG_SHOW_PROMPT",),
    "stream_runs": ("MINIGENT_CLIENT_STREAM_RUNS",),
    "show_tool_results": ("MINIGENT_CLIENT_SHOW_TOOL_RESULTS",),
    "show_reasoning": ("MINIGENT_CLIENT_SHOW_REASONING",),
    "token_mode": ("MINIGENT_CLIENT_TOKENS",),
    "chat_submit_mode": ("MINIGENT_CLIENT_CHAT_SUBMIT_MODE",),
    "wake_acknowledgement": ("MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT",),
    "wake_acknowledgement_sound": ("MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT_SOUND",),
    "capture_ended_acknowledgement": ("MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT",),
    "capture_ended_acknowledgement_sound": ("MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT_SOUND",),
    "stt_provider": ("MINIGENT_VOICE_STT_PROVIDER",),
    "stt_device": ("MINIGENT_VOICE_STT_DEVICE",),
    "stt_compute_type": ("MINIGENT_VOICE_STT_COMPUTE_TYPE",),
    "stt_language": ("MINIGENT_VOICE_STT_LANGUAGE",),
    "tts_provider": ("MINIGENT_VOICE_TTS_PROVIDER",),
    "tts_voice": ("MINIGENT_VOICE_TTS_VOICE",),
    "tts_model": ("MINIGENT_VOICE_TTS_MODEL",),
    "tts_model_dir": ("MINIGENT_VOICE_TTS_MODEL_DIR",),
    "tts_speaker": ("MINIGENT_VOICE_TTS_SPEAKER",),
    "tts_length_scale": ("MINIGENT_VOICE_TTS_LENGTH_SCALE",),
    "tts_sentence_silence": ("MINIGENT_VOICE_TTS_SENTENCE_SILENCE",),
    "wakeword_provider": ("MINIGENT_VOICE_WAKEWORD_PROVIDER",),
    "skill_name": ("MINIGENT_VOICE_SKILL",),
    "agent_presets": ("MINIGENT_CLIENT_AGENT_PRESETS",),
    "thread_id": ("MINIGENT_VOICE_THREAD_ID",),
    "audio_device": ("MINIGENT_VOICE_AUDIO_DEVICE",),
    "debug_capture_path": ("MINIGENT_VOICE_DEBUG_CAPTURE_PATH",),
    "stt_debug_path": ("MINIGENT_VOICE_STT_DEBUG_PATH",),
    "ducking_mode": ("MINIGENT_VOICE_DUCKING_MODE",),
    "ducked_output_volume": ("MINIGENT_VOICE_DUCKED_OUTPUT_VOLUME",),
    "stt_gain": ("MINIGENT_VOICE_STT_GAIN",),
    "stt_normalize_peak": ("MINIGENT_VOICE_STT_NORMALIZE_PEAK",),
    "stt_normalize_target_peak": ("MINIGENT_VOICE_STT_NORMALIZE_TARGET_PEAK",),
    "audio_sample_rate": ("MINIGENT_VOICE_AUDIO_SAMPLE_RATE",),
    "audio_block_size": ("MINIGENT_VOICE_AUDIO_BLOCK_SIZE",),
    "speech_silence_ms": ("MINIGENT_VOICE_END_SILENCE_MS",),
    "speech_max_seconds": ("MINIGENT_VOICE_MAX_RECORD_SECONDS",),
    "wakeword_cooldown_ms": ("MINIGENT_VOICE_WAKEWORD_COOLDOWN_MS",),
    "post_wake_speech_timeout_ms": ("MINIGENT_VOICE_POST_WAKE_SPEECH_TIMEOUT_MS",),
    "follow_up_timeout_ms": ("MINIGENT_VOICE_FOLLOW_UP_TIMEOUT_MS",),
    "post_wake_settle_ms": ("MINIGENT_VOICE_POST_WAKE_SETTLE_MS",),
    "wakeword_preroll_ms": ("MINIGENT_VOICE_WAKEWORD_PREROLL_MS",),
    "stt_pad_leading_ms": ("MINIGENT_VOICE_STT_PAD_LEADING_MS",),
    "stt_pad_trailing_ms": ("MINIGENT_VOICE_STT_PAD_TRAILING_MS",),
    "vad_threshold": ("MINIGENT_VOICE_VAD_THRESHOLD",),
    "stt_model": ("MINIGENT_VOICE_STT_MODEL",),
    "openai_api_key": ("OPENAI_API_KEY",),
    "openai_base_url": ("OPENAI_BASE_URL",),
    "openrouter_api_key": ("OPENROUTER_API_KEY",),
    "openrouter_base_url": ("OPENROUTER_BASE_URL",),
    "openrouter_http_referer": ("OPENROUTER_HTTP_REFERER",),
    "openrouter_app_name": ("OPENROUTER_APP_NAME",),
    "picovoice_access_key": ("PICOVOICE_ACCESS_KEY",),
    "porcupine_keyword_path": ("MINIGENT_VOICE_KEYWORD_PATH",),
    "openwakeword_model": ("MINIGENT_VOICE_OWW_MODEL",),
    "openwakeword_threshold": ("MINIGENT_VOICE_OWW_THRESHOLD",),
}

PRINCIPAL_CONFIG_ENV_BY_FIELD: dict[str, str] = {
    "user_id": "MINIGENT_VOICE_USER_ID",
    "tenant_id": "MINIGENT_VOICE_TENANT_ID",
    "is_admin": "MINIGENT_VOICE_ADMIN",
    "api_token": "MINIGENT_VOICE_API_TOKEN",
}

VOICE_CONFIG_FIELD_ALIASES: dict[str, str] = {
    "wake_phrase": "wake_phrase",
    "prompt_preamble": "prompt_preamble",
    "location": "location",
    "debug_show_prompt": "debug_show_prompt",
    "skill": "skill_name",
    "skill_name": "skill_name",
    "thread_id": "thread_id",
    "audio_device": "audio_device",
    "debug_capture_path": "debug_capture_path",
    "stt_debug_path": "stt_debug_path",
    "ducking_mode": "ducking_mode",
    "ducked_output_volume": "ducked_output_volume",
    "stt_provider": "stt_provider",
    "stt_device": "stt_device",
    "stt_compute_type": "stt_compute_type",
    "stt_language": "stt_language",
    "stt_model": "stt_model",
    "stt_gain": "stt_gain",
    "stt_normalize_peak": "stt_normalize_peak",
    "stt_normalize_target_peak": "stt_normalize_target_peak",
    "tts_provider": "tts_provider",
    "tts_voice": "tts_voice",
    "tts_model": "tts_model",
    "tts_model_dir": "tts_model_dir",
    "tts_speaker": "tts_speaker",
    "tts_length_scale": "tts_length_scale",
    "tts_sentence_silence": "tts_sentence_silence",
    "wakeword_provider": "wakeword_provider",
    "audio_sample_rate": "audio_sample_rate",
    "audio_block_size": "audio_block_size",
    "speech_silence_ms": "speech_silence_ms",
    "speech_max_seconds": "speech_max_seconds",
    "wakeword_cooldown_ms": "wakeword_cooldown_ms",
    "post_wake_speech_timeout_ms": "post_wake_speech_timeout_ms",
    "follow_up_timeout_ms": "follow_up_timeout_ms",
    "post_wake_settle_ms": "post_wake_settle_ms",
    "wakeword_preroll_ms": "wakeword_preroll_ms",
    "stt_pad_leading_ms": "stt_pad_leading_ms",
    "stt_pad_trailing_ms": "stt_pad_trailing_ms",
    "vad_threshold": "vad_threshold",
}

WAKEWORD_CONFIG_FIELD_ALIASES: dict[str, str] = {
    "provider": "wakeword_provider",
    "keyword_path": "porcupine_keyword_path",
    "porcupine_keyword_path": "porcupine_keyword_path",
    "model": "openwakeword_model",
    "openwakeword_model": "openwakeword_model",
    "threshold": "openwakeword_threshold",
    "openwakeword_threshold": "openwakeword_threshold",
}


CLIENT_CONFIG_ENV = "MINDWEFT_CLIENT_CONFIG"
LEGACY_CLIENT_CONFIG_ENV = "MINIGENT_CLIENT_CONFIG"


def default_client_config_paths() -> tuple[Path, ...]:
    configured_home = os.getenv("XDG_CONFIG_HOME", "").strip()
    config_home = Path(configured_home).expanduser() if configured_home else None
    if config_home is None or not config_home.is_absolute():
        config_home = Path.home() / ".config"
    return (
        config_home / "mindweft" / "client.toml",
        config_home / "minigent" / "client.toml",
        Path.home() / ".minigent" / "client.toml",
        Path.cwd() / ".mindweft-client.toml",
        Path.cwd() / ".minigent-client.toml",
    )


def load_client_config_overrides(
    config_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    explicit_path = (
        config_path
        or _clean_optional(os.getenv(CLIENT_CONFIG_ENV))
        or _clean_optional(os.getenv(LEGACY_CLIENT_CONFIG_ENV))
    )
    if explicit_path is not None:
        path = Path(explicit_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Mindweft client config file not found: {path}")
        return parse_client_config_file(path), str(path)
    for path in default_client_config_paths():
        if path.exists():
            return parse_client_config_file(path), str(path)
    return {}, None


def parse_client_config_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    config_path = Path(path).expanduser()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("Mindweft client config file must contain a TOML table")
    return parse_client_config(raw)


def parse_client_config(raw: dict[str, Any]) -> dict[str, Any]:
    valid_fields = {item.name for item in fields(ClientConfig)} - {
        "principal",
        "extra_headers",
        "config_path",
    }
    overrides: dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"principal", "voice", "agents"}:
            continue
        if key in valid_fields:
            overrides[key] = value

    principal = raw.get("principal")
    if principal is not None:
        if not isinstance(principal, dict):
            raise ValueError("[principal] in Mindweft client config must be a table")
        overrides["principal"] = _parse_principal_config(principal)

    voice = raw.get("voice")
    if voice is not None:
        if not isinstance(voice, dict):
            raise ValueError("[voice] in Mindweft client config must be a table")
        _apply_named_table(voice, VOICE_CONFIG_FIELD_ALIASES, overrides)
        wakeword = voice.get("wakeword")
        if wakeword is not None:
            if not isinstance(wakeword, dict):
                raise ValueError("[voice.wakeword] in Mindweft client config must be a table")
            _apply_named_table(wakeword, WAKEWORD_CONFIG_FIELD_ALIASES, overrides)

    agents = raw.get("agents")
    if agents is not None:
        if not isinstance(agents, dict):
            raise ValueError("[agents] in Mindweft client config must be a table")
        overrides["agent_presets"] = parse_agent_presets(agents)
    return overrides


def _apply_named_table(
    table: dict[str, Any], aliases: dict[str, str], overrides: dict[str, Any]
) -> None:
    for key, value in table.items():
        field_name = aliases.get(key)
        if field_name is not None:
            overrides[field_name] = value


def _parse_principal_config(raw: dict[str, Any]) -> PrincipalConfig:
    return PrincipalConfig(
        user_id=str(raw.get("user_id", "demo-user")),
        tenant_id=str(raw.get("tenant_id", "demo-tenant")),
        is_admin=bool(raw.get("is_admin", False)),
        api_token=_clean_optional(str(raw["api_token"]))
        if raw.get("api_token") is not None
        else None,
    )


def _apply_file_overrides(config: ClientConfig, overrides: dict[str, Any]) -> ClientConfig:
    if not overrides:
        return config
    replace_values: dict[str, Any] = {}
    for field_name, value in overrides.items():
        if field_name == "principal":
            replace_values["principal"] = _merge_principal_file_override(config.principal, value)
            continue
        if any(name in os.environ for name in CLIENT_CONFIG_ENV_BY_FIELD.get(field_name, ())):
            continue
        replace_values[field_name] = _coerce_config_value(field_name, value)
    if not replace_values:
        return config
    return replace(config, **replace_values)


def _merge_principal_file_override(
    env_principal: PrincipalConfig, file_principal: PrincipalConfig
) -> PrincipalConfig:
    values: dict[str, Any] = {}
    for field_name, env_name in PRINCIPAL_CONFIG_ENV_BY_FIELD.items():
        values[field_name] = (
            getattr(env_principal, field_name)
            if env_name in os.environ
            else getattr(file_principal, field_name)
        )
    return PrincipalConfig(**values)


def _coerce_config_value(field_name: str, value: Any) -> Any:
    if field_name == "agent_presets":
        return value if isinstance(value, tuple) else parse_agent_presets(value)
    if field_name in {
        "debug_show_prompt",
        "stream_runs",
        "show_tool_results",
        "show_reasoning",
        "resume_last",
        "stt_normalize_peak",
    }:
        return bool(value)
    if field_name in {
        "tts_speaker",
        "ducked_output_volume",
        "audio_sample_rate",
        "audio_block_size",
        "speech_silence_ms",
        "wakeword_cooldown_ms",
        "post_wake_speech_timeout_ms",
        "follow_up_timeout_ms",
        "post_wake_settle_ms",
        "wakeword_preroll_ms",
        "stt_pad_leading_ms",
        "stt_pad_trailing_ms",
    }:
        return int(value) if value is not None else None
    if field_name in {
        "tts_length_scale",
        "tts_sentence_silence",
        "stt_gain",
        "stt_normalize_target_peak",
        "speech_max_seconds",
        "vad_threshold",
        "openwakeword_threshold",
    }:
        return float(value) if value is not None else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if field_name in {"base_url", "openai_base_url", "openrouter_base_url"}:
            return stripped.rstrip("/")
        return stripped
    return value


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
                raise ValueError(
                    "MINIGENT_CLIENT_AGENT_PRESETS object keys must be non-empty names"
                )
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
