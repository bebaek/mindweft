from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
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


class TenantEntitlements(BaseModel):
    tenant_id: str
    features: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    version: int = 1
    updated_at: datetime = Field(default_factory=utc_now)


class TenantContext(BaseModel):
    principal: "Principal"
    tenant_id: str
    slug: str | None = None
    status: TenantStatus | None = None
    plan: str | None = None
    region: str | None = None
    features: dict[str, bool] = Field(default_factory=dict)
    limits: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    entitlements_version: int | None = None


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    thread_id: str
    role: MessageRole
    content: str
    created_by: str | None = None
    metadata: dict[str, Any] | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_arguments: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Thread(BaseModel):
    thread_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    skill_name: str | None = None
    skill_names: list[str] | None = None
    capability_profile: str | None = None
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


class CreateThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_name: str | None = None
    skill_names: list[str] | None = None
    capability_profile: str | None = None


class AddMessageRequest(BaseModel):
    content: str
    metadata: dict[str, Any] | None = None


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
