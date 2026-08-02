from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.attachments import (
    AttachmentLimitExceeded,
    AttachmentStoreSettings,
    InMemoryAttachmentStore,
    SQLiteAttachmentStore,
)


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


def test_attachment_stores_enforce_limits_atomically(tmp_path: Path) -> None:
    stores = [InMemoryAttachmentStore(), SQLiteAttachmentStore(tmp_path / "limits.db")]
    for store in stores:
        store.put(
            "tenant-1",
            "thread-1",
            mime_type="image/png",
            data=b"first",
            created_by="user-1",
            max_per_thread=1,
            max_bytes_per_thread=10,
        )
        with pytest.raises(AttachmentLimitExceeded, match="count"):
            store.put(
                "tenant-1",
                "thread-1",
                mime_type="image/png",
                data=b"second",
                created_by="user-1",
                max_per_thread=1,
                max_bytes_per_thread=10,
            )
        assert store.usage("tenant-1", "thread-1") == (1, len(b"first"))

        store.put(
            "tenant-1",
            "thread-2",
            mime_type="image/png",
            data=b"first",
            created_by="user-1",
            max_per_thread=5,
            max_bytes_per_thread=10,
        )
        with pytest.raises(AttachmentLimitExceeded, match="bytes"):
            store.put(
                "tenant-1",
                "thread-2",
                mime_type="image/png",
                data=b"second",
                created_by="user-1",
                max_per_thread=5,
                max_bytes_per_thread=10,
            )
        assert store.usage("tenant-1", "thread-2") == (1, len(b"first"))


def test_sqlite_attachment_store_enforces_limit_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "shared-limits.db"
    stores = [SQLiteAttachmentStore(path), SQLiteAttachmentStore(path)]
    barrier = Barrier(2)

    def upload(store: SQLiteAttachmentStore) -> object:
        barrier.wait()
        try:
            return store.put(
                "tenant-1",
                "thread-1",
                mime_type="image/png",
                data=b"image",
                created_by="user-1",
                max_per_thread=1,
                max_bytes_per_thread=100,
            )
        except AttachmentLimitExceeded as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(upload, stores))

    assert sum(isinstance(result, AttachmentLimitExceeded) for result in results) == 1
    assert stores[0].usage("tenant-1", "thread-1") == (1, len(b"image"))


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
