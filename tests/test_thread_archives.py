from datetime import datetime, timezone

import pytest

from app.models import DocumentPart, Message, MessageRole, Thread, ThreadContext
from app.thread_archives import (
    ThreadArchiveV1,
    build_thread_archive,
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
    assert "schema_name" not in payload
    assert "tenant_id" not in payload["thread"]
    assert "execution_user_id" not in payload["thread"]
    assert "created_by" not in payload["messages"][0]
    restored_archive = ThreadArchiveV1.model_validate(payload)
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


def test_thread_archive_rejects_attachment_parts_for_core_format() -> None:
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

    with pytest.raises(ValueError, match="attachment parts"):
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
