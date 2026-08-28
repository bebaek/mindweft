from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.attachments import AttachmentRecord
from app.message_parts import is_attachment_part, remap_attachment_ids
from app.models import (
    Message,
    MessagePart,
    MessageRole,
    Thread,
    ThreadContext,
    ThreadImportProvenance,
    utc_now,
)

THREAD_ARCHIVE_SCHEMA = "mindweft.thread-archive"
THREAD_ARCHIVE_VERSION = 4
THREAD_LINEAGE_ARCHIVE_SCHEMA = "mindweft.thread-lineage-archive"
THREAD_LINEAGE_ARCHIVE_VERSION = 1
ThreadArchiveProfilePolicy = Literal["defaults", "available", "strict"]
ThreadArchiveOrganizationPolicy = Literal["reset", "preserve"]
ThreadArchiveTimestampPolicy = Literal["reset", "preserve"]
MAX_ARCHIVE_MESSAGES = 10_000
MAX_ARCHIVE_ATTACHMENTS = 1_000
MAX_ARCHIVE_PROVENANCE_HOPS = 64
MAX_LINEAGE_ARCHIVE_THREADS = 100
MAX_LINEAGE_ARCHIVE_MESSAGES = 10_000
MAX_LINEAGE_ARCHIVE_ATTACHMENTS = 1_000


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
    version: Literal[2] = 2
    attachments: list[ThreadArchiveAttachment] = Field(
        default_factory=list,
        max_length=MAX_ARCHIVE_ATTACHMENTS,
    )


class ThreadArchiveOrganization(ThreadArchiveModel):
    pinned: bool = False
    archived: bool = False


class ThreadArchiveV3(ThreadArchiveBase):
    version: Literal[3] = 3
    attachments: list[ThreadArchiveAttachment] = Field(
        default_factory=list,
        max_length=MAX_ARCHIVE_ATTACHMENTS,
    )
    organization: ThreadArchiveOrganization


class ThreadArchiveImportProvenance(ThreadArchiveModel):
    archive_id: str = Field(min_length=1)
    source_thread_id: str = Field(min_length=1)
    imported_at: datetime


class ThreadArchiveV4(ThreadArchiveBase):
    version: Literal[4] = THREAD_ARCHIVE_VERSION
    attachments: list[ThreadArchiveAttachment] = Field(
        default_factory=list,
        max_length=MAX_ARCHIVE_ATTACHMENTS,
    )
    organization: ThreadArchiveOrganization
    import_provenance_chain: list[ThreadArchiveImportProvenance] = Field(
        default_factory=list,
        max_length=MAX_ARCHIVE_PROVENANCE_HOPS,
    )


ThreadArchive = Annotated[
    ThreadArchiveV1 | ThreadArchiveV2 | ThreadArchiveV3 | ThreadArchiveV4,
    Field(discriminator="version"),
]


class ThreadLineageArchiveEntry(ThreadArchiveModel):
    archive: ThreadArchiveV4
    parent_source_thread_id: str | None = None
    fork_source_message_id: str | None = None
    compacted_through_source_message_id: str | None = None


