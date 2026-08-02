from __future__ import annotations

import copy
import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.agent_skills import discover_agent_skills
from app.unified_config_schema import parse_unified_config

CONFIG_FILE_ENV = "MINIGENT_CONFIG_FILE"
DOTENV_FILE_ENV = "MINIGENT_DOTENV_FILE"
CONFIG_DISCOVERY_ENV = "MINIGENT_CONFIG_DISCOVERY"
XDG_CONFIG_HOME_ENV = "XDG_CONFIG_HOME"
DEFAULT_CONFIG_FILE = "minigent.toml"
DEFAULT_USER_CONFIG_DIR = "minigent"
DEFAULT_DOTENV_FILE = ".env"
DEFAULT_CODING_DOTENV_FILE = ".env.coding"
DEFAULT_THREAD_DB_PATH = ".data/minigent-threads.db"
DEFAULT_CODING_THREAD_DB_PATH = ".data/minigent-coding-threads.db"
DEFAULT_VOICE_THREAD_DB_PATH = ".data/minigent-voice-threads.db"
LLM_PROFILES_ENV = "MINIGENT_LLM_PROFILES"
LLM_DEFAULT_PROFILE_ENV = "MINIGENT_LLM_DEFAULT_PROFILE"


@dataclass(frozen=True)
class ResolvedConfig:
    """Resolved startup configuration and the files that contributed to it.

    ``env`` contains the normalized environment mapping produced from the unified
    TOML config and selected dotenv file. Real process-environment values are not
    included unless they are needed to project TOML settings such as ``api_key_env``.
    """

    env: dict[str, str]
    config_path: Path | None
    dotenv_path: Path | None


