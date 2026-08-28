from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ThreadStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"


class TenantStatus(str, Enum):
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"
    DELETED = "deleted"


class Tenant(BaseModel):
    id: str
    slug: str
    name: str
    status: TenantStatus = TenantStatus.PROVISIONING
    plan: str | None = None
    region: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TenantUserRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class TenantUserStatus(str, Enum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class TenantUser(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    email: str | None = None
    display_name: str | None = None
    role: TenantUserRole = TenantUserRole.MEMBER
    status: TenantUserStatus = TenantUserStatus.INVITED
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TenantEntitlements(BaseModel):
    tenant_id: str
    features: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    version: int = 1
    updated_at: datetime = Field(default_factory=utc_now)


class TenantDomain(BaseModel):
    id: str
    tenant_id: str
    domain: str
    verified: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class TenantContext(BaseModel):
    principal: "Principal"
    tenant_id: str
    slug: str | None = None
    status: TenantStatus | None = None
    plan: str | None = None
    region: str | None = None
    features: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    execution_config_version: int | None = None
    entitlements_version: int | None = None
    membership_id: str | None = None
    membership_email: str | None = None
    membership_display_name: str | None = None
    user_role: TenantUserRole | None = None
    user_status: TenantUserStatus | None = None
    membership_metadata: dict[str, Any] = Field(default_factory=dict)


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class AttachmentPartBase(BaseModel):
    mime_type: str
    data: str | None = None
    url: str | None = None
    attachment_id: str | None = None


class AudioPart(AttachmentPartBase):
    type: Literal["audio"] = "audio"
    filename: str = Field(min_length=1, max_length=255)


class ImagePart(AttachmentPartBase):
    type: Literal["image"] = "image"
    detail: Literal["auto", "low", "high"] = "auto"


class DocumentPart(AttachmentPartBase):
    type: Literal["document"] = "document"
    filename: str = Field(min_length=1, max_length=255)


MessagePart = Annotated[
    TextPart | AudioPart | ImagePart | DocumentPart, Field(discriminator="type")
]


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    thread_id: str
    source_message_id: str | None = None
    role: MessageRole
    content: str
    parts: list[MessagePart] | None = None
    created_by: str | None = None
    metadata: dict[str, Any] | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_arguments: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ThreadImportProvenance(BaseModel):
    archive_id: str
    source_thread_id: str
    imported_at: datetime


class Thread(BaseModel):
    thread_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    execution_user_id: str | None = None
    title: str | None = None
    title_source: Literal["generated", "semantic", "manual"] | None = None
    title_updated_at: datetime | None = None
    pinned_at: datetime | None = None
    archived_at: datetime | None = None
    skill_name: str | None = None
    skill_names: list[str] | None = None
    capability_profile: str | None = None
    llm_profile: str | None = None
    parent_thread_id: str | None = None
    fork_message_id: str | None = None
    compacted_through_message_id: str | None = None
    import_source_archive_id: str | None = None
    import_source_thread_id: str | None = None
    imported_at: datetime | None = None
    import_provenance_chain: list[ThreadImportProvenance] = Field(default_factory=list)
    status: ThreadStatus = ThreadStatus.IDLE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ThreadContext(BaseModel):
    thread_id: str
    summary: str = ""
    summarized_message_count: int = 0
    updated_at: datetime = Field(default_factory=utc_now)


class AuditRecord(BaseModel):
    audit_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    actor_user_id: str
    action: str
    affected_count: int
    thread_ids: list[str] = Field(default_factory=list)
    resource_type: str | None = None
    resource_id: str | None = None
    old_values: dict[str, Any] | None = None
    new_values: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Principal(BaseModel):
    user_id: str
    tenant_id: str
    is_admin: bool = False


class CreateThreadResponse(BaseModel):
    thread_id: str


class ForkThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at_message_id: str = Field(min_length=1)


class ForkThreadResponse(BaseModel):
    thread_id: str
    parent_thread_id: str
    fork_message_id: str


class ThreadListItem(BaseModel):
    thread_id: str
    title: str
    title_source: Literal["generated", "semantic", "manual"] | None = None
    title_updated_at: datetime | None = None
    pinned_at: datetime | None = None
    archived_at: datetime | None = None
    status: ThreadStatus
    skill_name: str | None = None
    skill_names: list[str] | None = None
    capability_profile: str | None = None
    llm_profile: str | None = None
    parent_thread_id: str | None = None
    fork_message_id: str | None = None
    compacted_through_message_id: str | None = None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class ThreadLineageResponse(BaseModel):
    thread: ThreadListItem
    parent: ThreadListItem | None = None
    children: list[ThreadListItem] = Field(default_factory=list)
    siblings: list[ThreadListItem] = Field(default_factory=list)
    import_provenance: ThreadImportProvenance | None = None
    import_provenance_chain: list[ThreadImportProvenance] = Field(default_factory=list)


class ImportedLineageDeleteResponse(BaseModel):
    deleted_thread_ids: list[str]
    deleted_count: int


class ThreadListResponse(BaseModel):
    threads: list[ThreadListItem]
    total: int
    limit: int
    offset: int


class ThreadSearchMatch(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    snippet: str
    created_at: datetime


class ThreadSearchResult(BaseModel):
    thread: ThreadListItem
    match_count: int
    matches: list[ThreadSearchMatch] = Field(default_factory=list)


class ThreadSearchResponse(BaseModel):
    results: list[ThreadSearchResult]
    total: int
    limit: int
    offset: int


class CreateThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_name: str | None = None
    skill_name: str | None = None
    skill_names: list[str] | None = None
    capability_profile: str | None = None
    llm_profile: str | None = None


class UpdateThreadTitleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)


class UpdateThreadOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool | None = None
    archived: bool | None = None


class GenerateThreadTitleResponse(BaseModel):
    thread_id: str
    status: Literal["updated", "skipped", "failed"]
    title: str | None = None
    reason: str | None = None


class ThreadTitleResponse(BaseModel):
    thread_id: str
    title: str
    title_source: Literal["generated", "manual"]
    title_updated_at: datetime


class ExecutionOptionItem(BaseModel):
    name: str
    description: str | None = None
    id: str | None = None
    display_name: str | None = None
    source: str | None = None
    version: int | None = None


class ExecutionOptionSection(BaseModel):
    default: str | None = None
    defaults: list[str] | None = None
    items: list[ExecutionOptionItem] = Field(default_factory=list)


class ExecutionLLMOptionItem(ExecutionOptionItem):
    input_modalities: list[str] | None = None
    audio_input_allowed: bool = False
    audio_input_reason: Literal["disabled", "backend_unsupported", "profile_unsupported"] | None = (
        None
    )
    image_input_allowed: bool
    document_input_allowed: bool = False
    document_input_reason: (
        Literal["disabled", "backend_unsupported", "profile_unsupported"] | None
    ) = None
    image_input_reason: Literal["disabled", "backend_unsupported", "profile_unsupported"] | None = (
        None
    )
    capability_declared: bool = False


class ExecutionLLMOptionSection(BaseModel):
    default: str | None = None
    effective_default: ExecutionLLMOptionItem
    items: list[ExecutionLLMOptionItem] = Field(default_factory=list)


class ExecutionAgentOptionItem(BaseModel):
    name: str
    description: str | None = None
    id: str | None = None
    display_name: str | None = None
    source: str | None = None
    version: int | None = None
    skill_name: str | None = None
    skills: list[str] | None = None
    capability_profile: str | None = None
    llm_profile: str | None = Field(default=None, exclude_if=lambda value: value is None)


class ExecutionAgentOptionSection(BaseModel):
    default: str | None = None
    items: list[ExecutionAgentOptionItem] = Field(default_factory=list)


class ExecutionOptionsResponse(BaseModel):
    tenant_id: str
    skills: ExecutionOptionSection
    capability_profiles: ExecutionOptionSection
    llm_profiles: ExecutionLLMOptionSection
    agents: ExecutionAgentOptionSection = Field(default_factory=ExecutionAgentOptionSection)


class AddMessageRequest(BaseModel):
    content: str = ""
    parts: list[MessagePart] | None = None
    metadata: dict[str, Any] | None = None


class PrivateValueConsentDecisionRequest(BaseModel):
    approve: bool
    one_shot: bool = True


class RunThreadResponse(BaseModel):
    reply: str


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] | None = None


class LLMResponse(BaseModel):
    content: str | None = None
    # Backward-compatible single-call view. New code should prefer tool_calls.
    tool_call: ToolCall | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int] | None = None
    metadata: dict[str, Any] | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.tool_call is not None and not self.tool_calls:
            self.tool_calls = [self.tool_call]
        elif self.tool_call is None and self.tool_calls:
            self.tool_call = self.tool_calls[0]
