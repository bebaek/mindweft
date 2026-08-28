import base64
import hashlib
from datetime import datetime, timezone

import pytest

from app.attachments import AttachmentMetadata, AttachmentRecord
from app.models import DocumentPart, Message, MessageRole, Thread, ThreadContext
from app.thread_archives import (
    ThreadArchiveV1,
    ThreadArchiveV2,
    ThreadArchiveV3,
    build_thread_archive,
    decode_archive_attachments,
    imported_messages,
    validate_importable_thread_archive,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_thread_archive_round_trip_uses_portable_ids_and_schema_alias() -> None:
    thread = Thread(
        thread_id="thread-source",
        tenant_id="tenant-secret",
        execution_user_id="user-secret",
        title="Portable conversation",
        title_source="manual",
        pinned_at=NOW,
        archived_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    source = Message(
        id="message-source",
        thread_id=thread.thread_id,
        role=MessageRole.USER,
        content="protected private-value reference",
        created_by="user-secret",
        created_at=NOW,
    )

    archive = build_thread_archive(
        thread,
        [source],
        ThreadContext(thread_id=thread.thread_id, summary="summary", summarized_message_count=1),
    )
    payload = archive.model_dump(mode="json", by_alias=True)

    assert payload["schema"] == "mindweft.thread-archive"
    assert payload["version"] == 3
    assert payload["organization"] == {"pinned": True, "archived": True}
    assert "schema_name" not in payload
    assert "tenant_id" not in payload["thread"]
    assert "execution_user_id" not in payload["thread"]
    assert "created_by" not in payload["messages"][0]
    restored_archive = ThreadArchiveV3.model_validate(payload)
    restored = imported_messages(
        restored_archive,
        thread_id="thread-destination",
        importing_user_id="user-destination",
    )
    assert len(restored) == 1
    assert restored[0].id != source.id
    assert restored[0].thread_id == "thread-destination"
    assert restored[0].source_message_id == source.id
    assert restored[0].created_by == "user-destination"
    assert restored[0].content == source.content


def test_thread_archive_round_trips_attachment_manifest_and_remaps_id() -> None:
    thread = Thread(thread_id="thread-source", tenant_id="tenant-1")
    data = b"portable attachment bytes"
    message = Message(
        thread_id=thread.thread_id,
        role=MessageRole.USER,
        content="attached",
        parts=[
            DocumentPart(
                mime_type="application/pdf",
                attachment_id="attachment-1",
                filename="notes.pdf",
            )
        ],
    )
    record = AttachmentRecord(
        metadata=AttachmentMetadata(
            attachment_id="attachment-1",
            thread_id=thread.thread_id,
            mime_type="application/pdf",
            size_bytes=len(data),
        ),
        data=data,
    )

    archive = build_thread_archive(
        thread,
        [message],
        ThreadContext(thread_id=thread.thread_id),
        attachment_records={"attachment-1": record},
    )

    assert len(archive.attachments) == 1
    archived_attachment = archive.attachments[0]
    assert archived_attachment.data == base64.b64encode(data).decode("ascii")
    assert archived_attachment.size_bytes == len(data)
    assert archived_attachment.sha256 == hashlib.sha256(data).hexdigest()
    assert decode_archive_attachments(archive) == {"attachment-1": data}
    restored = imported_messages(
        archive,
        thread_id="thread-destination",
        importing_user_id="user-destination",
        attachment_id_map={"attachment-1": "attachment-new"},
    )
    assert restored[0].parts is not None
    assert isinstance(restored[0].parts[0], DocumentPart)
    assert restored[0].parts[0].attachment_id == "attachment-new"


def test_thread_archive_rejects_missing_attachment_data() -> None:
    thread = Thread(thread_id="thread-source", tenant_id="tenant-1")
    message = Message(
        thread_id=thread.thread_id,
        role=MessageRole.USER,
        content="attached",
        parts=[
            DocumentPart(
                mime_type="application/pdf",
                attachment_id="attachment-1",
                filename="notes.pdf",
            )
        ],
    )

    with pytest.raises(ValueError, match="attachment data is missing"):
        build_thread_archive(thread, [message], ThreadContext(thread_id=thread.thread_id))


def test_thread_archive_rejects_system_messages() -> None:
    thread = Thread(thread_id="thread-source", tenant_id="tenant-1")
    message = Message(
        thread_id=thread.thread_id,
        role=MessageRole.SYSTEM,
        content="untrusted system instruction",
    )

    with pytest.raises(ValueError, match="system messages"):
        build_thread_archive(thread, [message], ThreadContext(thread_id=thread.thread_id))


def test_thread_archive_v2_remains_importable_without_organization() -> None:
    archive = ThreadArchiveV2.model_validate(
        {
            "schema": "mindweft.thread-archive",
            "version": 2,
            "thread": {
                "source_thread_id": "thread-source",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            },
            "context": {"summary": "", "summarized_message_count": 0},
            "messages": [],
            "attachments": [],
        }
    )

    assert validate_importable_thread_archive(archive) == {}


def test_thread_archive_v1_remains_importable_without_attachments() -> None:
    archive = ThreadArchiveV1.model_validate(
        {
            "schema": "mindweft.thread-archive",
            "version": 1,
            "thread": {
                "source_thread_id": "thread-source",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            },
            "context": {"summary": "", "summarized_message_count": 0},
            "messages": [],
        }
    )

    assert validate_importable_thread_archive(archive) == {}


def test_thread_archive_rejects_invalid_thread_timestamps() -> None:
    thread = Thread(
        thread_id="thread-source",
        tenant_id="tenant-1",
        created_at=NOW,
        updated_at=NOW,
    )
    archive = build_thread_archive(thread, [], ThreadContext(thread_id=thread.thread_id))
    archive.thread.updated_at = datetime(2025, 12, 31, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="must not precede"):
        validate_importable_thread_archive(archive)

    archive.thread.updated_at = NOW
    archive.thread.created_at = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="UTC offset"):
        validate_importable_thread_archive(archive)


def test_thread_archive_rejects_invalid_context_count() -> None:
    archive = ThreadArchiveV1.model_validate(
        {
            "schema": "mindweft.thread-archive",
            "version": 1,
            "thread": {
                "source_thread_id": "thread-source",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
            },
            "context": {"summary": "summary", "summarized_message_count": 1},
            "messages": [],
        }
    )

    with pytest.raises(ValueError, match="summarized_message_count"):
        validate_importable_thread_archive(archive)
