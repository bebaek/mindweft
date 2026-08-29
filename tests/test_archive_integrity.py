from __future__ import annotations

from app import thread_archives
from app.models import Message, MessageRole, Thread, ThreadContext
from mindweft_archive import (
    LINEAGE_ARCHIVE_CHECKSUM_VERSION,
    LINEAGE_ARCHIVE_SCHEMA,
    THREAD_ARCHIVE_CHECKSUM_VERSION,
    THREAD_ARCHIVE_SCHEMA,
    content_sha256,
)
from mindweft_client import archive_commands


def test_content_checksum_is_canonical_and_does_not_mutate_payload() -> None:
    payload = {
        "schema": THREAD_ARCHIVE_SCHEMA,
        "version": THREAD_ARCHIVE_CHECKSUM_VERSION,
        "content_sha256": "ignored",
        "metadata": {"z": "café", "a": [2, 1]},
    }
    reordered = {
        "metadata": {"a": [2, 1], "z": "café"},
        "content_sha256": "different ignored value",
        "version": THREAD_ARCHIVE_CHECKSUM_VERSION,
        "schema": THREAD_ARCHIVE_SCHEMA,
    }

    original = dict(payload)

    assert content_sha256(payload) == content_sha256(reordered)
    assert payload == original


def test_server_and_client_share_archive_integrity_contract() -> None:
    assert thread_archives.content_sha256 is content_sha256
    assert archive_commands.content_sha256 is content_sha256
    assert thread_archives.THREAD_ARCHIVE_SCHEMA == THREAD_ARCHIVE_SCHEMA
    assert thread_archives.THREAD_ARCHIVE_VERSION == THREAD_ARCHIVE_CHECKSUM_VERSION
    assert thread_archives.THREAD_LINEAGE_ARCHIVE_SCHEMA == LINEAGE_ARCHIVE_SCHEMA
    assert thread_archives.THREAD_LINEAGE_ARCHIVE_VERSION == LINEAGE_ARCHIVE_CHECKSUM_VERSION

    thread = Thread(thread_id="source-thread", tenant_id="tenant-1")
    message = Message(
        id="source-message",
        thread_id=thread.thread_id,
        role=MessageRole.USER,
        content="portable content",
    )
    archive = thread_archives.build_thread_archive(
        thread,
        [message],
        ThreadContext(thread_id=thread.thread_id),
    )
    payload = archive.model_dump(mode="json", by_alias=True)

    assert payload["content_sha256"] == content_sha256(payload)
    assert archive_commands.verify_archive_payload(payload) == {
        "valid": True,
        "schema": THREAD_ARCHIVE_SCHEMA,
        "version": THREAD_ARCHIVE_CHECKSUM_VERSION,
        "archive_id": archive.archive_id,
        "content_sha256": archive.content_sha256,
        "thread_count": 1,
        "attachment_count": 0,
    }
