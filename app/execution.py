from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from fastapi import HTTPException

from app.admin_store import SQLiteTenantConfigStore
from app.llm import LLMAdapter, MockLLMAdapter, OpenAICompatibleAdapter, build_llm_adapter_from_env
from app.mcp import MCPServerConfig
from app.tools import ToolRegistry, build_tool_registry, build_tool_registry_from_env

TENANT_EXECUTION_CONFIGS_ENV = "MINIGENT_TENANT_EXECUTION_CONFIGS"
TENANT_CONFIG_SOURCE_ENV = "MINIGENT_TENANT_CONFIG_SOURCE"
DEFAULT_TENANT_KEY = "*"
TENANT_CONFIG_SOURCE_ENV_ONLY = "env"
TENANT_CONFIG_SOURCE_STORE = "store"
TENANT_CONFIG_SOURCE_STORE_WITH_DEFAULTS = "store-with-defaults"


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


@dataclass(frozen=True)
class TenantExecutionConfig:
    tenant_id: str
    llm: TenantLLMConfig = field(default_factory=TenantLLMConfig)
    tools: TenantToolConfig = field(default_factory=TenantToolConfig)


@dataclass(frozen=True)
class TenantExecutionContext:
    llm_adapter: LLMAdapter
    tool_registry: ToolRegistry
    config: TenantExecutionConfig


class TenantExecutionResolver:
    def resolve(self, tenant_id: str) -> TenantExecutionContext:
        raise NotImplementedError

    def describe(self, tenant_id: str | None = None) -> dict[str, object]:
        raise NotImplementedError

    def invalidate(self, tenant_id: str) -> None:
        _ = tenant_id


class FixedTenantExecutionResolver(TenantExecutionResolver):
    def __init__(self, llm_adapter: LLMAdapter, tool_registry: ToolRegistry) -> None:
        self._context = TenantExecutionContext(
            llm_adapter=llm_adapter,
            tool_registry=tool_registry,
            config=TenantExecutionConfig(tenant_id=DEFAULT_TENANT_KEY),
        )

    def resolve(self, tenant_id: str) -> TenantExecutionContext:
        _ = tenant_id
        return self._context

    def describe(self, tenant_id: str | None = None) -> dict[str, object]:
        _ = tenant_id
        return {
            "tenant_id": DEFAULT_TENANT_KEY,
            "llm": self._context.llm_adapter.describe(),
            "mcp_servers": self._context.tool_registry.mcp_servers(),
            "local_tools": sorted(
                spec.name for spec in self._context.tool_registry.specs() if "." not in spec.name
            ),
        }


class InMemoryTenantExecutionResolver(TenantExecutionResolver):
    def __init__(
        self,
        tenant_configs: dict[str, TenantExecutionConfig],
        *,
        default_context: TenantExecutionContext | None = None,
    ) -> None:
        self._tenant_configs = dict(tenant_configs)
        self._default_context = default_context
        self._contexts: dict[str, TenantExecutionContext] = {}
        self._lock = Lock()

    def resolve(self, tenant_id: str) -> TenantExecutionContext:
        with self._lock:
            context = self._contexts.get(tenant_id)
            if context is not None:
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

            context = TenantExecutionContext(
                llm_adapter=_build_llm_adapter(config.llm),
                tool_registry=build_tool_registry(
                    mcp_server_configs=config.tools.mcp_servers,
                    allowed_local_tools=config.tools.allowed_local_tools,
                ),
                config=config,
            )
            self._contexts[tenant_id] = context
            return context

    def describe(self, tenant_id: str | None = None) -> dict[str, object]:
        if tenant_id is None:
            if self._default_context is not None:
                return {
                    "tenant_id": DEFAULT_TENANT_KEY,
                    "llm": self._default_context.llm_adapter.describe(),
                    "mcp_servers": self._default_context.tool_registry.mcp_servers(),
                    "local_tools": sorted(
                        spec.name
                        for spec in self._default_context.tool_registry.specs()
                        if "." not in spec.name
                    ),
                }
            if DEFAULT_TENANT_KEY not in self._tenant_configs and self._tenant_configs:
                tenant_id = sorted(self._tenant_configs)[0]
            else:
                tenant_id = DEFAULT_TENANT_KEY

        context = self.resolve(tenant_id)
        return {
            "tenant_id": tenant_id,
            "llm": context.llm_adapter.describe(),
            "mcp_servers": context.tool_registry.mcp_servers(),
            "local_tools": sorted(
                spec.name for spec in context.tool_registry.specs() if "." not in spec.name
            ),
        }

    def invalidate(self, tenant_id: str) -> None:
        with self._lock:
            self._contexts.pop(tenant_id, None)


