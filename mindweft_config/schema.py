from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast


@dataclass(frozen=True)
class AppConfig:
    host: object = None
    port: object = None
    base_url: object = None
    thread_db_path: object = None
    max_iterations: object = None
    tool_timeout_seconds: object = None
    context_compaction_enabled: object = None


@dataclass(frozen=True)
class AuthConfig:
    mode: object = None
    tokens: object = None
    jwt_issuer: object = None
    jwt_audience: object = None
    jwt_shared_secret: object = None
    jwt_jwks_url: object = None
    jwt_jwks_cache_seconds: object = None
    jwt_algorithms: object = None
    jwt_user_claim: object = None
    jwt_tenant_claim: object = None
    jwt_admin_claim: object = None


@dataclass(frozen=True)
class OAuthConfig:
    store_path: object = None
    provider_id: object = None
    client_id: object = None
    authorize_url: object = None
    token_url: object = None
    redirect_uri: object = None
    scope: object = None
    auth_params: object = None
    account_id_jwt_claim: object = None


@dataclass(frozen=True)
class LLMConfig:
    default: object = None
    providers: object = None
    provider: object = None
    model: object = None
    url: object = None
    base_url: object = None
    extra_headers: object = None
    account_id_header: object = None
    api_key_env: object = None
    api_key: object = None
    max_tokens: object = None
    anthropic_version: object = None
    thinking_enabled: object = None
    thinking_budget_tokens: object = None
    thinking_effort: object = None
    prompt_cache_enabled: object = None
    input_modalities: object = None
    timeout: object = None


@dataclass(frozen=True)
class AudioInputConfig:
    enabled: object = None
    max_bytes: object = None
    max_audio_files: object = None
    max_total_bytes: object = None
    max_duration_seconds: object = None
    allowed_mime_types: object = None


@dataclass(frozen=True)
class ImageInputConfig:
    enabled: object = None
    max_bytes: object = None
    max_images: object = None
    max_total_bytes: object = None
    max_pixels: object = None
    max_dimension: object = None
    allowed_mime_types: object = None


@dataclass(frozen=True)
class DocumentInputConfig:
    enabled: object = None
    max_bytes: object = None
    max_documents: object = None
    max_total_bytes: object = None
    max_pages: object = None
    max_text_bytes: object = None
    allowed_mime_types: object = None


@dataclass(frozen=True)
class AttachmentConfig:
    db_path: object = None
    max_per_thread: object = None
    max_bytes_per_thread: object = None
    max_per_tenant: object = None
    max_bytes_per_tenant: object = None
    pending_ttl_seconds: object = None
    cleanup_interval_seconds: object = None


@dataclass(frozen=True)
class RateLimitConfig:
    db_path: object = None
    upload_tenant_capacity: object = None
    upload_tenant_refill_per_second: object = None
    upload_user_capacity: object = None
    upload_user_refill_per_second: object = None
    run_tenant_capacity: object = None
    run_tenant_refill_per_second: object = None
    run_user_capacity: object = None
    run_user_refill_per_second: object = None
    concurrent_run_tenant_capacity: object = None
    concurrent_run_user_capacity: object = None
    concurrent_run_lease_seconds: object = None
    concurrent_run_heartbeat_seconds: object = None


@dataclass(frozen=True)
class CodingConfig:
    enabled: object = None
    tenant_id: object = None
    workspace: object = None
    workspaces: object = None
    default_workspace_scope: object = None
    workspace_scope: object = None
    workspace_scopes: object = None
    inject_workspace_skill: object = None
    shell_enabled: object = None
    shell_allowed_command_prefixes: object = None
    bridge_host: object = None
    bridge_port: object = None
    bridge_allow_globs: object = None
    bridge_deny_globs: object = None
    mcp_gateway_enabled: object = None
    mcp_gateway_port: object = None
    mcp_gateway_path_prefix: object = None
    mcp_server_specs: object = None


@dataclass(frozen=True)
class MCPConfig:
    broker_enabled: object = None
    broker_url: object = None
    servers: object = None


@dataclass(frozen=True)
class VoiceConfig:
    api_token: object = None
    tenant_id: object = None
    user_id: object = None
    thread_id: object = None
    skill: object = None
    location: object = None
    wake_phrase: object = None
    wakeword_provider: object = None
    stt_provider: object = None
    tts_provider: object = None


@dataclass(frozen=True)
class QualityConfig:
    enabled: object = None
    provider: object = None
    model: object = None
    base_url: object = None
    api_key: object = None
    mode: object = None
    timeout: object = None
    max_payload_chars: object = None


