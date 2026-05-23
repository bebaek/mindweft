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


class LLMResponse(BaseModel):
    content: str | None = None
    tool_call: ToolCall | None = None
    usage: dict[str, int] | None = None