class ThreadLineageArchiveV1(ThreadArchiveModel):
    schema_name: Literal["mindweft.thread-lineage-archive"] = Field(
        default=THREAD_LINEAGE_ARCHIVE_SCHEMA,
        validation_alias="schema",
        serialization_alias="schema",
    )
    version: Literal[1] = THREAD_LINEAGE_ARCHIVE_VERSION
    archive_id: str = Field(default_factory=lambda: str(uuid4()))
    exported_at: datetime = Field(default_factory=utc_now)
    requested_source_thread_id: str = Field(min_length=1)
    root_source_thread_id: str = Field(min_length=1)
    threads: list[ThreadLineageArchiveEntry] = Field(
        min_length=1,
        max_length=MAX_LINEAGE_ARCHIVE_THREADS,
    )

    @model_validator(mode="after")
    def validate_tree(self) -> ThreadLineageArchiveV1:
        entries_by_id: dict[str, ThreadLineageArchiveEntry] = {}
        seen_ids: set[str] = set()
        for index, entry in enumerate(self.threads):
            source_thread_id = entry.archive.thread.source_thread_id
            if source_thread_id in entries_by_id:
                raise ValueError("lineage archive source thread IDs must be unique")
            if index == 0:
                if source_thread_id != self.root_source_thread_id:
                    raise ValueError("lineage archive root must be the first thread")
                if entry.parent_source_thread_id is not None:
                    raise ValueError("lineage archive root must not have a parent")
            elif entry.parent_source_thread_id not in seen_ids:
                raise ValueError("lineage archive parents must precede their children")
            if entry.parent_source_thread_id is None:
                if entry.fork_source_message_id is not None:
                    raise ValueError("lineage archive root must not have a fork message")
                if entry.compacted_through_source_message_id is not None:
                    raise ValueError("lineage archive root must not have a compaction boundary")
            else:
                parent = entries_by_id[entry.parent_source_thread_id]
                parent_message_ids = {
                    message.source_message_id for message in parent.archive.messages
                }
                if entry.fork_source_message_id not in parent_message_ids:
                    raise ValueError("lineage archive fork message must belong to the parent")
                if (
                    entry.compacted_through_source_message_id is not None
                    and entry.compacted_through_source_message_id not in parent_message_ids
                ):
                    raise ValueError(
                        "lineage archive compaction boundary must belong to the parent"
                    )
            entries_by_id[source_thread_id] = entry
            seen_ids.add(source_thread_id)
        if self.requested_source_thread_id not in entries_by_id:
            raise ValueError("lineage archive requested thread must be included")
        return self


class ThreadArchiveImportWarning(ThreadArchiveModel):
    code: str
    message: str


class ThreadArchiveImportResponse(ThreadArchiveModel):
    thread_id: str | None
    source_thread_id: str
    message_count: int
    attachment_count: int
    profile_policy: ThreadArchiveProfilePolicy
    organization_policy: ThreadArchiveOrganizationPolicy = "reset"
    timestamp_policy: ThreadArchiveTimestampPolicy = "reset"
    dry_run: bool = False
    warnings: list[ThreadArchiveImportWarning] = Field(default_factory=list)


def build_thread_archive(
    thread: Thread,
    messages: list[Message],
    context: ThreadContext,
    attachment_records: Mapping[str, AttachmentRecord] | None = None,
) -> ThreadArchiveV4:
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
    return ThreadArchiveV4(
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
        organization=ThreadArchiveOrganization(
            pinned=thread.pinned_at is not None,
            archived=thread.archived_at is not None,
        ),
        import_provenance_chain=[
            ThreadArchiveImportProvenance(
                archive_id=hop.archive_id,
                source_thread_id=hop.source_thread_id,
                imported_at=hop.imported_at,
            )
            for hop in _thread_import_provenance_chain(thread)
        ],
    )


def validate_importable_thread_archive(archive: ThreadArchive) -> dict[str, bytes]:
    if (
        archive.thread.created_at.utcoffset() is None
        or archive.thread.updated_at.utcoffset() is None
    ):
        raise ValueError("archive thread timestamps must include a UTC offset")
    if archive.thread.updated_at < archive.thread.created_at:
        raise ValueError("archive thread updated_at must not precede created_at")
    if isinstance(archive, ThreadArchiveV4):
        for hop in archive.import_provenance_chain:
            if hop.imported_at.utcoffset() is None:
                raise ValueError("archive import-provenance timestamps must include a UTC offset")
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


def _thread_import_provenance_chain(thread: Thread) -> list[ThreadImportProvenance]:
    if thread.import_provenance_chain:
        return thread.import_provenance_chain[:MAX_ARCHIVE_PROVENANCE_HOPS]
    if (
        thread.import_source_archive_id is None
        or thread.import_source_thread_id is None
        or thread.imported_at is None
    ):
        return []
    return [
        ThreadImportProvenance(
            archive_id=thread.import_source_archive_id,
            source_thread_id=thread.import_source_thread_id,
            imported_at=thread.imported_at,
        )
    ]


def _archive_attachments(archive: ThreadArchive) -> list[ThreadArchiveAttachment]:
    return (
        archive.attachments
        if isinstance(archive, (ThreadArchiveV2, ThreadArchiveV3, ThreadArchiveV4))
        else []
    )


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
