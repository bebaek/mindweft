from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.attachments import AttachmentRecord
from app.message_parts import is_attachment_part, remap_attachment_ids
from app.models import Message, MessagePart, MessageRole, Thread, ThreadContext, utc_now

THREAD_ARCHIVE_SCHEMA = "mindweft.thread-archive"
THREAD_ARCHIVE_VERSION = 2
ThreadArchiveProfilePolicy = Literal["defaults", "available", "strict"]
MAX_ARCHIVE_MESSAGES = 10_000
MAX_ARCHIVE_ATTACHMENTS = 1_000


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


class ThreadArchiveAttachment(ThreadArchiveModel):
    source_attachment_id: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    encoding: Literal["base64"] = "base64"
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data: str


class ThreadArchiveBase(ThreadArchiveModel):
    schema_name: Literal["mindweft.thread-archive"] = Field(
        default=THREAD_ARCHIVE_SCHEMA,
        validation_alias="schema",
        serialization_alias="schema",
    )
    archive_id: str = Field(default_factory=lambda: str(uuid4()))
    exported_at: datetime = Field(default_factory=utc_now)
    thread: ThreadArchiveThread
    context: ThreadArchiveContext
    messages: list[ThreadArchiveMessage] = Field(max_length=MAX_ARCHIVE_MESSAGES)


class ThreadArchiveV1(ThreadArchiveBase):
    version: Literal[1] = 1


class ThreadArchiveV2(ThreadArchiveBase):
    version: Literal[2] = THREAD_ARCHIVE_VERSION
    attachments: list[ThreadArchiveAttachment] = Field(
        default_factory=list,
        max_length=MAX_ARCHIVE_ATTACHMENTS,
    )


ThreadArchive = Annotated[ThreadArchiveV1 | ThreadArchiveV2, Field(discriminator="version")]


class ThreadArchiveImportWarning(ThreadArchiveModel):
    code: str
    message: str


class ThreadArchiveImportResponse(ThreadArchiveModel):
    thread_id: str
    source_thread_id: str
    message_count: int
    attachment_count: int
    profile_policy: ThreadArchiveProfilePolicy
    warnings: list[ThreadArchiveImportWarning] = Field(default_factory=list)


def build_thread_archive(
    thread: Thread,
    messages: list[Message],
    context: ThreadContext,
    attachment_records: Mapping[str, AttachmentRecord] | None = None,
) -> ThreadArchiveV2:
    _reject_system_messages(messages)
    records = dict(attachment_records or {})
    referenced_ids, referenced_mime_types = _message_attachment_references(messages)
    if set(records) != referenced_ids:
        missing = sorted(referenced_ids - set(records))
        extra = sorted(set(records) - referenced_ids)
        if missing:
            raise ValueError("archive attachment data is missing for: " + ", ".join(missing))
        raise ValueError("archive attachment data is unreferenced for: " + ", ".join(extra))
    attachments: list[ThreadArchiveAttachment] = []
    for attachment_id in sorted(referenced_ids):
        record = records[attachment_id]
        if record.metadata.attachment_id != attachment_id:
            raise ValueError(f"archive attachment record ID mismatch for '{attachment_id}'")
        if record.metadata.mime_type != referenced_mime_types[attachment_id]:
            raise ValueError(f"archive attachment MIME type mismatch for '{attachment_id}'")
        attachments.append(
            ThreadArchiveAttachment(
                source_attachment_id=attachment_id,
                mime_type=record.metadata.mime_type,
                size_bytes=len(record.data),
                sha256=hashlib.sha256(record.data).hexdigest(),
                data=base64.b64encode(record.data).decode("ascii"),
            )
        )
    return ThreadArchiveV2(
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
        attachments=attachments,
    )


