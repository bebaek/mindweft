from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from fastapi import HTTPException

from app.admin_store import SQLiteTenantConfigStore
from app.llm import (
    GenericOAuthResponsesAdapter,
    GoogleGeminiAdapter,
    LLMAdapter,
    MockLLMAdapter,
    OpenAICompatibleAdapter,
    build_llm_adapter_from_env,
)
from app.mcp import MCPHTTPClient, MCPPathPolicy, MCPServerConfig, load_mcp_server_configs_from_env
from app.mcp_manager import MCPServerManager
from app.oauth import (
    GENERIC_OAUTH_PROVIDER,
    OAUTH_ACCOUNT_ID_JWT_CLAIM_ENV,
    OAUTH_AUTH_PARAMS_ENV,
    OAUTH_AUTHORIZE_URL_ENV,
    OAUTH_CLIENT_ID_ENV,
    OAUTH_PROVIDER_ID_ENV,
    OAUTH_REDIRECT_URI_ENV,
    OAUTH_SCOPE_ENV,
    OAUTH_STORE_PATH_ENV,
    OAUTH_TOKEN_URL_ENV,
)
from app.redaction import ToolResultRedactionPolicy, parse_tool_result_redaction_policy
from app.tools import DEFAULT_LOCAL_TOOL_NAMES, LOCAL_TOOL_NAMES, ToolRegistry, build_tool_registry

TENANT_EXECUTION_CONFIGS_ENV = "MINIGENT_TENANT_EXECUTION_CONFIGS"
TENANT_CONFIG_SOURCE_ENV = "MINIGENT_TENANT_CONFIG_SOURCE"
DEFAULT_TENANT_KEY = "*"
TENANT_CONFIG_SOURCE_ENV_ONLY = "env"
TENANT_CONFIG_SOURCE_STORE = "store"
TENANT_CONFIG_SOURCE_STORE_WITH_DEFAULTS = "store-with-defaults"
TENANT_ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
AGENT_BACKEND_ENV = "MINIGENT_AGENT_BACKEND"
AGENT_BACKEND_PEER_ENV = "MINIGENT_AGENT_BACKEND_PEER"
AGENT_BACKEND_CWD_ENV = "MINIGENT_AGENT_BACKEND_CWD"
AGENT_BACKEND_TIMEOUT_ENV = "MINIGENT_AGENT_BACKEND_TIMEOUT_SECONDS"
AGENT_BACKEND_POLL_INTERVAL_ENV = "MINIGENT_AGENT_BACKEND_POLL_INTERVAL_SECONDS"
AGENT_BACKEND_MCP_BROKER_ENABLED_ENV = "MINIGENT_MCP_BROKER_ENABLED"
AGENT_BACKEND_NATIVE = "native"
AGENT_BACKEND_PEER_AGENT = "peer_agent"
QUALITY_ENABLED_ENV = "MINIGENT_REMOTE_QUALITY_ENABLED"
QUALITY_MODE_ENV = "MINIGENT_REMOTE_QUALITY_MODE"
QUALITY_PROVIDER_ENV = "MINIGENT_REMOTE_QUALITY_PROVIDER"
QUALITY_MODEL_ENV = "MINIGENT_REMOTE_QUALITY_MODEL"
QUALITY_BASE_URL_ENV = "MINIGENT_REMOTE_QUALITY_BASE_URL"
QUALITY_API_KEY_ENV = "MINIGENT_REMOTE_QUALITY_API_KEY"
QUALITY_TIMEOUT_ENV = "MINIGENT_REMOTE_QUALITY_TIMEOUT"
QUALITY_MAX_PAYLOAD_CHARS_ENV = "MINIGENT_REMOTE_QUALITY_MAX_PAYLOAD_CHARS"


@dataclass(frozen=True)
class TenantLLMConfig:
    provider: str = "mock"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0


@dataclass(frozen=True)
class TenantToolConfig:
    allowed_local_tools: list[str] | None = None
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    result_redaction_policy: ToolResultRedactionPolicy = field(
        default_factory=ToolResultRedactionPolicy
    )


@dataclass(frozen=True)
class TenantAgentBackendConfig:
    type: str = AGENT_BACKEND_NATIVE
    peer: str | None = None
    cwd: str | None = None
    timeout_seconds: float = 180.0
    poll_interval_seconds: float = 1.0
    mcp_broker_enabled: bool = True


@dataclass(frozen=True)
class TenantQualityConfig:
    enabled: bool = False
    mode: str = "critique_draft"
    provider: str = "mock"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    max_payload_chars: int = 6000


@dataclass(frozen=True)
class TenantExecutionConfig:
    tenant_id: str
    llm: TenantLLMConfig = field(default_factory=TenantLLMConfig)
    tools: TenantToolConfig = field(default_factory=TenantToolConfig)
    agent_backend: TenantAgentBackendConfig = field(default_factory=TenantAgentBackendConfig)
    quality: TenantQualityConfig = field(default_factory=TenantQualityConfig)
    skills: "TenantSkillsConfig" = field(default_factory=lambda: TenantSkillsConfig())
    capability_profiles: "TenantCapabilityProfilesConfig" = field(
        default_factory=lambda: TenantCapabilityProfilesConfig()
    )


@dataclass(frozen=True)
class TenantSkillConfig:
    name: str
    system_prompt: str
    description: str | None = None
    allowed_local_tools: list[str] | None = None
    mcp_server_names: list[str] | None = None


@dataclass(frozen=True)
class TenantSkillsConfig:
    default_skill: str | None = None
    items: list[TenantSkillConfig] = field(default_factory=list)


@dataclass(frozen=True)
class TenantCapabilityProfileConfig:
    name: str
    description: str | None = None
    allowed_local_tools: list[str] | None = None
    mcp_server_names: list[str] | None = None


@dataclass(frozen=True)
class TenantCapabilityProfilesConfig:
    default_profile: str | None = None
    items: list[TenantCapabilityProfileConfig] = field(default_factory=list)


@dataclass(frozen=True)
class TenantExecutionContext:
    llm_adapter: LLMAdapter
    tool_registry: ToolRegistry
    config: TenantExecutionConfig
    mcp_generation: int = 0
    mcp_manager: MCPServerManager | None = None


@dataclass(frozen=True)
class TenantExecutionValidationReport:
    valid: bool
    config_shape: dict[str, Any]
    llm: dict[str, Any]
    tools: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "config_shape": self.config_shape,
            "llm": self.llm,
            "tools": self.tools,
        }


class TenantExecutionResolver:
    def resolve(self, tenant_id: str) -> TenantExecutionContext:
        raise NotImplementedError

    def describe(
        self,
        tenant_id: str | None = None,
        *,
        include_export: bool = False,
    ) -> dict[str, object]:
        raise NotImplementedError

    def invalidate(self, tenant_id: str) -> None:
        _ = tenant_id


class FixedTenantExecutionResolver(TenantExecutionResolver):
    def __init__(
        self,
        llm_adapter: LLMAdapter,
        tool_registry: ToolRegistry,
        *,
        config: TenantExecutionConfig | None = None,
        mcp_manager: MCPServerManager | None = None,
        mcp_generation: int = 0,
    ) -> None:
        self._mcp_manager = mcp_manager
        self._context = TenantExecutionContext(
            llm_adapter=llm_adapter,
            tool_registry=tool_registry,
            config=config or TenantExecutionConfig(tenant_id=DEFAULT_TENANT_KEY),
            mcp_generation=mcp_generation,
            mcp_manager=mcp_manager,
        )

    def resolve(self, tenant_id: str) -> TenantExecutionContext:
        _ = tenant_id
        self._refresh_context_if_needed()
        return self._context

    def describe(
        self,
        tenant_id: str | None = None,
        *,
        include_export: bool = False,
    ) -> dict[str, object]:
        _ = tenant_id
        self._refresh_context_if_needed()
        return _describe_context(
            DEFAULT_TENANT_KEY,
            self._context,
            include_export=include_export,
        )

    def _refresh_context_if_needed(self) -> None:
        if self._mcp_manager is None or not self._context.config.tools.mcp_servers:
            return
        registry, generation = _build_registry_for_config(
            self._context.config,
            mcp_manager=self._mcp_manager,
        )
        if generation == self._context.mcp_generation:
            return
        self._context = TenantExecutionContext(
            llm_adapter=self._context.llm_adapter,
            tool_registry=registry,
            config=self._context.config,
            mcp_generation=generation,
            mcp_manager=self._mcp_manager,
        )


