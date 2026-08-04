from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.execution import (
    TenantAgentPresetConfig,
    TenantCapabilityProfileConfig,
    TenantExecutionConfig,
    TenantSkillConfig,
)
from app.mcp import (
    DEFAULT_MCP_PROTOCOL_VERSION,
    MCPServerConfig,
    parse_mcp_private_value_policy,
    parse_mcp_private_value_tool_policies,
)
from app.network_policy import validate_public_https_url
from app.redaction import parse_tool_result_redaction_policy
from app.tools import DEFAULT_LOCAL_TOOL_NAMES

_USER_RESOURCE_ID_PATTERN = re.compile(r"^user:[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_RESOURCE_REF_PATTERN = re.compile(r"^(?:user|shared):[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_CREDENTIAL_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$")
_FORBIDDEN_CREDENTIAL_HEADER_NAMES = frozenset(
    {
        "host",
        "connection",
        "content-length",
        "transfer-encoding",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "proxy-authorization",
    }
)
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "cookie",
        "set-cookie",
        "host",
        "connection",
        "content-length",
        "transfer-encoding",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
    }
)


class UserExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UserSkillDefinition(UserExecutionModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str | None = Field(
        default=None,
        max_length=200_000,
        validation_alias=AliasChoices("system_prompt", "systemPrompt"),
    )
    workspace_scope: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("workspace_scope", "workspaceScope"),
    )

    @model_validator(mode="after")
    def require_system_prompt(self) -> UserSkillDefinition:
        if self.system_prompt is None:
            raise ValueError("personal skill system_prompt is required")
        return self

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_user_resource_id(value)


