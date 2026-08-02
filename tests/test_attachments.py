from pathlib import Path

from app.attachments import AttachmentStoreSettings, SQLiteAttachmentStore


def test_attachment_store_settings_from_env() -> None:
    defaults = AttachmentStoreSettings.from_env({})
    assert defaults.db_path is None
    assert defaults.max_per_thread == 100
    assert defaults.max_bytes_per_thread == 256 * 1024 * 1024
    assert AttachmentStoreSettings.from_env(
        {
            "MINIGENT_ATTACHMENT_DB_PATH": "/data/attachments.db",
            "MINIGENT_ATTACHMENT_MAX_PER_THREAD": "12",
            "MINIGENT_ATTACHMENT_MAX_BYTES_PER_THREAD": "3456",
        }
    ) == AttachmentStoreSettings(
        db_path="/data/attachments.db",
        max_per_thread=12,
        max_bytes_per_thread=3456,
    )


def test_sqlite_attachment_store_persists_and_scopes_records(tmp_path: Path) -> None:
    path = tmp_path / "attachments.db"
    first = SQLiteAttachmentStore(path)
    metadata = first.put(
        "tenant-1",
        "thread-1",
        mime_type="image/png",
        data=b"image-bytes",
        created_by="user-1",
    )

    second = SQLiteAttachmentStore(path)
    record = second.get("tenant-1", "thread-1", metadata.attachment_id)

    assert record is not None
    assert record.data == b"image-bytes"
    assert record.metadata.mime_type == "image/png"
    assert second.usage("tenant-1", "thread-1") == (1, len(b"image-bytes"))
    assert second.get("tenant-1", "thread-2", metadata.attachment_id) is None
    assert second.get("tenant-2", "thread-1", metadata.attachment_id) is None


def test_sqlite_attachment_store_deletes_thread_records(tmp_path: Path) -> None:
    store = SQLiteAttachmentStore(tmp_path / "attachments.db")
    first = store.put(
        "tenant-1",
        "thread-1",
        mime_type="image/png",
        data=b"first",
        created_by="user-1",
    )
    second = store.put(
        "tenant-1",
        "thread-2",
        mime_type="image/png",
        data=b"second",
        created_by="user-1",
    )

    assert store.delete("tenant-1", "thread-1", first.attachment_id) is True
    assert store.get("tenant-1", "thread-1", first.attachment_id) is None
    replacement = store.put(
        "tenant-1",
        "thread-1",
        mime_type="image/png",
        data=b"replacement",
        created_by="user-1",
    )
    assert store.delete_thread("tenant-1", "thread-1") == 1
    assert store.get("tenant-1", "thread-1", replacement.attachment_id) is None
    assert store.get("tenant-1", "thread-2", second.attachment_id) is not None
