from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.unified_config_schema import parse_unified_config

CONFIG_FILE_ENV = "MINIGENT_CONFIG_FILE"
DOTENV_FILE_ENV = "MINIGENT_DOTENV_FILE"
CONFIG_DISCOVERY_ENV = "MINIGENT_CONFIG_DISCOVERY"
DEFAULT_CONFIG_FILE = "minigent.toml"
DEFAULT_DOTENV_FILE = ".env"
DEFAULT_CODING_DOTENV_FILE = ".env.coding"
DEFAULT_THREAD_DB_PATH = ".data/minigent-threads.db"
DEFAULT_CODING_THREAD_DB_PATH = ".data/minigent-coding-threads.db"
DEFAULT_VOICE_THREAD_DB_PATH = ".data/minigent-voice-threads.db"


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
    "coding": {
        "enabled": "MINIGENT_CODING_MCP_GATEWAY_ENABLED",
        "tenant_id": "MINIGENT_CODING_TENANT_ID",
        "workspace": "MINIGENT_CODING_WORKSPACE",
        "workspaces": "MINIGENT_CODING_WORKSPACES",
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
    to replace most local .env entries. Set MINIGENT_DOTENV_FILE to use a dotenv file other
    than .env. Set ``discover_default_files`` to ``False`` for isolated subprocesses/tests
    that must ignore cwd-local ``minigent.toml`` and ``.env`` files unless explicit paths
    are configured.
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
    default discovery is disabled. When ``discover_default_files`` is ``None``, discovery
    defaults to enabled unless ``MINIGENT_CONFIG_DISCOVERY`` is set to an off-like value
    (``0``, ``false``, ``no``, ``off``, ``disabled``, or ``explicit``).
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
    default_path = base_dir / DEFAULT_CONFIG_FILE
    return default_path if default_path.exists() else None


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
        data.get("tenant_execution_configs"), env, coding_section=data.get("coding")
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
    coding_section: object = None,
) -> None:
    if section is not None:
        env["MINIGENT_TENANT_EXECUTION_CONFIGS"] = _format_json_env_value(
            _with_coding_mcp_server_projections(section, coding_section)
        )


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