class UserMCPServerDefinition(UserExecutionModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    url: str = Field(min_length=1, max_length=4096)
    credential_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices("credential_ref", "credentialRef"),
    )
    headers: dict[str, str] = Field(default_factory=dict)
    protocol_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("protocol_version", "protocolVersion"),
    )
    allowed_tools: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("allowed_tools", "allowedTools"),
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=600,
        validation_alias=AliasChoices("timeout_seconds", "timeoutSeconds"),
    )
    path_policy: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("path_policy", "pathPolicy"),
    )
    result_redaction: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("result_redaction", "resultRedaction"),
    )
    private_value_policy: str | dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("private_value_policy", "privateValuePolicy"),
    )
    private_value_tool_policies: dict[str, dict[str, Any]] | None = Field(
        default=None,
        validation_alias=AliasChoices("private_value_tool_policies", "privateValueToolPolicies"),
    )
    trusted_input_preprocessor_tools: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "trusted_input_preprocessor_tools", "trustedInputPreprocessorTools"
        ),
    )

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_user_resource_id(value)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must be an absolute HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("url must not contain embedded credentials")
        return value

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        malformed = sorted(
            name
            for name, header_value in value.items()
            if not name.strip()
            or "\r" in name
            or "\n" in name
            or "\r" in header_value
            or "\n" in header_value
        )
        if malformed:
            raise ValueError("header names and values must not be empty or contain newlines")
        sensitive = sorted(name for name in value if name.lower() in _SENSITIVE_HEADER_NAMES)
        if sensitive:
            raise ValueError(
                "reusable credential headers must use credential_ref: " + ", ".join(sensitive)
            )
        return value

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str | None) -> str | None:
        if value is not None and not _CREDENTIAL_REF_PATTERN.fullmatch(value):
            raise ValueError("credential_ref contains unsupported characters")
        return value

    @field_validator("allowed_tools", "trusted_input_preprocessor_tools")
    @classmethod
    def validate_tool_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not item.strip() for item in value):
            raise ValueError("tool names must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("tool names must not contain duplicates")
        return value


class UserCapabilityProfileDefinition(UserExecutionModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    mcp_server_refs: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("mcp_server_refs", "mcpServerRefs"),
    )
    allowed_local_tools: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("allowed_local_tools", "allowedLocalTools"),
    )

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_user_resource_id(value)

    @field_validator("mcp_server_refs")
    @classmethod
    def validate_server_refs(cls, value: list[str]) -> list[str]:
        return _validate_resource_refs(value, "mcp_server_refs")

    @field_validator("allowed_local_tools")
    @classmethod
    def validate_local_tools(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not item.strip() for item in value):
            raise ValueError("allowed_local_tools entries must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("allowed_local_tools must not contain duplicates")
        return value


class UserAgentDefinition(UserExecutionModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    skill_refs: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("skill_refs", "skillRefs"),
    )
    capability_profile_ref: str | None = Field(
        default=None,
        validation_alias=AliasChoices("capability_profile_ref", "capabilityProfileRef"),
    )

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_user_resource_id(value)

    @field_validator("skill_refs")
    @classmethod
    def validate_skill_refs(cls, value: list[str]) -> list[str]:
        return _validate_resource_refs(value, "skill_refs")

    @field_validator("capability_profile_ref")
    @classmethod
    def validate_profile_ref(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_resource_ref(value)
        return value


class UserExecutionDefaults(UserExecutionModel):
    agent_ref: str | None = Field(
        default=None,
        validation_alias=AliasChoices("agent_ref", "agentRef"),
    )
    skill_refs: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("skill_refs", "skillRefs"),
    )
    capability_profile_ref: str | None = Field(
        default=None,
        validation_alias=AliasChoices("capability_profile_ref", "capabilityProfileRef"),
    )

    @field_validator("agent_ref", "capability_profile_ref")
    @classmethod
    def validate_optional_ref(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_resource_ref(value)
        return value

    @field_validator("skill_refs")
    @classmethod
    def validate_default_skill_refs(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_resource_refs(value, "skill_refs")


class UserSkillCollection(UserExecutionModel):
    items: list[UserSkillDefinition] = Field(default_factory=list)


class UserMCPServerCollection(UserExecutionModel):
    items: list[UserMCPServerDefinition] = Field(default_factory=list)


class UserCapabilityProfileCollection(UserExecutionModel):
    items: list[UserCapabilityProfileDefinition] = Field(default_factory=list)


class UserAgentCollection(UserExecutionModel):
    items: list[UserAgentDefinition] = Field(default_factory=list)


class UserExecutionConfig(UserExecutionModel):
    defaults: UserExecutionDefaults = Field(default_factory=UserExecutionDefaults)
    skills: UserSkillCollection = Field(default_factory=UserSkillCollection)
    mcp_servers: UserMCPServerCollection = Field(
        default_factory=UserMCPServerCollection,
        validation_alias=AliasChoices("mcp_servers", "mcpServers"),
    )
    capability_profiles: UserCapabilityProfileCollection = Field(
        default_factory=UserCapabilityProfileCollection,
        validation_alias=AliasChoices("capability_profiles", "capabilityProfiles"),
    )
    agents: UserAgentCollection = Field(default_factory=UserAgentCollection)

    @model_validator(mode="after")
    def validate_catalog(self) -> UserExecutionConfig:
        collections: tuple[tuple[str, list[Any]], ...] = (
            ("skill", self.skills.items),
            ("MCP server", self.mcp_servers.items),
            ("capability profile", self.capability_profiles.items),
            ("agent", self.agents.items),
        )
        for label, items in collections:
            _validate_unique_resources(label, items)

        personal_skills = {item.id for item in self.skills.items}
        personal_servers = {item.id for item in self.mcp_servers.items}
        personal_profiles = {item.id for item in self.capability_profiles.items}
        personal_agents = {item.id for item in self.agents.items}

        for profile in self.capability_profiles.items:
            _validate_personal_refs_exist(
                profile.mcp_server_refs,
                personal_servers,
                f"capability profile '{profile.id}'",
            )
        for agent in self.agents.items:
            _validate_personal_refs_exist(
                agent.skill_refs,
                personal_skills,
                f"agent '{agent.id}'",
            )
            if agent.capability_profile_ref is not None:
                _validate_personal_refs_exist(
                    [agent.capability_profile_ref],
                    personal_profiles,
                    f"agent '{agent.id}'",
                )

        if self.defaults.agent_ref is not None:
            _validate_personal_refs_exist([self.defaults.agent_ref], personal_agents, "defaults")
        if self.defaults.skill_refs is not None:
            _validate_personal_refs_exist(self.defaults.skill_refs, personal_skills, "defaults")
        if self.defaults.capability_profile_ref is not None:
            _validate_personal_refs_exist(
                [self.defaults.capability_profile_ref], personal_profiles, "defaults"
            )
        return self


@dataclass(frozen=True)
class UserExecutionConfigValidationReport:
    valid: bool
    config: UserExecutionConfig | None
    errors: list[str]


def validate_user_execution_config(payload: object) -> UserExecutionConfigValidationReport:
    try:
        config = UserExecutionConfig.model_validate(payload)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            prefix = f"{location}: " if location else ""
            errors.append(prefix + str(error["msg"]))
        return UserExecutionConfigValidationReport(valid=False, config=None, errors=errors)
    return UserExecutionConfigValidationReport(valid=True, config=config, errors=[])


@dataclass(frozen=True)
class EffectiveSkill:
    id: str
    display_name: str
    description: str | None
    source: Literal["shared", "user"]
    version: int | None
    config: TenantSkillConfig
    stored_ref: str


@dataclass(frozen=True)
class EffectiveCapabilityProfile:
    id: str
    display_name: str
    description: str | None
    source: Literal["shared", "user"]
    version: int | None
    config: TenantCapabilityProfileConfig | UserCapabilityProfileDefinition
    stored_ref: str


@dataclass(frozen=True)
class EffectiveAgent:
    id: str
    display_name: str
    description: str | None
    source: Literal["shared", "user"]
    version: int | None
    skill_refs: list[str]
    capability_profile_ref: str | None
    uses_skill_list: bool = True


class UserExecutionConfigSource(Protocol):
    def get_user_execution_config(self, tenant_id: str, user_id: str) -> Any | None: ...


class UserExecutionResolutionError(RuntimeError):
    pass


class UserExecutionUnsupportedError(UserExecutionResolutionError):
    pass


@dataclass(frozen=True)
class PersonalCapabilityConstraints:
    allowed_local_tools: list[str] | None
    shared_mcp_server_names: set[str]
    personal_mcp_servers: list[MCPServerConfig]


@dataclass(frozen=True)
class EffectiveExecutionCatalog:
    tenant_config: TenantExecutionConfig
    user_config: UserExecutionConfig | None = None
    user_config_version: int | None = None
    personal_mcp_servers_allowed: bool = False
    credential_source: Any | None = None
    credential_tenant_id: str | None = None
    credential_user_id: str | None = None

    @property
    def default_agent_ref(self) -> str | None:
        if self.user_config is not None and self.user_config.defaults.agent_ref is not None:
            return self.user_config.defaults.agent_ref
        return self.tenant_config.agents.default_agent

    @property
    def default_skill_refs(self) -> list[str] | None:
        if self.user_config is not None and self.user_config.defaults.skill_refs is not None:
            return list(self.user_config.defaults.skill_refs)
        if self.tenant_config.skills.default_skill is None:
            return None
        return [self.tenant_config.skills.default_skill]

    @property
    def default_capability_profile_ref(self) -> str | None:
        if (
            self.user_config is not None
            and self.user_config.defaults.capability_profile_ref is not None
        ):
            return self.user_config.defaults.capability_profile_ref
        return self.tenant_config.capability_profiles.default_profile

    def skill_options(self) -> list[EffectiveSkill]:
        options = [
            EffectiveSkill(
                id=f"shared:{skill.name}",
                display_name=skill.name,
                description=skill.description,
                source="shared",
                version=None,
                config=skill,
                stored_ref=skill.name,
            )
            for skill in self.tenant_config.skills.items
        ]
        if self.user_config is not None:
            options.extend(
                EffectiveSkill(
                    id=skill.id,
                    display_name=skill.name,
                    description=skill.description,
                    source="user",
                    version=self.user_config_version,
                    config=TenantSkillConfig(
                        name=skill.id,
                        system_prompt=skill.system_prompt,
                        description=skill.description,
                        workspace_scope=skill.workspace_scope,
                    ),
                    stored_ref=skill.id,
                )
                for skill in self.user_config.skills.items
            )
        return options

    def capability_profile_options(self) -> list[EffectiveCapabilityProfile]:
        options = [
            EffectiveCapabilityProfile(
                id=f"shared:{profile.name}",
                display_name=profile.name,
                description=profile.description,
                source="shared",
                version=None,
                config=profile,
                stored_ref=profile.name,
            )
            for profile in self.tenant_config.capability_profiles.items
        ]
        if self.user_config is not None:
            options.extend(
                EffectiveCapabilityProfile(
                    id=profile.id,
                    display_name=profile.name,
                    description=profile.description,
                    source="user",
                    version=self.user_config_version,
                    config=profile,
                    stored_ref=profile.id,
                )
                for profile in self.user_config.capability_profiles.items
            )
        return options

    def agent_options(self) -> list[EffectiveAgent]:
        options = [self._shared_agent(agent) for agent in self.tenant_config.agents.items]
        if self.user_config is not None:
            options.extend(
                EffectiveAgent(
                    id=agent.id,
                    display_name=agent.name,
                    description=agent.description,
                    source="user",
                    version=self.user_config_version,
                    skill_refs=list(agent.skill_refs),
                    capability_profile_ref=agent.capability_profile_ref,
                )
                for agent in self.user_config.agents.items
            )
        return options

    def resolve_skill_refs(
        self,
        refs: list[str] | None,
        *,
        use_defaults: bool = True,
    ) -> list[EffectiveSkill]:
        resolved_refs = self.default_skill_refs if refs is None and use_defaults else refs
        if resolved_refs is None:
            return []
        options = self.skill_options()
        by_ref = {option.id: option for option in options}
        by_ref.update(
            {option.display_name: option for option in options if option.source == "shared"}
        )
        resolved: list[EffectiveSkill] = []
        for ref in resolved_refs:
            option = by_ref.get(ref)
            if option is None:
                raise UserExecutionResolutionError(
                    f"Unknown skill '{ref}' for tenant '{self.tenant_config.tenant_id}'"
                )
            resolved.append(option)
        return resolved

    def resolve_agent(
        self,
        ref: str | None,
        *,
        use_default: bool = True,
    ) -> EffectiveAgent | None:
        resolved_ref = self.default_agent_ref if ref is None and use_default else ref
        if resolved_ref is None:
            return None
        options = self.agent_options()
        by_ref = {option.id: option for option in options}
        by_ref.update(
            {option.display_name: option for option in options if option.source == "shared"}
        )
        option = by_ref.get(resolved_ref)
        if option is None:
            raise UserExecutionResolutionError(
                f"Unknown agent preset '{resolved_ref}' for tenant '{self.tenant_config.tenant_id}'"
            )
        return option

    def resolve_capability_profile(
        self,
        ref: str | None,
        *,
        use_default: bool = True,
    ) -> EffectiveCapabilityProfile | None:
        resolved_ref = self.default_capability_profile_ref if ref is None and use_default else ref
        if resolved_ref is None:
            return None
        options = self.capability_profile_options()
        by_ref = {option.id: option for option in options}
        by_ref.update(
            {option.display_name: option for option in options if option.source == "shared"}
        )
        option = by_ref.get(resolved_ref)
        if option is None:
            raise UserExecutionResolutionError(
                f"Unknown capability profile '{resolved_ref}' for tenant "
                f"'{self.tenant_config.tenant_id}'"
            )
        return option

    def personal_capability_constraints(
        self,
        capability: EffectiveCapabilityProfile,
    ) -> PersonalCapabilityConstraints:
        if capability.source != "user" or not isinstance(
            capability.config, UserCapabilityProfileDefinition
        ):
            raise ValueError("personal capability constraints require a user capability profile")
        profile = capability.config
        available_local_tools = set(
            self.tenant_config.tools.allowed_local_tools
            if self.tenant_config.tools.allowed_local_tools is not None
            else DEFAULT_LOCAL_TOOL_NAMES
        )
        if profile.allowed_local_tools is not None:
            unavailable_tools = sorted(set(profile.allowed_local_tools) - available_local_tools)
            if unavailable_tools:
                raise UserExecutionResolutionError(
                    f"Personal capability profile '{capability.id}' references tenant-unavailable "
                    "local tools: " + ", ".join(unavailable_tools)
                )

        tenant_mcp_server_names = {server.name for server in self.tenant_config.tools.mcp_servers}
        shared_mcp_server_names: set[str] = set()
        personal_mcp_servers: list[MCPServerConfig] = []
        user_servers = (
            {server.id: server for server in self.user_config.mcp_servers.items}
            if self.user_config is not None
            else {}
        )
        for ref in profile.mcp_server_refs:
            if ref.startswith("user:"):
                if not self.personal_mcp_servers_allowed:
                    raise UserExecutionUnsupportedError(
                        "Tenant policy does not allow user-owned MCP servers"
                    )
                server = user_servers.get(ref)
                if server is None:
                    raise UserExecutionResolutionError(f"Unknown personal MCP server '{ref}'")
                credential_headers: dict[str, str] = {}
                if server.credential_ref is not None:
                    credential_headers = self._personal_credential_headers(
                        server.credential_ref,
                        server_id=server.id,
                        static_headers=server.headers,
                    )
                personal_mcp_servers.append(
                    _personal_mcp_server_config(server, credential_headers=credential_headers)
                )
                continue
            server_name = ref.removeprefix("shared:")
            if server_name not in tenant_mcp_server_names:
                raise UserExecutionResolutionError(
                    f"Unknown shared MCP server '{ref}' for tenant '{self.tenant_config.tenant_id}'"
                )
            shared_mcp_server_names.add(server_name)
        return PersonalCapabilityConstraints(
            allowed_local_tools=(
                list(profile.allowed_local_tools)
                if profile.allowed_local_tools is not None
                else None
            ),
            shared_mcp_server_names=shared_mcp_server_names,
            personal_mcp_servers=personal_mcp_servers,
        )

    def _personal_credential_headers(
        self,
        credential_ref: str,
        *,
        server_id: str,
        static_headers: dict[str, str],
    ) -> dict[str, str]:
        getter = getattr(self.credential_source, "get_user_execution_credential", None)
        if (
            not callable(getter)
            or self.credential_tenant_id is None
            or self.credential_user_id is None
        ):
            raise UserExecutionUnsupportedError(
                "Encrypted personal credential storage is not configured"
            )
        credential = getter(
            self.credential_tenant_id,
            self.credential_user_id,
            credential_ref,
        )
        if credential is None:
            raise UserExecutionResolutionError(
                f"Personal MCP server '{server_id}' credential '{credential_ref}' was not found"
            )
        header_name = str(getattr(credential, "header_name", ""))
        header_value = str(getattr(credential, "header_value", ""))
        _validate_runtime_credential_header(header_name, header_value)
        if header_name.lower() in {name.lower() for name in static_headers}:
            raise UserExecutionResolutionError(
                f"Personal MCP server '{server_id}' credential header conflicts with a static header"
            )
        return {header_name: header_value}

    def _shared_agent(self, agent: TenantAgentPresetConfig) -> EffectiveAgent:
        skill_refs: list[str] = []
        if agent.skill_name is not None:
            skill_refs = [agent.skill_name]
        elif agent.skills is not None:
            skill_refs = list(agent.skills)
        return EffectiveAgent(
            id=f"shared:{agent.name}",
            display_name=agent.name,
            description=agent.description,
            source="shared",
            version=None,
            skill_refs=skill_refs,
            capability_profile_ref=agent.capability_profile,
            uses_skill_list=agent.skills is not None,
        )


def effective_execution_catalog(
    tenant_config: TenantExecutionConfig,
    source: UserExecutionConfigSource | None,
    *,
    tenant_id: str,
    user_id: str,
) -> EffectiveExecutionCatalog:
    if source is None:
        return EffectiveExecutionCatalog(tenant_config=tenant_config)
    record = source.get_user_execution_config(tenant_id, user_id)
    if record is None:
        return EffectiveExecutionCatalog(tenant_config=tenant_config)
    report = validate_user_execution_config(record.config)
    if not report.valid or report.config is None:
        raise RuntimeError(
            f"Stored user execution config for '{tenant_id}/{user_id}' is invalid: "
            + "; ".join(report.errors)
        )
    allow_personal_mcp_servers = False
    policy_getter = getattr(source, "get_tenant_mcp_server_catalog_policy", None)
    if callable(policy_getter):
        policy = policy_getter(tenant_id)
        allow_personal_mcp_servers = policy is None or bool(
            getattr(policy, "allow_custom_mcp_servers", False)
        )
    return EffectiveExecutionCatalog(
        tenant_config=tenant_config,
        user_config=report.config,
        user_config_version=record.version,
        personal_mcp_servers_allowed=allow_personal_mcp_servers,
        credential_source=source,
        credential_tenant_id=tenant_id,
        credential_user_id=user_id,
    )


def _personal_mcp_server_config(
    server: UserMCPServerDefinition,
    *,
    credential_headers: dict[str, str] | None = None,
) -> MCPServerConfig:
    try:
        validate_public_https_url(server.url)
    except ValueError as exc:
        raise UserExecutionUnsupportedError(f"User-owned MCP server URL {exc}") from exc
    if server.allowed_tools is not None and not set(server.trusted_input_preprocessor_tools) <= set(
        server.allowed_tools
    ):
        raise UserExecutionResolutionError(
            f"Personal MCP server '{server.id}' trusted input preprocessors must be allowed tools"
        )
    try:
        result_redaction = parse_tool_result_redaction_policy(
            server.result_redaction,
            context=f"Personal MCP server '{server.id}'",
        )
        private_value_policy = parse_mcp_private_value_policy(
            server.private_value_policy,
            context=f"Personal MCP server '{server.id}'",
        )
        private_value_tool_policies = parse_mcp_private_value_tool_policies(
            server.private_value_tool_policies,
            context=f"Personal MCP server '{server.id}'",
        )
    except (RuntimeError, ValueError) as exc:
        raise UserExecutionResolutionError(str(exc)) from exc
    headers = dict(server.headers)
    if credential_headers:
        headers.update(credential_headers)
    return MCPServerConfig(
        name=server.id,
        url=server.url,
        headers=headers,
        protocol_version=server.protocol_version or DEFAULT_MCP_PROTOCOL_VERSION,
        allowed_tools=(list(server.allowed_tools) if server.allowed_tools is not None else None),
        result_redaction_policy=result_redaction,
        private_value_policy=private_value_policy,
        private_value_tool_policies=private_value_tool_policies,
        trusted_input_preprocessor_tools=frozenset(server.trusted_input_preprocessor_tools),
        timeout_seconds=server.timeout_seconds,
        public_network_only=True,
    )


def _validate_runtime_credential_header(header_name: str, header_value: str) -> None:
    if (
        not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header_name)
        or header_name.lower() in _FORBIDDEN_CREDENTIAL_HEADER_NAMES
        or not header_value
        or "\r" in header_value
        or "\n" in header_value
    ):
        raise UserExecutionResolutionError("Stored personal MCP credential header is invalid")


def has_personal_execution_refs(
    skill_names: list[str] | None,
    capability_profile: str | None,
) -> bool:
    return any(ref.startswith("user:") for ref in skill_names or []) or (
        capability_profile is not None and capability_profile.startswith("user:")
    )


def _validate_user_resource_id(value: str) -> str:
    if not _USER_RESOURCE_ID_PATTERN.fullmatch(value):
        raise ValueError("id must use the 'user:' namespace followed by a lowercase resource name")
    return value


def _validate_resource_ref(value: str) -> str:
    if not _RESOURCE_REF_PATTERN.fullmatch(value):
        raise ValueError("resource reference must use a valid 'user:' or 'shared:' qualified ID")
    return value


def _validate_resource_refs(values: list[str], field_name: str) -> list[str]:
    for value in values:
        _validate_resource_ref(value)
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


def _validate_unique_resources(label: str, items: list[Any]) -> None:
    ids = [item.id for item in items]
    duplicate_ids = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicate_ids:
        raise ValueError(f"duplicate {label} ids: {', '.join(duplicate_ids)}")
    names = [item.name for item in items]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise ValueError(f"duplicate {label} names: {', '.join(duplicate_names)}")


def _validate_personal_refs_exist(refs: list[str], known: set[str], context: str) -> None:
    missing = sorted(ref for ref in refs if ref.startswith("user:") and ref not in known)
    if missing:
        raise ValueError(f"{context} references unknown personal resources: {', '.join(missing)}")