def validate_importable_thread_archive(archive: ThreadArchive) -> dict[str, bytes]:
    if archive.thread.title is not None and not archive.thread.title.strip():
        raise ValueError("archive thread title must not be blank")
    if archive.context.summarized_message_count > len(archive.messages):
        raise ValueError("archive summarized_message_count exceeds message count")
    messages = [_archive_message_as_message(message) for message in archive.messages]
    _reject_system_messages(messages, importing=True)
    referenced_ids, referenced_mime_types = _message_attachment_references(messages)
    attachment_data = decode_archive_attachments(archive)
    if set(attachment_data) != referenced_ids:
        missing = sorted(referenced_ids - set(attachment_data))
        extra = sorted(set(attachment_data) - referenced_ids)
        if missing:
            raise ValueError("archive attachment data is missing for: " + ", ".join(missing))
        raise ValueError("archive attachment data is unreferenced for: " + ", ".join(extra))
    archive_attachments = _archive_attachments(archive)
    attachments_by_id = {
        attachment.source_attachment_id: attachment for attachment in archive_attachments
    }
    for attachment_id, expected_mime_type in referenced_mime_types.items():
        if attachments_by_id[attachment_id].mime_type != expected_mime_type:
            raise ValueError(f"archive attachment MIME type mismatch for '{attachment_id}'")
    return attachment_data


def decode_archive_attachments(archive: ThreadArchive) -> dict[str, bytes]:
    decoded: dict[str, bytes] = {}
    for attachment in _archive_attachments(archive):
        if attachment.source_attachment_id in decoded:
            raise ValueError(f"duplicate archive attachment ID '{attachment.source_attachment_id}'")
        try:
            data = base64.b64decode(attachment.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"archive attachment '{attachment.source_attachment_id}' data is not valid base64"
            ) from exc
        if len(data) != attachment.size_bytes:
            raise ValueError(
                f"archive attachment '{attachment.source_attachment_id}' size does not match"
            )
        if hashlib.sha256(data).hexdigest() != attachment.sha256:
            raise ValueError(
                f"archive attachment '{attachment.source_attachment_id}' checksum does not match"
            )
        decoded[attachment.source_attachment_id] = data
    return decoded


def imported_messages(
    archive: ThreadArchive,
    *,
    thread_id: str,
    importing_user_id: str,
    attachment_id_map: Mapping[str, str] | None = None,
    validated: bool = False,
) -> list[Message]:
    if not validated:
        validate_importable_thread_archive(archive)
    attachment_id_map = attachment_id_map or {}
    return [
        Message(
            thread_id=thread_id,
            source_message_id=(message.upstream_source_message_id or message.source_message_id),
            role=message.role,
            content=message.content,
            parts=remap_attachment_ids(message.parts, attachment_id_map),
            created_by=importing_user_id if message.role == MessageRole.USER else None,
            metadata=message.metadata,
            tool_name=message.tool_name,
            tool_call_id=message.tool_call_id,
            tool_arguments=message.tool_arguments,
            created_at=message.created_at,
        )
        for message in archive.messages
    ]


def _archive_attachments(archive: ThreadArchive) -> list[ThreadArchiveAttachment]:
    return archive.attachments if isinstance(archive, ThreadArchiveV2) else []


def _archive_message_as_message(message: ThreadArchiveMessage) -> Message:
    return Message(
        thread_id="archive-validation",
        role=message.role,
        content=message.content,
        parts=message.parts,
    )


def _reject_system_messages(messages: list[Message], *, importing: bool = False) -> None:
    for message in messages:
        if message.role == MessageRole.SYSTEM:
            action = "cannot import" if importing else "with"
            raise ValueError(f"thread archives {action} system messages")


def _message_attachment_references(messages: list[Message]) -> tuple[set[str], dict[str, str]]:
    attachment_ids: set[str] = set()
    mime_types: dict[str, str] = {}
    for message in messages:
        for part in message.parts or []:
            if not is_attachment_part(part):
                continue
            if part.attachment_id is None:
                raise ValueError("archive attachment parts require attachment_id")
            if part.data is not None or part.url is not None:
                raise ValueError("archive attachment parts must reference manifest data only")
            existing_mime_type = mime_types.get(part.attachment_id)
            if existing_mime_type is not None and existing_mime_type != part.mime_type:
                raise ValueError(
                    f"archive attachment '{part.attachment_id}' has conflicting MIME types"
                )
            attachment_ids.add(part.attachment_id)
            mime_types[part.attachment_id] = part.mime_type
    return attachment_ids, mime_types