_SIMPLE_SECTION_ENV_MAP: dict[str, dict[str, str]] = {
    "app": {
        "host": "MINIGENT_HOST",
        "port": "MINIGENT_PORT",
        "base_url": "MINIGENT_BASE_URL",
        "thread_db_path": "MINIGENT_THREAD_DB_PATH",
        "max_iterations": "MINIGENT_MAX_ITERATIONS",
        "tool_timeout_seconds": "MINIGENT_TOOL_TIMEOUT_SECONDS",
        "context_compaction_enabled": "MINIGENT_CONTEXT_COMPACTION_ENABLED",
    },
    "auth": {
        "mode": "MINIGENT_AUTH_MODE",
        "tokens": "MINIGENT_AUTH_TOKENS",
        "jwt_issuer": "MINIGENT_JWT_ISSUER",
        "jwt_audience": "MINIGENT_JWT_AUDIENCE",
        "jwt_shared_secret": "MINIGENT_JWT_SHARED_SECRET",
        "jwt_jwks_url": "MINIGENT_JWT_JWKS_URL",
        "jwt_jwks_cache_seconds": "MINIGENT_JWT_JWKS_CACHE_SECONDS",
        "jwt_algorithms": "MINIGENT_JWT_ALGORITHMS",
        "jwt_user_claim": "MINIGENT_JWT_USER_CLAIM",
        "jwt_tenant_claim": "MINIGENT_JWT_TENANT_CLAIM",
        "jwt_admin_claim": "MINIGENT_JWT_ADMIN_CLAIM",
    },
    "oauth": {
        "store_path": "MINIGENT_OAUTH_STORE_PATH",
        "provider_id": "MINIGENT_OAUTH_PROVIDER_ID",
        "client_id": "MINIGENT_OAUTH_CLIENT_ID",
        "authorize_url": "MINIGENT_OAUTH_AUTHORIZE_URL",
        "token_url": "MINIGENT_OAUTH_TOKEN_URL",
        "redirect_uri": "MINIGENT_OAUTH_REDIRECT_URI",
        "scope": "MINIGENT_OAUTH_SCOPE",
        "auth_params": "MINIGENT_OAUTH_AUTH_PARAMS",
        "account_id_jwt_claim": "MINIGENT_OAUTH_ACCOUNT_ID_JWT_CLAIM",
    },
    "image_input": {
        "enabled": "MINIGENT_IMAGE_INPUT_ENABLED",
        "max_bytes": "MINIGENT_IMAGE_INPUT_MAX_BYTES",
        "max_images": "MINIGENT_IMAGE_INPUT_MAX_IMAGES",
        "max_total_bytes": "MINIGENT_IMAGE_INPUT_MAX_TOTAL_BYTES",
        "max_pixels": "MINIGENT_IMAGE_INPUT_MAX_PIXELS",
        "max_dimension": "MINIGENT_IMAGE_INPUT_MAX_DIMENSION",
        "allowed_mime_types": "MINIGENT_IMAGE_INPUT_ALLOWED_MIME_TYPES",
    },
    "attachments": {
        "db_path": "MINIGENT_ATTACHMENT_DB_PATH",
        "max_per_thread": "MINIGENT_ATTACHMENT_MAX_PER_THREAD",
        "max_bytes_per_thread": "MINIGENT_ATTACHMENT_MAX_BYTES_PER_THREAD",
        "max_per_tenant": "MINIGENT_ATTACHMENT_MAX_PER_TENANT",
        "max_bytes_per_tenant": "MINIGENT_ATTACHMENT_MAX_BYTES_PER_TENANT",
        "pending_ttl_seconds": "MINIGENT_ATTACHMENT_PENDING_TTL_SECONDS",
        "cleanup_interval_seconds": "MINIGENT_ATTACHMENT_CLEANUP_INTERVAL_SECONDS",
    },
    "rate_limits": {
        "db_path": "MINIGENT_RATE_LIMIT_DB_PATH",
        "upload_tenant_capacity": "MINIGENT_UPLOAD_RATE_LIMIT_TENANT_CAPACITY",
        "upload_tenant_refill_per_second": ("MINIGENT_UPLOAD_RATE_LIMIT_TENANT_REFILL_PER_SECOND"),
        "upload_user_capacity": "MINIGENT_UPLOAD_RATE_LIMIT_USER_CAPACITY",
        "upload_user_refill_per_second": "MINIGENT_UPLOAD_RATE_LIMIT_USER_REFILL_PER_SECOND",
        "run_tenant_capacity": "MINIGENT_RUN_RATE_LIMIT_TENANT_CAPACITY",
        "run_tenant_refill_per_second": "MINIGENT_RUN_RATE_LIMIT_TENANT_REFILL_PER_SECOND",
        "run_user_capacity": "MINIGENT_RUN_RATE_LIMIT_USER_CAPACITY",
        "run_user_refill_per_second": "MINIGENT_RUN_RATE_LIMIT_USER_REFILL_PER_SECOND",
    },
    "coding": {
        "enabled": "MINIGENT_CODING_MCP_GATEWAY_ENABLED",
        "tenant_id": "MINIGENT_CODING_TENANT_ID",
        "workspace": "MINIGENT_CODING_WORKSPACE",
        "workspaces": "MINIGENT_CODING_WORKSPACES",
        "default_workspace_scope": "MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE",
        "workspace_scope": "MINIGENT_CODING_WORKSPACE_SCOPE",
        "inject_workspace_skill": "MINIGENT_CODING_INJECT_WORKSPACE_SKILL",
        "shell_enabled": "MINIGENT_CODING_SHELL_ENABLED",
        "shell_allowed_command_prefixes": "MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES",
        "bridge_host": "MINIGENT_CODING_BRIDGE_HOST",
        "bridge_port": "MINIGENT_CODING_BRIDGE_PORT",
        "bridge_allow_globs": "MINIGENT_CODING_BRIDGE_ALLOW_GLOBS",
        "bridge_deny_globs": "MINIGENT_CODING_BRIDGE_DENY_GLOBS",
    },
    "voice": {
        "api_token": "MINIGENT_VOICE_API_TOKEN",
        "tenant_id": "MINIGENT_VOICE_TENANT_ID",
        "user_id": "MINIGENT_VOICE_USER_ID",
        "thread_id": "MINIGENT_VOICE_THREAD_ID",
        "skill": "MINIGENT_VOICE_SKILL",
        "location": "MINIGENT_VOICE_LOCATION",
        "wake_phrase": "MINIGENT_VOICE_WAKE_PHRASE",
        "wakeword_provider": "MINIGENT_VOICE_WAKEWORD_PROVIDER",
        "stt_provider": "MINIGENT_VOICE_STT_PROVIDER",
        "tts_provider": "MINIGENT_VOICE_TTS_PROVIDER",
    },
    "quality": {
        "enabled": "MINIGENT_REMOTE_QUALITY_ENABLED",
        "provider": "MINIGENT_REMOTE_QUALITY_PROVIDER",
        "model": "MINIGENT_REMOTE_QUALITY_MODEL",
        "base_url": "MINIGENT_REMOTE_QUALITY_BASE_URL",
        "api_key": "MINIGENT_REMOTE_QUALITY_API_KEY",
        "mode": "MINIGENT_REMOTE_QUALITY_MODE",
        "timeout": "MINIGENT_REMOTE_QUALITY_TIMEOUT",
        "max_payload_chars": "MINIGENT_REMOTE_QUALITY_MAX_PAYLOAD_CHARS",
    },
    "logging": {
        "level": "MINIGENT_LOG_LEVEL",
        "format": "MINIGENT_LOG_FORMAT",
    },
}