class InMemoryTenantExecutionResolver(TenantExecutionResolver):
    def __init__(
        self,
        tenant_configs: dict[str, TenantExecutionConfig],
        *,
        default_context: TenantExecutionContext | None = None,
        mcp_manager: MCPServerManager | None = None,
    ) -> None:
        self._tenant_configs = dict(tenant_configs)
        self._default_context = default_context
        self._mcp_manager = mcp_manager
        self._contexts: dict[str, TenantExecutionContext] = {}
        self._lock = Lock()

    def resolve(self, tenant_id: str) -> TenantExecutionContext:
        with self._lock:
            context = self._contexts.get(tenant_id)
            if context is not None:
                if self._refresh_context_if_needed(tenant_id, context):
                    return self._contexts[tenant_id]
                return context

            config = self._tenant_configs.get(tenant_id)
            if config is None:
                config = self._tenant_configs.get(DEFAULT_TENANT_KEY)
            if config is None:
                if self._default_context is not None:
                    return self._default_context
                raise HTTPException(
                    status_code=403,
                    detail=f"Tenant '{tenant_id}' has no execution configuration",
                )

            registry, generation = _build_registry_for_config(
                config,
                mcp_manager=self._mcp_manager,
            )
            context = TenantExecutionContext(
                llm_adapter=_build_llm_adapter(config.llm),
                tool_registry=registry,
                config=config,
                mcp_generation=generation,
                mcp_manager=self._mcp_manager,
            )
            self._contexts[tenant_id] = context
            return context

    def describe(
        self,
        tenant_id: str | None = None,
        *,
        include_export: bool = False,
    ) -> dict[str, object]:
        if tenant_id is None:
            if self._default_context is not None:
                return _describe_context(
                    DEFAULT_TENANT_KEY,
                    self._default_context,
                    include_export=include_export,
                )
            if DEFAULT_TENANT_KEY not in self._tenant_configs and self._tenant_configs:
                tenant_id = sorted(self._tenant_configs)[0]
            else:
                tenant_id = DEFAULT_TENANT_KEY

        context = self.resolve(tenant_id)
        return _describe_context(tenant_id, context, include_export=include_export)

    def invalidate(self, tenant_id: str) -> None:
        with self._lock:
            self._contexts.pop(tenant_id, None)

    def _refresh_context_if_needed(self, tenant_id: str, context: TenantExecutionContext) -> bool:
        if self._mcp_manager is None or not context.config.tools.mcp_servers:
            return False
        registry, generation = _build_registry_for_config(
            context.config,
            mcp_manager=self._mcp_manager,
        )
        if generation == context.mcp_generation:
            return False
        self._contexts[tenant_id] = TenantExecutionContext(
            llm_adapter=context.llm_adapter,
            tool_registry=registry,
            config=context.config,
            mcp_generation=generation,
            mcp_manager=self._mcp_manager,
        )
        return True


class StoreBackedTenantExecutionResolver(TenantExecutionResolver):
    def __init__(
        self,
        store: SQLiteTenantConfigStore,
        *,
        fallback_resolver: TenantExecutionResolver | None = None,
        mcp_manager: MCPServerManager | None = None,
    ) -> None:
        self._store = store
        self._fallback_resolver = fallback_resolver
        self._mcp_manager = mcp_manager
        self._contexts: dict[str, TenantExecutionContext] = {}
        self._lock = Lock()

    def resolve(self, tenant_id: str) -> TenantExecutionContext:
        with self._lock:
            context = self._contexts.get(tenant_id)
            if context is not None:
                if self._refresh_context_if_needed(tenant_id, context):
                    return self._contexts[tenant_id]
                return context

            payload = self._store.get_raw_config(tenant_id)
            if payload is None:
                payload = self._store.get_raw_config(DEFAULT_TENANT_KEY)
            if payload is None:
                if self._fallback_resolver is not None:
                    return self._fallback_resolver.resolve(tenant_id)
                raise HTTPException(
                    status_code=403,
                    detail=f"Tenant '{tenant_id}' has no execution configuration",
                )

            config = parse_tenant_execution_config(tenant_id, payload)
            registry, generation = _build_registry_for_config(
                config,
                mcp_manager=self._mcp_manager,
            )
            context = TenantExecutionContext(
                llm_adapter=_build_llm_adapter(config.llm),
                tool_registry=registry,
                config=config,
                mcp_generation=generation,
                mcp_manager=self._mcp_manager,
            )
            self._contexts[tenant_id] = context
            return context

    def describe(
        self,
        tenant_id: str | None = None,
        *,
        include_export: bool = False,
    ) -> dict[str, object]:
        if tenant_id is not None:
            context = self.resolve(tenant_id)
            return _describe_context(tenant_id, context, include_export=include_export)
        if self._fallback_resolver is not None:
            return self._fallback_resolver.describe(include_export=include_export)
        return {"tenant_id": None, "llm": None, "mcp_servers": [], "local_tools": []}

    def invalidate(self, tenant_id: str) -> None:
        with self._lock:
            self._contexts.pop(tenant_id, None)

    def _refresh_context_if_needed(self, tenant_id: str, context: TenantExecutionContext) -> bool:
        if self._mcp_manager is None or not context.config.tools.mcp_servers:
            return False
        registry, generation = _build_registry_for_config(
            context.config,
            mcp_manager=self._mcp_manager,
        )
        if generation == context.mcp_generation:
            return False
        self._contexts[tenant_id] = TenantExecutionContext(
            llm_adapter=context.llm_adapter,
            tool_registry=registry,
            config=context.config,
            mcp_generation=generation,
            mcp_manager=self._mcp_manager,
        )
        return True


def _describe_context(
    tenant_id: str,
    context: TenantExecutionContext,
    *,
    include_export: bool,
) -> dict[str, object]:
    llm = context.llm_adapter.describe()
    result: dict[str, object] = {
        "tenant_id": tenant_id,
        "llm": llm,
        "agent_backend": _agent_backend_public_dict(context.config.agent_backend),
        "quality": _quality_public_dict(context.config.quality),
        "mcp_servers": context.tool_registry.mcp_servers(),
        "local_tools": sorted(
            spec.name for spec in context.tool_registry.specs() if "." not in spec.name
        ),
    }
    if include_export:
        result["unified_config_export"] = _unified_config_export_public_dict(context, llm)
    return result


def _unified_config_export_public_dict(
    context: TenantExecutionContext,
    llm_description: dict[str, Any],
) -> dict[str, object]:
    config = context.config
    export: dict[str, object] = {}
    coding = _coding_config_export_public_dict()
    if coding:
        export["coding"] = coding
    llm = _llm_export_public_dict(config.llm, llm_description)
    if llm:
        export["llm"] = llm
    oauth = _generic_oauth_public_dict(llm)
    if oauth:
        export["oauth"] = oauth
    if config.agent_backend.mcp_broker_enabled:
        export["mcp"] = {"broker_enabled": True}
    tenant_config = _tenant_execution_config_public_dict(config)
    if tenant_config:
        export["tenant_execution_configs"] = {config.tenant_id: tenant_config}
    export["runtime"] = _runtime_export_public_dict(context)
    return export