@dataclass(frozen=True)
class LoggingConfig:
    level: object = None
    format: object = None


@dataclass(frozen=True)
class AgentSkillsConfig:
    dirs: object = None
    directories: object = None


@dataclass(frozen=True)
class UnifiedConfig:
    profile: object = None
    app: AppConfig = field(default_factory=AppConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    audio_input: AudioInputConfig = field(default_factory=AudioInputConfig)
    image_input: ImageInputConfig = field(default_factory=ImageInputConfig)
    document_input: DocumentInputConfig = field(default_factory=DocumentInputConfig)
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    coding: CodingConfig = field(default_factory=CodingConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent_skills: AgentSkillsConfig = field(default_factory=AgentSkillsConfig)
    peer_agents: object = None
    tenant_execution_configs: object = None
    runtime: object = None


SECTION_FIELDS: dict[str, set[str]] = {
    "app": set(AppConfig.__dataclass_fields__),
    "auth": set(AuthConfig.__dataclass_fields__),
    "oauth": set(OAuthConfig.__dataclass_fields__),
    "llm": set(LLMConfig.__dataclass_fields__),
    "audio_input": set(AudioInputConfig.__dataclass_fields__),
    "image_input": set(ImageInputConfig.__dataclass_fields__),
    "document_input": set(DocumentInputConfig.__dataclass_fields__),
    "attachments": set(AttachmentConfig.__dataclass_fields__),
    "rate_limits": set(RateLimitConfig.__dataclass_fields__),
    "coding": set(CodingConfig.__dataclass_fields__),
    "mcp": set(MCPConfig.__dataclass_fields__),
    "voice": set(VoiceConfig.__dataclass_fields__),
    "quality": set(QualityConfig.__dataclass_fields__),
    "logging": set(LoggingConfig.__dataclass_fields__),
    "agent_skills": set(AgentSkillsConfig.__dataclass_fields__),
}

TOP_LEVEL_KEYS = {
    "profile",
    "app",
    "auth",
    "oauth",
    "llm",
    "audio_input",
    "image_input",
    "document_input",
    "attachments",
    "rate_limits",
    "coding",
    "mcp",
    "voice",
    "quality",
    "logging",
    "agent_skills",
    "peer_agents",
    "tenant_execution_configs",
    "runtime",
}

SECTION_TYPES: dict[str, type[Any]] = {
    "app": AppConfig,
    "auth": AuthConfig,
    "oauth": OAuthConfig,
    "llm": LLMConfig,
    "audio_input": AudioInputConfig,
    "image_input": ImageInputConfig,
    "document_input": DocumentInputConfig,
    "attachments": AttachmentConfig,
    "rate_limits": RateLimitConfig,
    "coding": CodingConfig,
    "mcp": MCPConfig,
    "voice": VoiceConfig,
    "quality": QualityConfig,
    "logging": LoggingConfig,
    "agent_skills": AgentSkillsConfig,
}

_STRING_KEYS = {
    "profile",
    "app.host",
    "app.base_url",
    "app.thread_db_path",
    "auth.mode",
    "auth.jwt_issuer",
    "auth.jwt_audience",
    "auth.jwt_shared_secret",
    "auth.jwt_jwks_url",
    "auth.jwt_algorithms",
    "auth.jwt_user_claim",
    "auth.jwt_tenant_claim",
    "auth.jwt_admin_claim",
    "oauth.store_path",
    "rate_limits.db_path",
    "oauth.provider_id",
    "oauth.client_id",
    "oauth.authorize_url",
    "oauth.token_url",
    "oauth.redirect_uri",
    "oauth.scope",
    "oauth.account_id_jwt_claim",
    "llm.default",
    "llm.provider",
    "llm.model",
    "llm.url",
    "llm.base_url",
    "llm.account_id_header",
    "llm.api_key_env",
    "llm.api_key",
    "llm.anthropic_version",
    "llm.thinking_effort",
    "coding.tenant_id",
    "coding.workspace",
    "coding.default_workspace_scope",
    "coding.workspace_scope",
    "coding.bridge_host",
    "coding.mcp_gateway_path_prefix",
    "mcp.broker_url",
    "voice.api_token",
    "voice.tenant_id",
    "voice.user_id",
    "voice.thread_id",
    "voice.skill",
    "voice.location",
    "voice.wake_phrase",
    "voice.wakeword_provider",
    "voice.stt_provider",
    "voice.tts_provider",
    "quality.provider",
    "quality.model",
    "quality.base_url",
    "quality.api_key",
    "quality.mode",
    "logging.level",
    "logging.format",
}

_INT_KEYS = {
    "app.port",
    "app.max_iterations",
    "auth.jwt_jwks_cache_seconds",
    "llm.max_tokens",
    "llm.thinking_budget_tokens",
    "image_input.max_bytes",
    "image_input.max_images",
    "image_input.max_total_bytes",
    "image_input.max_pixels",
    "image_input.max_dimension",
    "document_input.max_bytes",
    "document_input.max_documents",
    "document_input.max_total_bytes",
    "document_input.max_pages",
    "document_input.max_text_bytes",
    "attachments.max_per_thread",
    "attachments.max_bytes_per_thread",
    "attachments.max_per_tenant",
    "attachments.max_bytes_per_tenant",
    "attachments.pending_ttl_seconds",
    "attachments.cleanup_interval_seconds",
    "rate_limits.upload_tenant_capacity",
    "rate_limits.upload_user_capacity",
    "rate_limits.run_tenant_capacity",
    "rate_limits.run_user_capacity",
    "rate_limits.concurrent_run_tenant_capacity",
    "rate_limits.concurrent_run_user_capacity",
    "rate_limits.concurrent_run_lease_seconds",
    "rate_limits.concurrent_run_heartbeat_seconds",
    "coding.bridge_port",
    "coding.mcp_gateway_port",
    "quality.max_payload_chars",
}

_NUMBER_KEYS = {
    "app.tool_timeout_seconds",
    "llm.timeout",
    "quality.timeout",
    "rate_limits.upload_tenant_refill_per_second",
    "rate_limits.upload_user_refill_per_second",
    "rate_limits.run_tenant_refill_per_second",
    "rate_limits.run_user_refill_per_second",
}

_BOOL_KEYS = {
    "app.context_compaction_enabled",
    "llm.thinking_enabled",
    "llm.prompt_cache_enabled",
    "image_input.enabled",
    "document_input.enabled",
    "coding.enabled",
    "coding.inject_workspace_skill",
    "coding.shell_enabled",
    "coding.mcp_gateway_enabled",
    "mcp.broker_enabled",
    "quality.enabled",
}

_STRING_LIST_KEYS = {
    "image_input.allowed_mime_types",
    "document_input.allowed_mime_types",
    "llm.input_modalities",
    "coding.workspaces",
    "coding.shell_allowed_command_prefixes",
    "coding.bridge_allow_globs",
    "coding.bridge_deny_globs",
    "agent_skills.dirs",
    "agent_skills.directories",
}

_DICT_KEYS = {
    "auth.tokens",
    "oauth.auth_params",
    "llm.extra_headers",
    "llm.providers",
    "coding.workspace_scopes",
}

_LIST_KEYS = {
    "mcp.servers",
    "coding.mcp_server_specs",
    "peer_agents",
}

_DICT_OR_LIST_KEYS = {
    "tenant_execution_configs",
    "runtime",
}


def parse_unified_config(data: dict[str, Any]) -> UnifiedConfig:
    errors = validate_unified_config_data(data)
    if errors:
        raise ValueError("; ".join(errors))
    sections: dict[str, Any] = {}
    for section_name, section_type in SECTION_TYPES.items():
        raw_section = data.get(section_name)
        sections[section_name] = (
            section_type(**raw_section) if isinstance(raw_section, dict) else section_type()
        )
    return UnifiedConfig(
        profile=data.get("profile"),
        peer_agents=cast(object, data.get("peer_agents")),
        tenant_execution_configs=cast(object, data.get("tenant_execution_configs")),
        runtime=cast(object, data.get("runtime")),
        **sections,
    )


def validate_unified_config_data(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["config root must be a TOML table"]
    errors: list[str] = []
    for key in data:
        if key not in TOP_LEVEL_KEYS:
            errors.append(f"unknown top-level key: {key}")
    for section_name, allowed_keys in SECTION_FIELDS.items():
        raw_section = data.get(section_name)
        if raw_section is None:
            continue
        if not isinstance(raw_section, dict):
            errors.append(f"{section_name} must be a table")
            continue
        for key, value in raw_section.items():
            dotted = f"{section_name}.{key}"
            if key not in allowed_keys:
                errors.append(f"unknown key: {dotted}")
                continue
            if dotted == "coding.workspace_scopes":
                errors.extend(_validate_workspace_scopes(value))
                continue
            if dotted == "llm.providers":
                errors.extend(_validate_llm_providers(value))
                continue
            errors.extend(_validate_value_type(dotted, value))
    for key in ("profile", "peer_agents", "tenant_execution_configs", "runtime"):
        if key in data:
            errors.extend(_validate_value_type(key, data[key]))
    errors.extend(_validate_llm_section_shape(data.get("llm")))
    return errors


def _validate_llm_section_shape(value: object) -> list[str]:
    if not isinstance(value, dict) or "providers" not in value:
        return []
    legacy_keys = set(value) - {"default", "providers"}
    if legacy_keys:
        return [
            "llm.providers cannot be combined with legacy llm keys: "
            + ", ".join(sorted(legacy_keys))
        ]
    providers = value.get("providers")
    default = value.get("default")
    if isinstance(providers, dict) and len(providers) > 1 and not default:
        return ["llm.default is required when llm.providers contains multiple profiles"]
    if isinstance(default, str) and isinstance(providers, dict) and default not in providers:
        return [f"llm.default references unknown provider profile: {default}"]
    return []


def _validate_llm_providers(value: object) -> list[str]:
    if not isinstance(value, dict) or not value:
        return ["llm.providers must be a non-empty table"]
    errors: list[str] = []
    allowed = {
        "provider",
        "model",
        "url",
        "base_url",
        "extra_headers",
        "api_key_env",
        "api_key",
        "max_tokens",
        "anthropic_version",
        "thinking_enabled",
        "thinking_budget_tokens",
        "thinking_effort",
        "prompt_cache_enabled",
        "input_modalities",
        "timeout",
    }
    for name, provider in value.items():
        if not isinstance(name, str) or not name.strip():
            errors.append("llm.providers profile names must be non-empty strings")
            continue
        if not isinstance(provider, dict):
            errors.append(f"llm.providers.{name} must be a table")
            continue
        for key, item in provider.items():
            if key not in allowed:
                errors.append(f"unknown key: llm.providers.{name}.{key}")
                continue
            errors.extend(_validate_value_type(f"llm.{key}", item))
        if not str(provider.get("provider", "")).strip():
            errors.append(f"llm.providers.{name}.provider is required")
    return errors


def _validate_workspace_scopes(value: object) -> list[str]:
    if not isinstance(value, dict):
        return ["coding.workspace_scopes must be a table/object"]
    errors: list[str] = []
    for name, scope in value.items():
        if not isinstance(name, str) or not name:
            errors.append("coding.workspace_scopes names must be non-empty strings")
            continue
        prefix = f"coding.workspace_scopes.{name}"
        if not isinstance(scope, dict):
            errors.append(f"{prefix} must be a table/object")
            continue
        unknown_keys = sorted(set(scope) - {"roots", "description"})
        for key in unknown_keys:
            errors.append(f"unknown key: {prefix}.{key}")
        roots = scope.get("roots")
        if (
            not isinstance(roots, list)
            or not roots
            or not all(isinstance(item, str) for item in roots)
        ):
            errors.append(f"{prefix}.roots must be a non-empty list of strings")
        if "description" in scope and not isinstance(scope["description"], str):
            errors.append(f"{prefix}.description must be a string")
    return errors


def _validate_value_type(dotted: str, value: object) -> list[str]:
    if dotted in _STRING_KEYS:
        return [] if isinstance(value, str) else [f"{dotted} must be a string"]
    if dotted in _INT_KEYS:
        return (
            []
            if isinstance(value, int) and not isinstance(value, bool)
            else [f"{dotted} must be an integer"]
        )
    if dotted in _NUMBER_KEYS:
        return (
            []
            if isinstance(value, int | float) and not isinstance(value, bool)
            else [f"{dotted} must be a number"]
        )
    if dotted in _BOOL_KEYS:
        return [] if isinstance(value, bool) else [f"{dotted} must be a boolean"]
    if dotted in _STRING_LIST_KEYS:
        if isinstance(value, str):
            return []
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return []
        return [f"{dotted} must be a string or list of strings"]
    if dotted in _DICT_KEYS:
        return [] if isinstance(value, dict) else [f"{dotted} must be a table/object"]
    if dotted in _LIST_KEYS:
        return [] if isinstance(value, list) else [f"{dotted} must be a list"]
    if dotted in _DICT_OR_LIST_KEYS:
        return (
            [] if isinstance(value, dict | list) else [f"{dotted} must be a table/object or list"]
        )
    return []