def apply_startup_config(
    *, cwd: Path | None = None, discover_default_files: bool | None = None
) -> None:
    """Load minigent.toml and .env into process env without overriding real env vars.

    Precedence is: existing process environment > selected .env > minigent.toml > built-in defaults.
    That keeps deployment overrides compatible while allowing a single friendly config file
    to replace most local .env entries. Cwd-local ``minigent.toml`` wins over the user-level
    ``$XDG_CONFIG_HOME/minigent/minigent.toml`` (or ``~/.config/minigent/minigent.toml``).
    Set MINIGENT_DOTENV_FILE to use a dotenv file other than .env. Set
    ``discover_default_files`` to ``False`` for isolated subprocesses/tests that must ignore
    cwd-local and user-level config files unless explicit paths are configured.
    """

    base_dir = Path.cwd() if cwd is None else cwd
    initial_keys = set(os.environ)
    resolved = resolve_unified_config(
        base_dir=base_dir,
        env=dict(os.environ),
        discover_default_files=discover_default_files,
    )
    config_env = dict(resolved.env)
    dotenv_env = _load_dotenv_values(resolved.dotenv_path)
    for key in dotenv_env:
        config_env.pop(key, None)
    _apply_env(config_env, protected_keys=initial_keys)

    # The selected dotenv file is a local override for the unified config, but still must
    # not override real env.
    _apply_env(dotenv_env, protected_keys=initial_keys)


def resolve_unified_config(
    *,
    base_dir: Path,
    env: dict[str, str] | None = None,
    discover_default_files: bool | None = None,
) -> ResolvedConfig:
    """Resolve unified TOML and dotenv configuration into an env mapping.

    Explicit ``MINIGENT_CONFIG_FILE``/``MINIGENT_DOTENV_FILE`` paths are honored even when
    default discovery is disabled. With discovery enabled, a cwd-local ``minigent.toml``
    takes precedence over the user-level XDG config. When ``discover_default_files`` is
    ``None``, discovery defaults to enabled unless ``MINIGENT_CONFIG_DISCOVERY`` is set to
    an off-like value (``0``, ``false``, ``no``, ``off``, ``disabled``, or ``explicit``).
    """

    lookup_env = dict(os.environ if env is None else env)
    discover_defaults = _should_discover_default_files(
        lookup_env, discover_default_files=discover_default_files
    )
    dotenv_path = resolve_dotenv_path(
        base_dir=base_dir, env=lookup_env, discover_default_file=discover_defaults
    )
    dotenv_env = _load_dotenv_values(dotenv_path)
    source_env = dict(lookup_env)
    source_env.update(dotenv_env)
    config_path = resolve_config_path(
        base_dir=base_dir, env=source_env, discover_default_file=discover_defaults
    )
    config_env = load_unified_config_env(config_path, source_env=source_env)
    resolved_env = dict(config_env)
    resolved_env.update(dotenv_env)
    return ResolvedConfig(env=resolved_env, config_path=config_path, dotenv_path=dotenv_path)