def _runtime_export_public_dict(context: TenantExecutionContext) -> dict[str, object]:
    return {
        "mcp_servers": [
            _runtime_mcp_server_public_dict(server)
            for server in context.tool_registry.mcp_servers()
        ],
        "tools": [spec.name for spec in context.tool_registry.specs()],
    }


def _runtime_mcp_server_public_dict(server: dict[str, Any]) -> dict[str, object]:
    exported: dict[str, object] = {}
    for key in ("name", "status", "server_name", "server_version", "tool_count"):
        value = server.get(key)
        if value not in (None, "None"):
            exported[key] = value
    return exported


def _coding_config_export_public_dict() -> dict[str, object]:
    exported: dict[str, object] = {}
    raw_path = os.getenv("MINIGENT_CODING_MCP_SERVERS_FILE", "").strip()
    if raw_path:
        exported["mcp_servers_file"] = raw_path
        path = Path(raw_path).expanduser()
        if path.exists() and path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                servers = payload.get("servers")
            else:
                servers = payload
            if isinstance(servers, list):
                exported["mcp_server_specs"] = [
                    _sanitize_coding_mcp_server_spec(server)
                    for server in servers
                    if isinstance(server, dict)
                ]
    return exported


def _sanitize_coding_mcp_server_spec(server: dict[str, Any]) -> dict[str, object]:
    exported: dict[str, object] = {}
    for key, value in server.items():
        if key == "headers" and isinstance(value, dict):
            exported[key] = {header: "<set>" for header in sorted(value)}
        elif key == "env" and isinstance(value, dict):
            exported[key] = {env_key: "<set>" for env_key in sorted(value)}
        elif _is_public_config_value(value):
            exported[key] = value
    return exported


def _is_public_config_value(value: object) -> bool:
    if isinstance(value, str | int | float | bool) or value is None:
        return True
    if isinstance(value, list):
        return all(_is_public_config_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_public_config_value(item) for key, item in value.items())
    return False


def _llm_export_public_dict(
    config: TenantLLMConfig,
    llm_description: dict[str, Any],
) -> dict[str, object]:
    provider = _string_value(llm_description.get("provider")) or config.provider
    exported: dict[str, object] = {}
    if provider:
        exported["provider"] = provider
    model = _string_value(llm_description.get("model")) or config.model
    if model:
        exported["model"] = model
    url = _string_value(llm_description.get("url")) or _string_value(
        llm_description.get("base_url")
    ) or config.base_url
    if url:
        exported["url" if provider == GENERIC_OAUTH_PROVIDER else "base_url"] = url
    if config.extra_headers:
        exported["extra_headers"] = dict(config.extra_headers)
    return exported


def _tenant_execution_config_public_dict(config: TenantExecutionConfig) -> dict[str, object]:
    exported: dict[str, object] = {}
    tools = _tenant_tool_config_public_dict(config.tools)
    if tools:
        exported["tools"] = tools
    skills = _tenant_skills_config_public_dict(config.skills)
    if skills:
        exported["skills"] = skills
    capability_profiles = _tenant_capability_profiles_public_dict(config.capability_profiles)
    if capability_profiles:
        exported["capability_profiles"] = capability_profiles
    agent_backend = _agent_backend_export_public_dict(config.agent_backend)
    if agent_backend:
        exported["agent_backend"] = agent_backend
    quality = _quality_export_public_dict(config.quality)
    if quality:
        exported["quality"] = quality
    return exported


def _agent_backend_export_public_dict(config: TenantAgentBackendConfig) -> dict[str, object]:
    defaults = TenantAgentBackendConfig()
    exported: dict[str, object] = {}
    if config.type != defaults.type:
        exported["type"] = config.type
    if config.peer is not None:
        exported["peer"] = config.peer
    if config.cwd is not None:
        exported["cwd"] = config.cwd
    if config.timeout_seconds != defaults.timeout_seconds:
        exported["timeout_seconds"] = config.timeout_seconds
    if config.poll_interval_seconds != defaults.poll_interval_seconds:
        exported["poll_interval_seconds"] = config.poll_interval_seconds
    if config.mcp_broker_enabled != defaults.mcp_broker_enabled:
        exported["mcp_broker_enabled"] = config.mcp_broker_enabled
    return exported


def _quality_export_public_dict(config: TenantQualityConfig) -> dict[str, object]:
    defaults = TenantQualityConfig()
    exported: dict[str, object] = {}
    if config.enabled != defaults.enabled:
        exported["enabled"] = config.enabled
    if config.mode != defaults.mode:
        exported["mode"] = config.mode
    if config.provider != defaults.provider:
        exported["provider"] = config.provider
    if config.model is not None:
        exported["model"] = config.model
    if config.base_url is not None:
        exported["base_url"] = config.base_url
    if config.extra_headers:
        exported["headers"] = sorted(config.extra_headers.keys())
    if config.timeout != defaults.timeout:
        exported["timeout"] = config.timeout
    if config.max_payload_chars != defaults.max_payload_chars:
        exported["max_payload_chars"] = config.max_payload_chars
    return exported


def _tenant_tool_config_public_dict(config: TenantToolConfig) -> dict[str, object]:
    exported: dict[str, object] = {}
    if config.allowed_local_tools is not None:
        exported["allowed_local_tools"] = list(config.allowed_local_tools)
    if config.mcp_servers:
        exported["mcp_servers"] = [
            _mcp_server_config_public_dict(server) for server in config.mcp_servers
        ]
    result_redaction = _result_redaction_public_dict(config.result_redaction_policy)
    if result_redaction:
        exported["result_redaction"] = result_redaction
    return exported


def _mcp_server_config_public_dict(config: MCPServerConfig) -> dict[str, object]:
    exported: dict[str, object] = {
        "name": config.name,
        "url": config.url,
    }
    if config.headers:
        exported["headers"] = {key: "<set>" for key in sorted(config.headers)}
    if config.allowed_tools is not None:
        exported["allowed_tools"] = list(config.allowed_tools)
    if config.path_policy.deny_globs or config.path_policy.allow_globs:
        exported["path_policy"] = {
            "deny_globs": list(config.path_policy.deny_globs),
            "allow_globs": list(config.path_policy.allow_globs),
        }
    result_redaction = _result_redaction_public_dict(config.result_redaction_policy)
    if result_redaction:
        exported["result_redaction"] = result_redaction
    return exported


def _result_redaction_public_dict(policy: ToolResultRedactionPolicy) -> dict[str, object]:
    if not policy.enabled or policy.mode != "best_effort" or policy.sensitive_tools:
        return {
            "enabled": policy.enabled,
            "mode": policy.mode,
            "sensitive_tools": sorted(policy.sensitive_tools),
        }
    return {}


def _tenant_skills_config_public_dict(config: TenantSkillsConfig) -> dict[str, object]:
    exported: dict[str, object] = {}
    if config.default_skill is not None:
        exported["default_skill"] = config.default_skill
    if config.items:
        exported["items"] = [
            _tenant_skill_config_public_dict(skill)
            for skill in config.items
        ]
    return exported


def _tenant_skill_config_public_dict(config: TenantSkillConfig) -> dict[str, object]:
    exported: dict[str, object] = {
        "name": config.name,
        "system_prompt": config.system_prompt,
    }
    if config.description is not None:
        exported["description"] = config.description
    if config.allowed_local_tools is not None:
        exported["allowed_local_tools"] = list(config.allowed_local_tools)
    if config.mcp_server_names is not None:
        exported["mcp_server_names"] = list(config.mcp_server_names)
    return exported


def _tenant_capability_profiles_public_dict(
    config: TenantCapabilityProfilesConfig,
) -> dict[str, object]:
    exported: dict[str, object] = {}
    if config.default_profile is not None:
        exported["default_profile"] = config.default_profile
    if config.items:
        exported["items"] = [
            _tenant_capability_profile_public_dict(profile)
            for profile in config.items
        ]
    return exported


