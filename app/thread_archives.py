from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.message_parts import is_attachment_part
from app.models import Message, MessagePart, MessageRole, Thread, ThreadContext, utc_now

THREAD_ARCHIVE_SCHEMA = "mindweft.thread-archive"
THREAD_ARCHIVE_VERSION = 1
MAX_ARCHIVE_MESSAGES = 10_000


class ThreadArchiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThreadArchiveThread(ThreadArchiveModel):
    source_thread_id: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=120)
    title_source: Literal["generated", "semantic", "manual"] | None = None
    skill_name: str | None = None
    skill_names: list[str] | None = None
    capability_profile: str | None = None
    llm_profile: str | None = None
    created_at: datetime
    updated_at: datetime


class ThreadArchiveContext(ThreadArchiveModel):
    summary: str = ""
    summarized_message_count: int = Field(default=0, ge=0)


class ThreadArchiveMessage(ThreadArchiveModel):
    source_message_id: str = Field(min_length=1)
    upstream_source_message_id: str | None = None
    role: MessageRole
    content: str
    parts: list[MessagePart] | None = None
    metadata: dict[str, Any] | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_arguments: dict[str, Any] | None = None
    created_at: datetime


class ThreadArchiveV1(ThreadArchiveModel):
    schema_name: Literal["mindweft.thread-archive"] = Field(
        default=THREAD_ARCHIVE_SCHEMA,
        validation_alias="schema",
        serialization_alias="schema",
    )
    version: Literal[1] = THREAD_ARCHIVE_VERSION
    archive_id: str = Field(default_factory=lambda: str(uuid4()))
    exported_at: datetime = Field(default_factory=utc_now)
    thread: ThreadArchiveThread
    context: ThreadArchiveContext
    messages: list[ThreadArchiveMessage] = Field(max_length=MAX_ARCHIVE_MESSAGES)


class ThreadArchiveImportWarning(ThreadArchiveModel):
    code: str
    message: str


class ThreadArchiveImportResponse(ThreadArchiveModel):
    thread_id: str
    source_thread_id: str
    message_count: int
    warnings: list[ThreadArchiveImportWarning] = Field(default_factory=list)


def build_thread_archive(
    thread: Thread,
    messages: list[Message],
    context: ThreadContext,
) -> ThreadArchiveV1:
    _reject_unsupported_messages(messages)
    return ThreadArchiveV1(
        thread=ThreadArchiveThread(
            source_thread_id=thread.thread_id,
            title=thread.title,
            title_source=thread.title_source,
            skill_name=thread.skill_name,
            skill_names=thread.skill_names,
            capability_profile=thread.capability_profile,
            llm_profile=thread.llm_profile,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
        ),
        context=ThreadArchiveContext(
            summary=context.summary,
            summarized_message_count=context.summarized_message_count,
        ),
        messages=[
            ThreadArchiveMessage(
                source_message_id=message.id,
                upstream_source_message_id=message.source_message_id,
                role=message.role,
                content=message.content,
                parts=message.parts,
                metadata=message.metadata,
                tool_name=message.tool_name,
                tool_call_id=message.tool_call_id,
                tool_arguments=message.tool_arguments,
                created_at=message.created_at,
            )
            for message in messages
        ],
    )


def validate_importable_thread_archive(archive: ThreadArchiveV1) -> None:
    if archive.thread.title is not None and not archive.thread.title.strip():
        raise ValueError("archive thread title must not be blank")
    if archive.context.summarized_message_count > len(archive.messages):
        raise ValueError("archive summarized_message_count exceeds message count")
    for message in archive.messages:
        if message.role == MessageRole.SYSTEM:
            raise ValueError("thread archives cannot import system messages")
        if any(is_attachment_part(part) for part in message.parts or []):
            raise ValueError("thread archives with attachment parts are not supported yet")


def imported_messages(
    archive: ThreadArchiveV1,
    *,
    thread_id: str,
    importing_user_id: str,
) -> list[Message]:
    validate_importable_thread_archive(archive)
    return [
        Message(
            thread_id=thread_id,
            source_message_id=(message.upstream_source_message_id or message.source_message_id),
            role=message.role,
            content=message.content,
            parts=message.parts,
            created_by=importing_user_id if message.role == MessageRole.USER else None,
            metadata=message.metadata,
            tool_name=message.tool_name,
            tool_call_id=message.tool_call_id,
            tool_arguments=message.tool_arguments,
            created_at=message.created_at,
        )
        for message in archive.messages
    ]


def _reject_unsupported_messages(messages: list[Message]) -> None:
    for message in messages:
        if message.role == MessageRole.SYSTEM:
            raise ValueError("thread archives with system messages are not supported")
        if any(is_attachment_part(part) for part in message.parts or []):
            raise ValueError("thread archives with attachment parts are not supported yet")