def apply_unified_config_to_env(
    env: dict[str, str], *, base_dir: Path, discover_default_files: bool | None = None
) -> None:
    """Apply minigent.toml-style settings to an env mapping used for child processes.

    Values already present in the mapping win. This is used by helper launchers that build
    an explicit child-process environment rather than relying on os.environ mutation.
    """

    resolved = resolve_unified_config(
        base_dir=base_dir, env=env, discover_default_files=discover_default_files
    )
    dotenv_keys = set(_load_dotenv_values(resolved.dotenv_path))
    for key, value in resolved.env.items():
        if key not in dotenv_keys:
            env.setdefault(key, value)
    for key in dotenv_keys:
        env.setdefault(key, resolved.env[key])


def _load_dotenv_values(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def _should_discover_default_files(
    env: dict[str, str], *, discover_default_files: bool | None
) -> bool:
    if discover_default_files is not None:
        return discover_default_files
    value = env.get(CONFIG_DISCOVERY_ENV, "").strip().lower()
    if value in {"0", "false", "no", "off", "disabled", "explicit"}:
        return False
    return True


def resolve_dotenv_path(
    *,
    base_dir: Path,
    env: dict[str, str] | None = None,
    discover_default_file: bool = True,
) -> Path | None:
    lookup_env = os.environ if env is None else env
    configured = lookup_env.get(DOTENV_FILE_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else base_dir / path
    if not discover_default_file:
        return None
    default_path = base_dir / DEFAULT_DOTENV_FILE
    return default_path if default_path.exists() else None


def default_user_config_path(env: Mapping[str, str] | None = None) -> Path:
    lookup_env = os.environ if env is None else env
    configured_home = lookup_env.get(XDG_CONFIG_HOME_ENV, "").strip()
    if configured_home:
        config_home = Path(configured_home).expanduser()
        if config_home.is_absolute():
            return config_home / DEFAULT_USER_CONFIG_DIR / DEFAULT_CONFIG_FILE

    configured_user_home = lookup_env.get("HOME", "").strip()
    user_home = Path(configured_user_home).expanduser() if configured_user_home else Path.home()
    return user_home / ".config" / DEFAULT_USER_CONFIG_DIR / DEFAULT_CONFIG_FILE


def resolve_config_path(
    *,
    base_dir: Path,
    env: dict[str, str] | None = None,
    discover_default_file: bool = True,
) -> Path | None:
    lookup_env = os.environ if env is None else env
    configured = lookup_env.get(CONFIG_FILE_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else base_dir / path
    if not discover_default_file:
        return None
    local_path = base_dir / DEFAULT_CONFIG_FILE
    if local_path.exists():
        return local_path
    user_path = default_user_config_path(lookup_env)
    return user_path if user_path.exists() else None


def load_unified_config_env(
    path: Path | None,
    *,
    source_env: dict[str, str] | None = None,
) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    with path.open("rb") as file:
        data = tomllib.load(file)
    parse_unified_config(data)
    env: dict[str, str] = {}
    _collect_simple_sections(data, env)
    _collect_llm_config(data.get("llm"), env, source_env=source_env)
    _collect_coding_inline_config(data.get("coding"), env)
    _collect_mcp_config(data.get("mcp"), env)
    _collect_peer_agents(data.get("peer_agents"), env)
    _collect_tenant_execution_configs(
        data.get("tenant_execution_configs"),
        env,
        llm_section=data.get("llm"),
        coding_section=data.get("coding"),
        agent_skills_section=data.get("agent_skills"),
        base_dir=path.parent,
    )
    return env


def _apply_env(values: dict[str, str], *, protected_keys: set[str]) -> None:
    for key, value in values.items():
        if key not in protected_keys:
            os.environ[key] = value


def _collect_simple_sections(data: dict[str, Any], env: dict[str, str]) -> None:
    for section_name, mapping in _SIMPLE_SECTION_ENV_MAP.items():
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        for config_key, env_key in mapping.items():
            if config_key in section:
                env[env_key] = _format_env_value(section[config_key])


def _collect_llm_config(
    section: object,
    env: dict[str, str],
    *,
    source_env: dict[str, str] | None,
) -> None:
    if not isinstance(section, dict):
        return
    providers = section.get("providers")
    if isinstance(providers, dict) and providers:
        default_profile = str(section.get("default", "")).strip() or next(iter(providers))
        selected = providers.get(default_profile)
        if not isinstance(selected, dict):
            return
        env[LLM_DEFAULT_PROFILE_ENV] = default_profile
        env[LLM_PROFILES_ENV] = _format_json_env_value(
            {
                name: _tenant_llm_from_unified_llm(profile, preserve_api_key_env=True)
                for name, profile in providers.items()
                if isinstance(name, str) and isinstance(profile, dict)
            }
        )
        _collect_llm_config(selected, env, source_env=source_env)
        return
    provider = str(section.get("provider", "")).strip().lower()
    if provider:
        env["MINIGENT_LLM_PROVIDER"] = provider
    if "model" in section:
        model = _format_env_value(section["model"])
        env["MINIGENT_LLM_MODEL"] = model
        if provider == "openai":
            env["OPENAI_MODEL"] = model
        elif provider == "openrouter":
            env["OPENROUTER_MODEL"] = model
        elif provider in {"google", "google-generative-ai", "gemini"}:
            env["GEMINI_MODEL"] = model
            env["GOOGLE_MODEL"] = model
        elif provider == "anthropic":
            env["ANTHROPIC_MODEL"] = model
    if "url" in section:
        env["MINIGENT_LLM_URL"] = _format_env_value(section["url"])
    if "base_url" in section:
        base_url = _format_env_value(section["base_url"])
        if provider == "openai":
            env["OPENAI_BASE_URL"] = base_url
        elif provider == "openrouter":
            env["OPENROUTER_BASE_URL"] = base_url
        elif provider in {"google", "google-generative-ai", "gemini"}:
            env["GOOGLE_BASE_URL"] = base_url
        elif provider == "anthropic":
            env["ANTHROPIC_BASE_URL"] = base_url
        else:
            env["MINIGENT_LLM_URL"] = base_url
    if "extra_headers" in section:
        env["MINIGENT_LLM_EXTRA_HEADERS"] = _format_json_env_value(section["extra_headers"])
    if "input_modalities" in section:
        env["MINIGENT_LLM_INPUT_MODALITIES"] = _format_env_value(section["input_modalities"])
    if provider == "anthropic":
        if "max_tokens" in section:
            env["ANTHROPIC_MAX_TOKENS"] = _format_env_value(section["max_tokens"])
        if "anthropic_version" in section:
            env["ANTHROPIC_VERSION"] = _format_env_value(section["anthropic_version"])
        if "thinking_enabled" in section:
            env["ANTHROPIC_THINKING_ENABLED"] = _format_env_value(section["thinking_enabled"])
        if "thinking_budget_tokens" in section:
            env["ANTHROPIC_THINKING_BUDGET_TOKENS"] = _format_env_value(
                section["thinking_budget_tokens"]
            )
        if "thinking_effort" in section:
            env["ANTHROPIC_THINKING_EFFORT"] = _format_env_value(section["thinking_effort"])
        if "prompt_cache_enabled" in section:
            env["ANTHROPIC_PROMPT_CACHE_ENABLED"] = _format_env_value(
                section["prompt_cache_enabled"]
            )
    if "account_id_header" in section:
        env["MINIGENT_LLM_ACCOUNT_ID_HEADER"] = _format_env_value(section["account_id_header"])

    api_key_env = str(section.get("api_key_env", "")).strip()
    if api_key_env:
        _copy_secret_env(api_key_env, _provider_api_key_env(provider), env, source_env=source_env)
    if "api_key" in section:
        target_key = _provider_api_key_env(provider)
        if target_key:
            env[target_key] = _format_env_value(section["api_key"])


def _collect_mcp_config(section: object, env: dict[str, str]) -> None:
    if not isinstance(section, dict):
        return
    if "broker_enabled" in section:
        env["MINIGENT_MCP_BROKER_ENABLED"] = _format_env_value(section["broker_enabled"])
    if "broker_url" in section:
        env["MINIGENT_MCP_BROKER_URL"] = _format_env_value(section["broker_url"])
    if "servers" in section:
        env["MINIGENT_MCP_SERVERS"] = _format_json_env_value(section["servers"])


def _collect_coding_inline_config(section: object, env: dict[str, str]) -> None:
    if not isinstance(section, dict):
        return
    if "workspace_scopes" in section:
        env["MINIGENT_CODING_WORKSPACE_SCOPES"] = _format_json_env_value(
            section["workspace_scopes"]
        )
    if "mcp_gateway_enabled" in section:
        env["MINIGENT_CODING_MCP_GATEWAY_ENABLED"] = _format_env_value(
            section["mcp_gateway_enabled"]
        )
    if "mcp_gateway_port" in section:
        env["MINIGENT_CODING_MCP_GATEWAY_PORT"] = _format_env_value(section["mcp_gateway_port"])
    if "mcp_gateway_path_prefix" in section:
        env["MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX"] = _format_env_value(
            section["mcp_gateway_path_prefix"]
        )
    if "mcp_server_specs" in section:
        env["MINIGENT_CODING_MCP_SERVER_SPECS"] = _format_json_env_value(
            section["mcp_server_specs"]
        )


def _collect_peer_agents(section: object, env: dict[str, str]) -> None:
    if section is not None:
        env["MINIGENT_PEER_AGENTS"] = _format_json_env_value(section)


def _collect_tenant_execution_configs(
    section: object,
    env: dict[str, str],
    *,
    llm_section: object = None,
    coding_section: object = None,
    agent_skills_section: object = None,
    base_dir: Path | None = None,
) -> None:
    if section is not None:
        projected = _with_default_llm_projection(section, llm_section)
        projected = _with_coding_mcp_server_projections(projected, coding_section)
        projected = _with_agent_skill_imports(projected, agent_skills_section, base_dir=base_dir)
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = _format_json_env_value(projected)


def _with_default_llm_projection(section: object, llm_section: object) -> object:
    """Use top-level unified [llm] as the default for tenant execution configs.

    Unified config is meant to keep common local config simple: a single top-level
    ``[llm]`` should define the LLM used by tenants unless a tenant explicitly overrides
    it.  Project that default into the internal tenant JSON so exported unified configs
    remain restartable even when stale process env vars such as ``MINIGENT_LLM_PROVIDER``
    are already set.
    """

    if not isinstance(section, dict) or not isinstance(llm_section, dict):
        return section
    providers = llm_section.get("providers")
    default_profile: str | None = None
    selected_llm = llm_section
    tenant_profiles: dict[str, dict[str, object]] = {}
    if isinstance(providers, dict) and providers:
        default_profile = str(llm_section.get("default", "")).strip() or next(iter(providers))
        selected = providers.get(default_profile)
        if not isinstance(selected, dict):
            return section
        selected_llm = selected
        tenant_profiles = {
            name: _tenant_llm_from_unified_llm(profile, preserve_api_key_env=True)
            for name, profile in providers.items()
            if isinstance(name, str) and isinstance(profile, dict)
        }
    tenant_llm = _tenant_llm_from_unified_llm(selected_llm)
    if not tenant_llm:
        return section
    projected = copy.deepcopy(section)
    for tenant_config in projected.values():
        if isinstance(tenant_config, dict) and "llm" not in tenant_config:
            tenant_config["llm"] = dict(tenant_llm)
        if isinstance(tenant_config, dict) and tenant_profiles:
            tenant_config.setdefault("llm_profiles", copy.deepcopy(tenant_profiles))
            tenant_config.setdefault("default_llm_profile", default_profile)
    return projected


def _tenant_llm_from_unified_llm(
    section: dict[str, Any], *, preserve_api_key_env: bool = False
) -> dict[str, object]:
    provider = str(section.get("provider", "")).strip().lower()
    tenant_llm: dict[str, object] = {}
    if provider:
        tenant_llm["provider"] = provider
    for key in (
        "model",
        "extra_headers",
        "timeout",
        "max_tokens",
        "anthropic_version",
        "thinking_enabled",
        "thinking_budget_tokens",
        "thinking_effort",
        "prompt_cache_enabled",
        "input_modalities",
    ):
        if key in section:
            tenant_llm[key] = copy.deepcopy(section[key])
    base_url = section.get("base_url", section.get("url"))
    if base_url is not None:
        tenant_llm["base_url"] = base_url
    api_key_env = str(section.get("api_key_env", "")).strip()
    if api_key_env:
        target_env = api_key_env if preserve_api_key_env else _provider_api_key_env(provider)
        if target_env:
            tenant_llm["api_key"] = f"${{{target_env}}}"
    elif "api_key" in section:
        tenant_llm["api_key"] = section["api_key"]
    return tenant_llm


def _with_agent_skill_imports(
    section: object,
    agent_skills_section: object,
    *,
    base_dir: Path | None,
) -> object:
    if not isinstance(section, dict) or not isinstance(agent_skills_section, dict):
        return section
    dirs = _agent_skill_dirs(agent_skills_section, base_dir=base_dir)
    if not dirs:
        return section
    imported_skills = discover_agent_skills(dirs)
    if not imported_skills:
        return section
    imported_names: set[str] = set()
    imported_items: list[dict[str, object]] = []
    for skill in imported_skills:
        if skill.name in imported_names:
            raise RuntimeError(f"Agent Skill '{skill.name}' is imported more than once")
        imported_names.add(skill.name)
        imported_items.append(
            {
                "name": skill.name,
                "description": skill.description,
                "instruction_source": {
                    "type": "agent_skill",
                    "path": str(skill.skill_md_path),
                },
            }
        )

    projected: dict[object, object] = {}
    for tenant_id, tenant_config in section.items():
        if not isinstance(tenant_config, dict):
            projected[tenant_id] = tenant_config
            continue
        tenant_copy: dict[object, object] = dict(tenant_config)
        raw_skills = tenant_copy.get("skills")
        skills: dict[object, object] = dict(raw_skills) if isinstance(raw_skills, dict) else {}
        raw_items = skills.get("items")
        items = list(raw_items) if isinstance(raw_items, list) else []
        existing_names = {
            item.get("name")
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        conflicts = sorted(imported_names & existing_names)
        if conflicts:
            raise RuntimeError(
                f"Tenant '{tenant_id}' Agent Skill imports conflict with configured skills: "
                + ", ".join(conflicts)
            )
        skills["items"] = [*items, *imported_items]
        tenant_copy["skills"] = skills
        projected[tenant_id] = tenant_copy
    return projected


def _agent_skill_dirs(section: dict[object, object], *, base_dir: Path | None) -> list[Path]:
    raw_dirs = section.get("dirs") or section.get("directories")
    if raw_dirs is None:
        return []
    if isinstance(raw_dirs, str):
        values = [part.strip() for part in raw_dirs.split(",") if part.strip()]
    elif isinstance(raw_dirs, list) and all(isinstance(item, str) for item in raw_dirs):
        values = raw_dirs
    else:
        raise RuntimeError("agent_skills.dirs must be a string or list of strings")
    dirs: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        dirs.append(path)
    return dirs


def _with_coding_mcp_server_projections(section: object, coding_section: object) -> object:
    """Project restartable coding MCP specs into tenant runtime config.

    ``minigent config export --local-coding`` writes MCP definitions once under
    ``coding.mcp_server_specs`` and intentionally removes the derived
    ``tenant_execution_configs.*.tools.mcp_servers`` entries. The runtime still validates
    skill/profile ``mcp_server_names`` against tenant tools, so recreate that projection
    while loading the unified config.
    """

    if not isinstance(section, dict) or not isinstance(coding_section, dict):
        return section
    specs = coding_section.get("mcp_server_specs")
    if not isinstance(specs, list):
        return section
    projected_servers = _tenant_mcp_servers_from_coding_specs(specs, coding_section)
    if not projected_servers:
        return section

    projected: dict[object, object] = {}
    for tenant_id, tenant_config in section.items():
        if not isinstance(tenant_config, dict):
            projected[tenant_id] = tenant_config
            continue
        tenant_copy: dict[object, object] = dict(tenant_config)
        raw_tools = tenant_copy.get("tools")
        tools: dict[object, object] = dict(raw_tools) if isinstance(raw_tools, dict) else {}
        if "mcp_servers" not in tools and "mcpServers" not in tools:
            tools["mcp_servers"] = projected_servers
        tenant_copy["tools"] = tools
        projected[tenant_id] = tenant_copy
    return projected


def _tenant_mcp_servers_from_coding_specs(
    specs: list[object], coding_section: dict[object, object]
) -> list[dict[str, object]]:
    servers: list[dict[str, object]] = []
    gateway_prefix = _coding_gateway_url_prefix(coding_section)
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            continue
        url = spec.get("url")
        if not isinstance(url, str) or not url:
            if spec.get("transport") == "stdio" and gateway_prefix is not None:
                url = f"{gateway_prefix}/{name}"
            else:
                continue
        server: dict[str, object] = {"name": name, "url": url}
        headers = spec.get("headers")
        if isinstance(headers, dict):
            server["headers"] = headers
        allowed_tools = spec.get("allowed_tools")
        if isinstance(allowed_tools, list) and all(isinstance(item, str) for item in allowed_tools):
            server["allowed_tools"] = allowed_tools
        path_policy = spec.get("path_policy")
        if isinstance(path_policy, dict):
            server["path_policy"] = path_policy
        result_redaction = spec.get("result_redaction")
        if isinstance(result_redaction, dict):
            server["result_redaction"] = result_redaction
        timeout_seconds = spec.get("timeout_seconds")
        if isinstance(timeout_seconds, int | float):
            server["timeout_seconds"] = timeout_seconds
        servers.append(server)
    return servers


def _coding_gateway_url_prefix(coding_section: dict[object, object]) -> str | None:
    if not _config_bool(coding_section.get("mcp_gateway_enabled")):
        return None
    host = str(coding_section.get("bridge_host") or "127.0.0.1").strip() or "127.0.0.1"
    raw_port = coding_section.get("mcp_gateway_port") or coding_section.get("bridge_port")
    port = raw_port if isinstance(raw_port, int | str) else 8765
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        port_int = 8765
    path_prefix = str(coding_section.get("mcp_gateway_path_prefix") or "/mcp")
    if not path_prefix.startswith("/"):
        path_prefix = f"/{path_prefix}"
    path_prefix = path_prefix.rstrip("/") or "/mcp"
    return f"http://{host}:{port_int}{path_prefix}"


def _config_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _provider_api_key_env(provider: str) -> str | None:
    if provider == "openai":
        return "OPENAI_API_KEY"
    if provider == "openrouter":
        return "OPENROUTER_API_KEY"
    if provider in {"google", "google-generative-ai", "gemini"}:
        return "GEMINI_API_KEY"
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    if provider in {"generic-oauth"}:
        return None
    return None


def _copy_secret_env(
    source_key: str,
    target_key: str | None,
    env: dict[str, str],
    *,
    source_env: dict[str, str] | None,
) -> None:
    if target_key is None:
        return
    lookup_env = os.environ if source_env is None else source_env
    value = lookup_env.get(source_key)
    if value:
        env[target_key] = value


def _format_env_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return ",".join(_format_env_value(item) for item in value)
    if isinstance(value, dict):
        return _format_json_env_value(value)
    return str(value)


def _format_json_env_value(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