def _tenant_capability_profile_public_dict(
    config: TenantCapabilityProfileConfig,
) -> dict[str, object]:
    exported: dict[str, object] = {"name": config.name}
    if config.description is not None:
        exported["description"] = config.description
    if config.allowed_local_tools is not None:
        exported["allowed_local_tools"] = list(config.allowed_local_tools)
    if config.mcp_server_names is not None:
        exported["mcp_server_names"] = list(config.mcp_server_names)
    return exported


def _generic_oauth_public_dict(llm: dict[str, object]) -> dict[str, object]:
    if llm.get("provider") != GENERIC_OAUTH_PROVIDER:
        return {}
    env_map = {
        "store_path": OAUTH_STORE_PATH_ENV,
        "provider_id": OAUTH_PROVIDER_ID_ENV,
        "client_id": OAUTH_CLIENT_ID_ENV,
        "authorize_url": OAUTH_AUTHORIZE_URL_ENV,
        "token_url": OAUTH_TOKEN_URL_ENV,
        "redirect_uri": OAUTH_REDIRECT_URI_ENV,
        "scope": OAUTH_SCOPE_ENV,
        "account_id_jwt_claim": OAUTH_ACCOUNT_ID_JWT_CLAIM_ENV,
    }
    exported: dict[str, object] = {}
    for key, env_key in env_map.items():
        value = os.getenv(env_key, "").strip()
        if value:
            exported[key] = value
    auth_params = os.getenv(OAUTH_AUTH_PARAMS_ENV, "").strip()
    if auth_params:
        try:
            parsed = json.loads(auth_params)
        except json.JSONDecodeError:
            exported["auth_params"] = auth_params
        else:
            if isinstance(parsed, dict):
                exported["auth_params"] = parsed
    return exported


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _build_registry_for_config(
    config: TenantExecutionConfig,
    *,
    mcp_manager: MCPServerManager | None,
) -> tuple[ToolRegistry, int]:
    if mcp_manager is None:
        return (
            build_tool_registry(
                mcp_server_configs=config.tools.mcp_servers,
                allowed_local_tools=config.tools.allowed_local_tools,
                result_redaction_policy=config.tools.result_redaction_policy,
            ),
            0,
        )
    snapshot = mcp_manager.snapshot(config.tools.mcp_servers)
    return (
        build_tool_registry(
            mcp_snapshot=snapshot,
            allowed_local_tools=config.tools.allowed_local_tools,
            result_redaction_policy=config.tools.result_redaction_policy,
        ),
        snapshot.generation,
    )