class StoreBackedTenantExecutionResolver(TenantExecutionResolver):
    def __init__(
        self,
        store: SQLiteTenantConfigStore,
        *,
        fallback_resolver: TenantExecutionResolver | None = None,
    ) -> None:
        self._store = store
        self._fallback_resolver = fallback_resolver
        self._contexts: dict[str, TenantExecutionContext] = {}
        self._lock = Lock()

    def resolve(self, tenant_id: str) -> TenantExecutionContext:
        with self._lock:
            context = self._contexts.get(tenant_id)
            if context is not None:
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
            context = TenantExecutionContext(
                llm_adapter=_build_llm_adapter(config.llm),
                tool_registry=build_tool_registry(
                    mcp_server_configs=config.tools.mcp_servers,
                    allowed_local_tools=config.tools.allowed_local_tools,
                ),
                config=config,
            )
            self._contexts[tenant_id] = context
            return context

    def describe(self, tenant_id: str | None = None) -> dict[str, object]:
        if tenant_id is not None:
            context = self.resolve(tenant_id)
            return {
                "tenant_id": tenant_id,
                "llm": context.llm_adapter.describe(),
                "mcp_servers": context.tool_registry.mcp_servers(),
                "local_tools": sorted(
                    spec.name for spec in context.tool_registry.specs() if "." not in spec.name
                ),
            }
        if self._fallback_resolver is not None:
            return self._fallback_resolver.describe()
        return {"tenant_id": None, "llm": None, "mcp_servers": [], "local_tools": []}

    def invalidate(self, tenant_id: str) -> None:
        with self._lock:
            self._contexts.pop(tenant_id, None)


def build_execution_resolver_from_env() -> TenantExecutionResolver:
    raw = os.getenv(TENANT_EXECUTION_CONFIGS_ENV, "").strip()
    if not raw:
        return FixedTenantExecutionResolver(
            llm_adapter=build_llm_adapter_from_env(),
            tool_registry=build_tool_registry_from_env(),
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{TENANT_EXECUTION_CONFIGS_ENV} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{TENANT_EXECUTION_CONFIGS_ENV} must be a JSON object")

    tenant_configs: dict[str, TenantExecutionConfig] = {}
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
    return InMemoryTenantExecutionResolver(tenant_configs)


def resolve_tenant_config_source(
    explicit_source: str | None = None,
) -> str:
    raw = explicit_source if explicit_source is not None else os.getenv(
        TENANT_CONFIG_SOURCE_ENV,
        TENANT_CONFIG_SOURCE_ENV_ONLY,
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


def parse_tenant_execution_config(
    tenant_id: str, payload: dict[str, Any]
) -> TenantExecutionConfig:
    llm_payload = payload.get("llm") or {}
    tools_payload = payload.get("tools") or {}
    if not isinstance(llm_payload, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' llm config must be an object")
    if not isinstance(tools_payload, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' tools config must be an object")

    return TenantExecutionConfig(
        tenant_id=tenant_id,
        llm=_parse_tenant_llm_config(tenant_id, llm_payload),
        tools=_parse_tenant_tool_config(tenant_id, tools_payload),
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
    return TenantToolConfig(
        allowed_local_tools=allowed_local_tools,
        mcp_servers=[_parse_mcp_server_config(tenant_id, entry) for entry in mcp_servers_raw],
    )


def _parse_mcp_server_config(tenant_id: str, entry: Any) -> MCPServerConfig:
    if not isinstance(entry, dict):
        raise RuntimeError(f"Tenant '{tenant_id}' mcp_servers entries must be objects")
    name = entry.get("name")
    url = entry.get("url")
    headers = entry.get("headers") or {}
    protocol_version = entry.get("protocolVersion") or entry.get("protocol_version") or "2025-11-25"
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
    )


def _build_llm_adapter(config: TenantLLMConfig) -> LLMAdapter:
    if config.provider == "mock":
        return MockLLMAdapter()
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
    return "https://api.openai.com/v1"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("Expected string value")
    stripped = value.strip()
    return stripped or None


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