def interpolate_tenant_execution_env_placeholders(
    value: Any,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Recursively replace ${NAME} placeholders in tenant execution config strings."""

    interpolation_env = env if env is not None else os.environ
    if isinstance(value, str):
        return TENANT_ENV_PLACEHOLDER_PATTERN.sub(
            lambda match: interpolation_env.get(match.group(1), ""), value
        )
    if isinstance(value, list):
        return [
            interpolate_tenant_execution_env_placeholders(item, interpolation_env) for item in value
        ]
    if isinstance(value, dict):
        return {
            key: interpolate_tenant_execution_env_placeholders(item, interpolation_env)
            for key, item in value.items()
        }
    return value


def build_execution_resolver_from_env(
    *,
    mcp_manager: MCPServerManager | None = None,
) -> TenantExecutionResolver:
    raw = os.getenv(TENANT_EXECUTION_CONFIGS_ENV, "").strip()
    if not raw:
        mcp_server_configs = load_mcp_server_configs_from_env()
        config = TenantExecutionConfig(
            tenant_id=DEFAULT_TENANT_KEY,
            tools=TenantToolConfig(mcp_servers=mcp_server_configs),
            agent_backend=_agent_backend_config_from_env(),
            quality=_quality_config_from_env(),
        )
        registry, generation = _build_registry_for_config(config, mcp_manager=mcp_manager)
        return FixedTenantExecutionResolver(
            llm_adapter=build_llm_adapter_from_env(),
            tool_registry=registry,
            config=config,
            mcp_manager=mcp_manager,
            mcp_generation=generation,
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{TENANT_EXECUTION_CONFIGS_ENV} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{TENANT_EXECUTION_CONFIGS_ENV} must be a JSON object")

    tenant_configs: dict[str, TenantExecutionConfig] = {}
    parsed = interpolate_tenant_execution_env_placeholders(parsed)
    for tenant_id, value in parsed.items():
        if not isinstance(tenant_id, str) or not tenant_id:
            raise RuntimeError(
                f"{TENANT_EXECUTION_CONFIGS_ENV} keys must be non-empty tenant identifiers"
            )
        if not isinstance(value, dict):
            raise RuntimeError(
                f"{TENANT_EXECUTION_CONFIGS_ENV} values must be objects with llm/tools config"
            )
        tenant_configs[tenant_id] = parse_tenant_execution_config(tenant_id, value)
    return InMemoryTenantExecutionResolver(tenant_configs, mcp_manager=mcp_manager)


def resolve_tenant_config_source(
    explicit_source: str | None = None,
) -> str:
    raw = (
        explicit_source
        if explicit_source is not None
        else os.getenv(
            TENANT_CONFIG_SOURCE_ENV,
            TENANT_CONFIG_SOURCE_ENV_ONLY,
        )
    )
    source = raw.strip().lower()
    if source in {
        TENANT_CONFIG_SOURCE_ENV_ONLY,
        TENANT_CONFIG_SOURCE_STORE,
        TENANT_CONFIG_SOURCE_STORE_WITH_DEFAULTS,
    }:
        return source
    raise RuntimeError(
        f"Unsupported {TENANT_CONFIG_SOURCE_ENV} '{raw}'. Expected "
        f"'{TENANT_CONFIG_SOURCE_ENV_ONLY}', '{TENANT_CONFIG_SOURCE_STORE}', "
        f"or '{TENANT_CONFIG_SOURCE_STORE_WITH_DEFAULTS}'."
    )


def parse_tenant_execution_config(tenant_id: str, payload: dict[str, Any]) -> TenantExecutionConfig:
    llm_payload = payload.get("llm") or {}
    tools_payload = payload.get("tools") or {}
    backend_payload = payload.get("agent_backend") or payload.get("agentBackend") or {}
    quality_payload = payload.get("quality") or {}
    skills_payload = payload.get("skills") or {}
    capability_profiles_payload = (
        payload.get("capability_profiles") or payload.get("capabilityProfiles") or {}
    )
    if not isinstance(llm_payload, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' llm config must be an object")
    if not isinstance(tools_payload, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' tools config must be an object")
    if not isinstance(backend_payload, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' agent_backend config must be an object")
    if not isinstance(quality_payload, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' quality config must be an object")
    if not isinstance(skills_payload, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' skills config must be an object")
    if not isinstance(capability_profiles_payload, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' capability_profiles config must be an object")

    tool_config = _parse_tenant_tool_config(tenant_id, tools_payload)

    return TenantExecutionConfig(
        tenant_id=tenant_id,
        llm=_parse_tenant_llm_config(tenant_id, llm_payload),
        tools=tool_config,
        agent_backend=_parse_tenant_agent_backend_config(tenant_id, backend_payload),
        quality=_parse_tenant_quality_config(tenant_id, quality_payload),
        skills=_parse_tenant_skills_config(tenant_id, skills_payload, tool_config),
        capability_profiles=_parse_tenant_capability_profiles_config(
            tenant_id, capability_profiles_payload, tool_config
        ),
    )


async def validate_tenant_execution_config(
    tenant_id: str,
    payload: dict[str, Any],
) -> TenantExecutionValidationReport:
    tool_errors = _validate_local_tool_policy(tenant_id, payload)
    config_errors: list[str] = []
    config: TenantExecutionConfig | None = None
    try:
        config = parse_tenant_execution_config(tenant_id, payload)
    except RuntimeError as exc:
        config_errors.append(str(exc))

    config_shape = {
        "ok": not config_errors and not tool_errors,
        "errors": [*config_errors, *tool_errors],
    }
    if config is None:
        llm = {
            "ok": False,
            "provider": None,
            "model": None,
            "base_url": None,
            "errors": ["Validation skipped until config shape issues are fixed."],
        }
        tools = {
            "ok": False,
            "errors": ["Validation skipped until config shape issues are fixed."],
            "local_tools": [],
            "unknown_local_tools": sorted(_extract_unknown_local_tools(payload)),
            "mcp_servers": [],
        }
        return TenantExecutionValidationReport(
            valid=False,
            config_shape=config_shape,
            llm=llm,
            tools=tools,
        )

    llm = _validate_llm_config(config)
    tools = await _validate_tool_config(config)
    return TenantExecutionValidationReport(
        valid=config_shape["ok"] and llm["ok"] and tools["ok"],
        config_shape=config_shape,
        llm=llm,
        tools=tools,
    )


def _parse_tenant_llm_config(tenant_id: str, payload: dict[str, Any]) -> TenantLLMConfig:
    provider = str(payload.get("provider", "mock")).strip().lower()
    extra_headers = payload.get("extra_headers") or payload.get("extraHeaders") or {}
    if not isinstance(extra_headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extra_headers.items()
    ):
        raise RuntimeError(f"Tenant '{tenant_id}' LLM extra_headers must be an object of strings")
    timeout_value = payload.get("timeout", 30.0)
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Tenant '{tenant_id}' LLM timeout must be numeric") from exc
    return TenantLLMConfig(
        provider=provider,
        model=_optional_str(payload.get("model")),
        base_url=_optional_str(payload.get("base_url") or payload.get("baseUrl")),
        api_key=_optional_str(payload.get("api_key") or payload.get("apiKey")),
        extra_headers=extra_headers,
        timeout=timeout,
    )


def _parse_tenant_tool_config(tenant_id: str, payload: dict[str, Any]) -> TenantToolConfig:
    allowed_local_tools_raw = payload.get("allowed_local_tools", payload.get("allowedLocalTools"))
    allowed_local_tools: list[str] | None
    if allowed_local_tools_raw is None:
        allowed_local_tools = None
    else:
        if not isinstance(allowed_local_tools_raw, list) or not all(
            isinstance(item, str) for item in allowed_local_tools_raw
        ):
            raise RuntimeError(
                f"Tenant '{tenant_id}' allowed_local_tools must be an array of strings"
            )
        allowed_local_tools = list(allowed_local_tools_raw)

    mcp_servers_raw = payload.get("mcp_servers", payload.get("mcpServers")) or []
    if not isinstance(mcp_servers_raw, list):
        raise RuntimeError(f"Tenant '{tenant_id}' mcp_servers must be an array")
    result_redaction_policy = parse_tool_result_redaction_policy(
        payload.get("result_redaction", payload.get("resultRedaction")),
        context=f"Tenant '{tenant_id}' tools",
    )
    return TenantToolConfig(
        allowed_local_tools=allowed_local_tools,
        mcp_servers=[_parse_mcp_server_config(tenant_id, entry) for entry in mcp_servers_raw],
        result_redaction_policy=result_redaction_policy,
    )


def _parse_tenant_agent_backend_config(
    tenant_id: str,
    payload: dict[str, Any],
) -> TenantAgentBackendConfig:
    backend_type = str(payload.get("type", AGENT_BACKEND_NATIVE)).strip().lower()
    if backend_type not in {AGENT_BACKEND_NATIVE, AGENT_BACKEND_PEER_AGENT}:
        raise RuntimeError(
            f"Tenant '{tenant_id}' agent_backend.type must be '{AGENT_BACKEND_NATIVE}' "
            f"or '{AGENT_BACKEND_PEER_AGENT}'"
        )
    peer = _optional_str(payload.get("peer"))
    cwd = _optional_str(payload.get("cwd"))
    if backend_type == AGENT_BACKEND_PEER_AGENT:
        if peer is None:
            raise RuntimeError(f"Tenant '{tenant_id}' agent_backend.peer is required")
        if cwd is None:
            raise RuntimeError(f"Tenant '{tenant_id}' agent_backend.cwd is required")
    return TenantAgentBackendConfig(
        type=backend_type,
        peer=peer,
        cwd=cwd,
        timeout_seconds=_positive_float_config(
            tenant_id,
            payload.get("timeout_seconds", payload.get("timeoutSeconds", 180.0)),
            "agent_backend.timeout_seconds",
        ),
        poll_interval_seconds=_positive_float_config(
            tenant_id,
            payload.get("poll_interval_seconds", payload.get("pollIntervalSeconds", 1.0)),
            "agent_backend.poll_interval_seconds",
        ),
        mcp_broker_enabled=_bool_config(
            tenant_id,
            payload.get("mcp_broker_enabled", payload.get("mcpBrokerEnabled", True)),
            "agent_backend.mcp_broker_enabled",
        ),
    )


def _agent_backend_config_from_env() -> TenantAgentBackendConfig:
    backend_type = os.getenv(AGENT_BACKEND_ENV, AGENT_BACKEND_NATIVE).strip().lower()
    if backend_type == AGENT_BACKEND_NATIVE:
        return TenantAgentBackendConfig()
    if backend_type != AGENT_BACKEND_PEER_AGENT:
        raise RuntimeError(
            f"Unsupported {AGENT_BACKEND_ENV} '{backend_type}'. Expected "
            f"'{AGENT_BACKEND_NATIVE}' or '{AGENT_BACKEND_PEER_AGENT}'."
        )
    peer = os.getenv(AGENT_BACKEND_PEER_ENV, "").strip()
    cwd = os.getenv(AGENT_BACKEND_CWD_ENV, "").strip()
    if not peer:
        raise RuntimeError(f"{AGENT_BACKEND_PEER_ENV} is required for peer_agent backend")
    if not cwd:
        raise RuntimeError(f"{AGENT_BACKEND_CWD_ENV} is required for peer_agent backend")
    return TenantAgentBackendConfig(
        type=AGENT_BACKEND_PEER_AGENT,
        peer=peer,
        cwd=cwd,
        timeout_seconds=_positive_float_env(AGENT_BACKEND_TIMEOUT_ENV, 180.0),
        poll_interval_seconds=_positive_float_env(AGENT_BACKEND_POLL_INTERVAL_ENV, 1.0),
        mcp_broker_enabled=_bool_env(AGENT_BACKEND_MCP_BROKER_ENABLED_ENV, True),
    )


def _parse_tenant_quality_config(tenant_id: str, payload: dict[str, Any]) -> TenantQualityConfig:
    provider = str(payload.get("provider", "mock")).strip().lower()
    mode = str(payload.get("mode", "critique_draft")).strip().lower()
    if mode != "critique_draft":
        raise RuntimeError(f"Tenant '{tenant_id}' quality.mode must be 'critique_draft'")
    extra_headers = payload.get("extra_headers") or payload.get("extraHeaders") or {}
    if not isinstance(extra_headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extra_headers.items()
    ):
        raise RuntimeError(
            f"Tenant '{tenant_id}' quality extra_headers must be an object of strings"
        )
    timeout_value = payload.get("timeout", 30.0)
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Tenant '{tenant_id}' quality timeout must be numeric") from exc
    max_payload_value = payload.get("max_payload_chars", payload.get("maxPayloadChars", 6000))
    try:
        max_payload_chars = int(max_payload_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Tenant '{tenant_id}' quality max_payload_chars must be an integer"
        ) from exc
    if max_payload_chars < 256:
        raise RuntimeError(f"Tenant '{tenant_id}' quality max_payload_chars must be at least 256")
    return TenantQualityConfig(
        enabled=_bool_config(tenant_id, payload.get("enabled", False), "quality.enabled"),
        mode=mode,
        provider=provider,
        model=_optional_str(payload.get("model")),
        base_url=_optional_str(payload.get("base_url") or payload.get("baseUrl")),
        api_key=_optional_str(payload.get("api_key") or payload.get("apiKey")),
        extra_headers=extra_headers,
        timeout=timeout,
        max_payload_chars=max_payload_chars,
    )


def _quality_config_from_env() -> TenantQualityConfig:
    max_payload_chars = int(_positive_float_env(QUALITY_MAX_PAYLOAD_CHARS_ENV, 6000.0))
    if max_payload_chars < 256:
        raise RuntimeError(f"{QUALITY_MAX_PAYLOAD_CHARS_ENV} must be at least 256")
    return TenantQualityConfig(
        enabled=_bool_env(QUALITY_ENABLED_ENV, False),
        mode=os.getenv(QUALITY_MODE_ENV, "critique_draft").strip().lower(),
        provider=os.getenv(QUALITY_PROVIDER_ENV, "mock").strip().lower(),
        model=os.getenv(QUALITY_MODEL_ENV, "").strip() or None,
        base_url=os.getenv(QUALITY_BASE_URL_ENV, "").strip() or None,
        api_key=os.getenv(QUALITY_API_KEY_ENV, "").strip() or None,
        timeout=_positive_float_env(QUALITY_TIMEOUT_ENV, 30.0),
        max_payload_chars=max_payload_chars,
    )


def _parse_tenant_skills_config(
    tenant_id: str,
    payload: dict[str, Any],
    tool_config: TenantToolConfig,
) -> TenantSkillsConfig:
    default_skill = _optional_str(payload.get("default_skill") or payload.get("defaultSkill"))
    items_raw = payload.get("items") or []
    if not isinstance(items_raw, list):
        raise RuntimeError(f"Tenant '{tenant_id}' skills.items must be an array")

    allowed_local_tools = (
        set(tool_config.allowed_local_tools)
        if tool_config.allowed_local_tools is not None
        else None
    )
    configured_mcp_server_names = {server.name for server in tool_config.mcp_servers}
    seen_names: set[str] = set()
    items: list[TenantSkillConfig] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            raise RuntimeError(f"Tenant '{tenant_id}' skills.items entries must be objects")
        name = _required_non_empty_str(tenant_id, entry.get("name"), "skills.items[].name")
        if name in seen_names:
            raise RuntimeError(f"Tenant '{tenant_id}' skill '{name}' is defined more than once")
        seen_names.add(name)
        system_prompt = _required_non_empty_str(
            tenant_id,
            entry.get("system_prompt") or entry.get("systemPrompt"),
            f"skill '{name}' system_prompt",
        )
        skill_allowed_local_tools = _optional_str_list(
            tenant_id,
            entry.get("allowed_local_tools") or entry.get("allowedLocalTools"),
            f"skill '{name}' allowed_local_tools",
        )
        if skill_allowed_local_tools is not None:
            unknown_tools = sorted(set(skill_allowed_local_tools) - LOCAL_TOOL_NAMES)
            if unknown_tools:
                raise RuntimeError(
                    f"Tenant '{tenant_id}' skill '{name}' references unknown local tools: "
                    + ", ".join(unknown_tools)
                )
            if allowed_local_tools is not None:
                disallowed_tools = sorted(set(skill_allowed_local_tools) - allowed_local_tools)
                if disallowed_tools:
                    raise RuntimeError(
                        f"Tenant '{tenant_id}' skill '{name}' local tools must be a subset of tenant tools: "
                        + ", ".join(disallowed_tools)
                    )
        mcp_server_names = _optional_str_list(
            tenant_id,
            entry.get("mcp_server_names") or entry.get("mcpServerNames"),
            f"skill '{name}' mcp_server_names",
        )
        if mcp_server_names is not None:
            unknown_servers = sorted(set(mcp_server_names) - configured_mcp_server_names)
            if unknown_servers:
                raise RuntimeError(
                    f"Tenant '{tenant_id}' skill '{name}' references unknown MCP servers: "
                    + ", ".join(unknown_servers)
                )
        items.append(
            TenantSkillConfig(
                name=name,
                description=_optional_str(entry.get("description")),
                system_prompt=system_prompt,
                allowed_local_tools=skill_allowed_local_tools,
                mcp_server_names=mcp_server_names,
            )
        )

    if default_skill is not None and default_skill not in seen_names:
        raise RuntimeError(
            f"Tenant '{tenant_id}' skills.default_skill must reference a configured skill"
        )
    return TenantSkillsConfig(default_skill=default_skill, items=items)


def _parse_tenant_capability_profiles_config(
    tenant_id: str,
    payload: dict[str, Any],
    tool_config: TenantToolConfig,
) -> TenantCapabilityProfilesConfig:
    default_profile = _optional_str(payload.get("default_profile") or payload.get("defaultProfile"))
    items_raw = payload.get("items") or []
    if not isinstance(items_raw, list):
        raise RuntimeError(f"Tenant '{tenant_id}' capability_profiles.items must be an array")

    allowed_local_tools = (
        set(tool_config.allowed_local_tools)
        if tool_config.allowed_local_tools is not None
        else None
    )
    configured_mcp_server_names = {server.name for server in tool_config.mcp_servers}
    seen_names: set[str] = set()
    items: list[TenantCapabilityProfileConfig] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"Tenant '{tenant_id}' capability_profiles.items entries must be objects"
            )
        name = _required_non_empty_str(
            tenant_id, entry.get("name"), "capability_profiles.items[].name"
        )
        if name in seen_names:
            raise RuntimeError(
                f"Tenant '{tenant_id}' capability profile '{name}' is defined more than once"
            )
        seen_names.add(name)
        profile_allowed_local_tools = _optional_str_list(
            tenant_id,
            entry.get("allowed_local_tools") or entry.get("allowedLocalTools"),
            f"capability profile '{name}' allowed_local_tools",
        )
        if profile_allowed_local_tools is not None:
            unknown_tools = sorted(set(profile_allowed_local_tools) - LOCAL_TOOL_NAMES)
            if unknown_tools:
                raise RuntimeError(
                    f"Tenant '{tenant_id}' capability profile '{name}' references unknown local tools: "
                    + ", ".join(unknown_tools)
                )
            if allowed_local_tools is not None:
                disallowed_tools = sorted(set(profile_allowed_local_tools) - allowed_local_tools)
                if disallowed_tools:
                    raise RuntimeError(
                        f"Tenant '{tenant_id}' capability profile '{name}' local tools must be a subset of tenant tools: "
                        + ", ".join(disallowed_tools)
                    )
        mcp_server_names = _optional_str_list(
            tenant_id,
            entry.get("mcp_server_names") or entry.get("mcpServerNames"),
            f"capability profile '{name}' mcp_server_names",
        )
        if mcp_server_names is not None:
            unknown_servers = sorted(set(mcp_server_names) - configured_mcp_server_names)
            if unknown_servers:
                raise RuntimeError(
                    f"Tenant '{tenant_id}' capability profile '{name}' references unknown MCP servers: "
                    + ", ".join(unknown_servers)
                )
        items.append(
            TenantCapabilityProfileConfig(
                name=name,
                description=_optional_str(entry.get("description")),
                allowed_local_tools=profile_allowed_local_tools,
                mcp_server_names=mcp_server_names,
            )
        )
    if default_profile is not None and default_profile not in seen_names:
        raise RuntimeError(
            f"Tenant '{tenant_id}' capability_profiles.default_profile must reference a configured capability profile"
        )
    return TenantCapabilityProfilesConfig(default_profile=default_profile, items=items)


def _parse_mcp_server_config(tenant_id: str, entry: Any) -> MCPServerConfig:
    if not isinstance(entry, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' mcp_servers entries must be objects")
    name = entry.get("name")
    url = entry.get("url")
    headers = entry.get("headers") or {}
    protocol_version = entry.get("protocolVersion") or entry.get("protocol_version") or "2025-11-25"
    allowed_tools = _optional_str_list(
        tenant_id,
        entry.get("allowed_tools") or entry.get("allowedTools"),
        f"mcp server '{name}' allowed_tools",
    )
    path_policy = _parse_mcp_path_policy(
        tenant_id,
        name,
        entry.get("path_policy") or entry.get("pathPolicy"),
    )
    result_redaction_policy = parse_tool_result_redaction_policy(
        entry.get("result_redaction", entry.get("resultRedaction")),
        context=f"Tenant '{tenant_id}' mcp server '{name}'",
    )
    timeout_seconds = _positive_float_config(
        tenant_id,
        entry.get("timeout_seconds", entry.get("timeoutSeconds", 30.0)),
        f"mcp server '{name}' timeout_seconds",
    )
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"Tenant '{tenant_id}' mcp server name must be a non-empty string")
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"Tenant '{tenant_id}' mcp server url must be a non-empty string")
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise RuntimeError(f"Tenant '{tenant_id}' mcp server headers must be a string map")
    return MCPServerConfig(
        name=name,
        url=url,
        headers=headers,
        protocol_version=str(protocol_version),
        allowed_tools=allowed_tools,
        path_policy=path_policy,
        result_redaction_policy=result_redaction_policy,
        timeout_seconds=timeout_seconds,
    )


def _parse_mcp_path_policy(
    tenant_id: str,
    server_name: object,
    raw: object,
) -> MCPPathPolicy:
    if raw is None:
        return MCPPathPolicy()
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"Tenant '{tenant_id}' mcp server '{server_name}' path_policy must be an object"
        )
    deny_globs = _optional_str_list(
        tenant_id,
        raw.get("deny_globs") or raw.get("denyGlobs"),
        f"mcp server '{server_name}' path_policy.deny_globs",
    )
    allow_globs = _optional_str_list(
        tenant_id,
        raw.get("allow_globs") or raw.get("allowGlobs"),
        f"mcp server '{server_name}' path_policy.allow_globs",
    )
    return MCPPathPolicy(deny_globs=deny_globs or [], allow_globs=allow_globs or [])


def _build_llm_adapter(config: TenantLLMConfig) -> LLMAdapter:
    if config.provider == "mock":
        return MockLLMAdapter()
    if config.provider == "generic-oauth":
        if not config.model:
            raise RuntimeError("Tenant LLM provider 'generic-oauth' requires model")
        if not config.base_url:
            raise RuntimeError("Tenant LLM provider 'generic-oauth' requires base_url")
        return GenericOAuthResponsesAdapter(
            url=config.base_url,
            model=config.model,
            extra_headers=config.extra_headers,
            timeout=config.timeout,
        )
    if config.provider in {"google", "google-generative-ai", "gemini"}:
        if not config.api_key:
            raise RuntimeError(f"Tenant LLM provider '{config.provider}' requires api_key")
        if not config.model:
            raise RuntimeError(f"Tenant LLM provider '{config.provider}' requires model")
        return GoogleGeminiAdapter(
            base_url=config.base_url or "https://generativelanguage.googleapis.com/v1beta",
            api_key=config.api_key,
            model=config.model,
            extra_headers=config.extra_headers,
            timeout=config.timeout,
        )
    if config.provider in {"openai", "openrouter", "openai-compatible"}:
        if not config.api_key:
            raise RuntimeError(f"Tenant LLM provider '{config.provider}' requires api_key")
        if not config.model:
            raise RuntimeError(f"Tenant LLM provider '{config.provider}' requires model")
        base_url = config.base_url or _default_base_url_for_provider(config.provider)
        return OpenAICompatibleAdapter(
            base_url=base_url,
            api_key=config.api_key,
            model=config.model,
            extra_headers=config.extra_headers,
            timeout=config.timeout,
        )
    raise RuntimeError(f"Unsupported tenant LLM provider '{config.provider}'")


def _default_base_url_for_provider(provider: str) -> str:
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if provider in {"google", "google-generative-ai", "gemini"}:
        return "https://generativelanguage.googleapis.com/v1beta"
    return "https://api.openai.com/v1"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("Expected string value")
    stripped = value.strip()
    return stripped or None


def _positive_float_config(tenant_id: str, value: object, label: str) -> float:
    if not isinstance(value, str | int | float):
        raise RuntimeError(f"Tenant '{tenant_id}' {label} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Tenant '{tenant_id}' {label} must be numeric") from exc
    if parsed <= 0:
        raise RuntimeError(f"Tenant '{tenant_id}' {label} must be positive")
    return parsed


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be positive")
    return parsed


def _bool_config(tenant_id: str, value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise RuntimeError(f"Tenant '{tenant_id}' {label} must be boolean")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be boolean")


def _agent_backend_public_dict(config: TenantAgentBackendConfig) -> dict[str, object]:
    payload: dict[str, object] = {"type": config.type}
    if config.peer is not None:
        payload["peer"] = config.peer
    if config.cwd is not None:
        payload["cwd"] = config.cwd
    payload["timeout_seconds"] = config.timeout_seconds
    payload["poll_interval_seconds"] = config.poll_interval_seconds
    payload["mcp_broker_enabled"] = config.mcp_broker_enabled
    return payload


def _quality_public_dict(config: TenantQualityConfig) -> dict[str, object]:
    return {
        "enabled": config.enabled,
        "mode": config.mode,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "headers": sorted(config.extra_headers.keys()),
        "timeout": config.timeout,
        "max_payload_chars": config.max_payload_chars,
    }


def _required_non_empty_str(tenant_id: str, value: object, label: str) -> str:
    try:
        parsed = _optional_str(value)
    except RuntimeError as exc:
        raise RuntimeError(f"Tenant '{tenant_id}' {label} must be a string") from exc
    if parsed is None:
        raise RuntimeError(f"Tenant '{tenant_id}' {label} must be a non-empty string")
    return parsed


def _optional_str_list(tenant_id: str, value: object, label: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"Tenant '{tenant_id}' {label} must be an array of strings")
    return list(value)


def _validate_local_tool_policy(tenant_id: str, payload: dict[str, Any]) -> list[str]:
    tools_payload = payload.get("tools") or {}
    if not isinstance(tools_payload, dict):
        return []
    allowed_local_tools_raw = tools_payload.get(
        "allowed_local_tools", tools_payload.get("allowedLocalTools")
    )
    if allowed_local_tools_raw is None:
        return []
    if not isinstance(allowed_local_tools_raw, list) or not all(
        isinstance(item, str) for item in allowed_local_tools_raw
    ):
        return []
    unknown_tools = sorted(set(allowed_local_tools_raw) - LOCAL_TOOL_NAMES)
    if not unknown_tools:
        return []
    return [
        f"Tenant '{tenant_id}' allowed_local_tools references unknown local tools: "
        + ", ".join(unknown_tools)
    ]


def _extract_unknown_local_tools(payload: dict[str, Any]) -> set[str]:
    tools_payload = payload.get("tools") or {}
    if not isinstance(tools_payload, dict):
        return set()
    allowed_local_tools_raw = tools_payload.get(
        "allowed_local_tools", tools_payload.get("allowedLocalTools")
    )
    if not isinstance(allowed_local_tools_raw, list):
        return set()
    return {
        tool
        for tool in allowed_local_tools_raw
        if isinstance(tool, str) and tool not in LOCAL_TOOL_NAMES
    }


def _validate_llm_config(config: TenantExecutionConfig) -> dict[str, Any]:
    errors: list[str] = []
    try:
        adapter = _build_llm_adapter(config.llm)
    except RuntimeError as exc:
        errors.append(str(exc))
        adapter = None

    if adapter is not None:
        adapter_details = adapter.describe()
        provider = adapter_details.get("provider")
        base_url = adapter_details.get("base_url")
        model = adapter_details.get("model")
    else:
        provider = config.llm.provider
        base_url = config.llm.base_url or (
            _default_base_url_for_provider(config.llm.provider)
            if config.llm.provider
            in {
                "openai",
                "openrouter",
                "openai-compatible",
                "google",
                "google-generative-ai",
                "gemini",
            }
            else None
        )
        model = config.llm.model
    return {
        "ok": not errors,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "errors": errors,
    }


async def _validate_tool_config(config: TenantExecutionConfig) -> dict[str, Any]:
    local_tools = sorted(config.tools.allowed_local_tools or list(DEFAULT_LOCAL_TOOL_NAMES))
    unknown_local_tools = sorted(
        tool for tool in (config.tools.allowed_local_tools or []) if tool not in LOCAL_TOOL_NAMES
    )
    mcp_server_reports: list[dict[str, Any]] = []
    for server in config.tools.mcp_servers:
        mcp_server_reports.append(await _validate_mcp_server(server))
    tool_errors = [report["error"] for report in mcp_server_reports if report["error"]]
    return {
        "ok": not tool_errors and not unknown_local_tools,
        "errors": tool_errors,
        "local_tools": local_tools,
        "unknown_local_tools": unknown_local_tools,
        "mcp_servers": mcp_server_reports,
    }


async def _validate_mcp_server(server: MCPServerConfig) -> dict[str, Any]:
    client = MCPHTTPClient(server)
    try:
        specs = await client.list_tools()
        info = client.server_info()
        return {
            "name": info.name,
            "url": info.url,
            "ok": True,
            "error": None,
            "tool_count": len(specs),
            "allowed_tools": server.allowed_tools,
            "path_policy": {
                "deny_globs": list(server.path_policy.deny_globs),
                "allow_globs": list(server.path_policy.allow_globs),
            },
            "protocol_version": info.protocol_version,
            "session": bool(info.session_id),
            "server_name": info.server_name,
            "server_version": info.server_version,
        }
    except HTTPException as exc:
        return {
            "name": server.name,
            "url": server.url,
            "ok": False,
            "error": str(exc.detail),
            "tool_count": 0,
            "allowed_tools": server.allowed_tools,
            "path_policy": {
                "deny_globs": list(server.path_policy.deny_globs),
                "allow_globs": list(server.path_policy.allow_globs),
            },
            "protocol_version": server.protocol_version,
            "session": False,
            "server_name": None,
            "server_version": None,
        }


def get_skill_config(
    config: TenantExecutionConfig,
    skill_name: str | None = None,
) -> TenantSkillConfig | None:
    resolved_skill_name = skill_name or config.skills.default_skill
    if resolved_skill_name is None:
        return None
    for skill in config.skills.items:
        if skill.name == resolved_skill_name:
            return skill
    raise HTTPException(
        status_code=400,
        detail=f"Unknown skill '{resolved_skill_name}' for tenant '{config.tenant_id}'",
    )


def get_skill_configs(
    config: TenantExecutionConfig,
    skill_names: list[str] | None = None,
) -> list[TenantSkillConfig]:
    resolved_skill_names = skill_names
    if resolved_skill_names is None:
        if config.skills.default_skill is None:
            return []
        resolved_skill_names = [config.skills.default_skill]
    resolved: list[TenantSkillConfig] = []
    for name in resolved_skill_names:
        skill = get_skill_config(config, name)
        if skill is not None:
            resolved.append(skill)
    return resolved


def get_capability_profile(
    config: TenantExecutionConfig,
    profile_name: str | None = None,
) -> TenantCapabilityProfileConfig | None:
    resolved_profile_name = profile_name or config.capability_profiles.default_profile
    if resolved_profile_name is None:
        return None
    for profile in config.capability_profiles.items:
        if profile.name == resolved_profile_name:
            return profile
    raise HTTPException(
        status_code=400,
        detail=(
            f"Unknown capability profile '{resolved_profile_name}' for tenant '{config.tenant_id}'"
        ),
    )


def build_tool_registry_for_skill(
    config: TenantExecutionConfig,
    skill_name: str | None = None,
    *,
    mcp_manager: MCPServerManager | None = None,
) -> ToolRegistry:
    skill = get_skill_config(config, skill_name)
    allowed_local_tools = config.tools.allowed_local_tools
    # Skills only narrow tenant permissions for a thread. They never expand the
    # tenant-level local tool or MCP server allowlists.
    if skill is not None and skill.allowed_local_tools is not None:
        if allowed_local_tools is None:
            allowed_local_tools = list(skill.allowed_local_tools)
        else:
            allowed_local_tools = sorted(set(allowed_local_tools) & set(skill.allowed_local_tools))
    mcp_servers = config.tools.mcp_servers
    if skill is not None and skill.mcp_server_names is not None:
        allowed_mcp_server_names = set(skill.mcp_server_names)
        mcp_servers = [server for server in mcp_servers if server.name in allowed_mcp_server_names]
    skill_config = TenantExecutionConfig(
        tenant_id=config.tenant_id,
        llm=config.llm,
        tools=TenantToolConfig(
            allowed_local_tools=allowed_local_tools,
            mcp_servers=mcp_servers,
            result_redaction_policy=config.tools.result_redaction_policy,
        ),
        skills=config.skills,
    )
    return _build_registry_for_config(skill_config, mcp_manager=mcp_manager)[0]


def build_tool_registry_for_capability_profile(
    config: TenantExecutionConfig,
    capability_profile: str | None = None,
    *,
    mcp_manager: MCPServerManager | None = None,
) -> ToolRegistry:
    profile = get_capability_profile(config, capability_profile)
    if profile is None:
        return _build_registry_for_config(config, mcp_manager=mcp_manager)[0]

    allowed_local_tools = config.tools.allowed_local_tools
    if profile.allowed_local_tools is not None:
        if allowed_local_tools is None:
            allowed_local_tools = list(profile.allowed_local_tools)
        else:
            allowed_local_tools = sorted(
                set(allowed_local_tools) & set(profile.allowed_local_tools)
            )
    mcp_servers = config.tools.mcp_servers
    if profile.mcp_server_names is not None:
        allowed_mcp_server_names = set(profile.mcp_server_names)
        mcp_servers = [server for server in mcp_servers if server.name in allowed_mcp_server_names]

    profile_config = TenantExecutionConfig(
        tenant_id=config.tenant_id,
        llm=config.llm,
        tools=TenantToolConfig(
            allowed_local_tools=allowed_local_tools,
            mcp_servers=mcp_servers,
            result_redaction_policy=config.tools.result_redaction_policy,
        ),
        skills=config.skills,
        capability_profiles=config.capability_profiles,
    )
    return _build_registry_for_config(profile_config, mcp_manager=mcp_manager)[0]


def redact_tenant_execution_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(payload))
    llm = redacted.get("llm")
    if isinstance(llm, dict) and llm.get("api_key"):
        llm["api_key"] = "<redacted>"
        llm["has_api_key"] = True
    tools = redacted.get("tools")
    if isinstance(tools, dict):
        mcp_servers = tools.get("mcp_servers") or tools.get("mcpServers")
        if isinstance(mcp_servers, list):
            for server in mcp_servers:
                if isinstance(server, dict):
                    headers = server.get("headers")
                    if isinstance(headers, dict):
                        server["headers"] = {key: "<redacted>" for key in headers}
                        server["has_headers"] = bool(headers)
    return redacted
