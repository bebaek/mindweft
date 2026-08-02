import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.attachments import (
    AttachmentLimitExceeded,
    AttachmentStoreSettings,
    InMemoryAttachmentStore,
    SQLiteAttachmentStore,
)


def _encoded_key(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def test_attachment_store_settings_from_env() -> None:
    defaults = AttachmentStoreSettings.from_env({})
    assert defaults.db_path is None
    assert defaults.max_per_thread == 100
    assert defaults.max_bytes_per_thread == 256 * 1024 * 1024
    assert defaults.max_per_tenant == 1_000
    assert defaults.max_bytes_per_tenant == 1024 * 1024 * 1024
    assert defaults.pending_ttl_seconds == 24 * 60 * 60
    assert defaults.cleanup_interval_seconds == 15 * 60
    assert AttachmentStoreSettings.from_env(
        {
            "MINIGENT_ATTACHMENT_DB_PATH": "/data/attachments.db",
            "MINIGENT_ATTACHMENT_MAX_PER_THREAD": "12",
            "MINIGENT_ATTACHMENT_MAX_BYTES_PER_THREAD": "3456",
            "MINIGENT_ATTACHMENT_MAX_PER_TENANT": "78",
            "MINIGENT_ATTACHMENT_MAX_BYTES_PER_TENANT": "9012",
            "MINIGENT_ATTACHMENT_PENDING_TTL_SECONDS": "34",
            "MINIGENT_ATTACHMENT_CLEANUP_INTERVAL_SECONDS": "56",
        }
    ) == AttachmentStoreSettings(
        db_path="/data/attachments.db",
        max_per_thread=12,
        max_bytes_per_thread=3456,
        max_per_tenant=78,
        max_bytes_per_tenant=9012,
        pending_ttl_seconds=34,
        cleanup_interval_seconds=56,
    )


def test_attachment_store_settings_parse_encryption_keyring() -> None:
    key = b"a" * 32
    settings = AttachmentStoreSettings.from_env(
        {
            "MINIGENT_ATTACHMENT_DB_PATH": "/data/attachments.db",
            "MINIGENT_ATTACHMENT_ENCRYPTION_KEY": _encoded_key(key),
            "MINIGENT_ATTACHMENT_KEY_VERSION": "2",
            "MINIGENT_ATTACHMENT_REENCRYPT_ON_STARTUP": "true",
        }
    )

    assert settings.encryption_key == key
    assert settings.decryption_keys == {2: key}
    assert settings.key_version == 2
    assert settings.reencrypt_on_startup is True

    with pytest.raises(RuntimeError, match="requires attachment encryption keys"):
        AttachmentStoreSettings.from_env(
            {
                "MINIGENT_ATTACHMENT_DB_PATH": "/data/attachments.db",
                "MINIGENT_ATTACHMENT_KEY_VERSION": "2",
            }
        )


def test_sqlite_attachment_store_migrates_pre_encryption_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy-schema.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE attachments (
                attachment_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_by TEXT,
                created_at TEXT NOT NULL,
                data BLOB NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO attachments (
                attachment_id, tenant_id, thread_id, mime_type,
                size_bytes, created_by, created_at, data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-1",
                "tenant-1",
                "thread-1",
                "image/png",
                len(b"legacy"),
                "user-1",
                "2026-01-01T00:00:00+00:00",
                b"legacy",
            ),
        )

    store = SQLiteAttachmentStore(path)
    record = store.get("tenant-1", "thread-1", "legacy-1")

    assert record is not None
    assert record.data == b"legacy"
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(attachments)")}
    assert {"nonce", "key_version", "reference_count", "expires_at"} <= columns
    assert store.delete_expired_pending(now=datetime(2100, 1, 1, tzinfo=timezone.utc)) == 0
    assert store.mark_referenced("tenant-1", "thread-1", "legacy-1") is True
    assert store.unmark_referenced("tenant-1", "thread-1", "legacy-1") is True
    assert store.delete_unreferenced("tenant-1", "thread-1", "legacy-1") is False
    with sqlite3.connect(path) as connection:
        reference_count = connection.execute(
            "SELECT reference_count FROM attachments WHERE attachment_id = 'legacy-1'"
        ).fetchone()[0]
    assert reference_count == -1


def test_sqlite_attachment_store_encrypts_and_authenticates_data(tmp_path: Path) -> None:
    path = tmp_path / "encrypted.db"
    key = b"a" * 32
    store = SQLiteAttachmentStore(path, encryption_key=key)
    metadata = store.put(
        "tenant-1",
        "thread-1",
        mime_type="image/png",
        data=b"sensitive-image",
        created_by="user-1",
    )

    with sqlite3.connect(path) as connection:
        stored_data, nonce, key_version = connection.execute(
            "SELECT data, nonce, key_version FROM attachments"
        ).fetchone()
    assert bytes(stored_data) != b"sensitive-image"
    assert len(bytes(nonce)) == 12
    assert key_version == 1
    assert (
        SQLiteAttachmentStore(path, encryption_key=key)
        .get("tenant-1", "thread-1", metadata.attachment_id)
        .data
        == b"sensitive-image"
    )

    with pytest.raises(RuntimeError, match="authentication failed"):
        SQLiteAttachmentStore(path, encryption_key=b"b" * 32)


def test_sqlite_attachment_store_rotates_keys_and_legacy_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "rotation.db"
    legacy = SQLiteAttachmentStore(path)
    legacy_metadata = legacy.put(
        "tenant-1",
        "thread-1",
        mime_type="image/png",
        data=b"legacy-image",
        created_by="user-1",
    )
    key_one = b"1" * 32
    first = SQLiteAttachmentStore(
        path,
        encryption_key=key_one,
        key_version=1,
        reencrypt_on_startup=True,
    )
    assert first.get("tenant-1", "thread-1", legacy_metadata.attachment_id).data == b"legacy-image"

    key_two = b"2" * 32
    rotated = SQLiteAttachmentStore(
        path,
        encryption_key=key_two,
        key_version=2,
        decryption_keys={1: key_one},
        reencrypt_on_startup=True,
    )
    assert (
        rotated.get("tenant-1", "thread-1", legacy_metadata.attachment_id).data == b"legacy-image"
    )
    with sqlite3.connect(path) as connection:
        versions = {row[0] for row in connection.execute("SELECT key_version FROM attachments")}
    assert versions == {2}
    assert (
        SQLiteAttachmentStore(
            path,
            encryption_key=key_two,
            key_version=2,
        )
        .get("tenant-1", "thread-1", legacy_metadata.attachment_id)
        .data
        == b"legacy-image"
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
    assert second.tenant_usage("tenant-1") == (1, len(b"image-bytes"))
    assert second.get("tenant-1", "thread-2", metadata.attachment_id) is None
    assert second.get("tenant-2", "thread-1", metadata.attachment_id) is None


def test_attachment_stores_expire_only_unreferenced_pending_uploads(tmp_path: Path) -> None:
    stores = [InMemoryAttachmentStore(), SQLiteAttachmentStore(tmp_path / "pending.db")]
    for store in stores:
        metadata = store.put(
            "tenant-1",
            "thread-1",
            mime_type="image/png",
            data=b"pending-image",
            created_by="user-1",
            pending_ttl_seconds=60,
        )
        before_expiry = metadata.created_at + timedelta(seconds=59)
        after_expiry = metadata.created_at + timedelta(seconds=61)

        assert store.delete_expired_pending(now=before_expiry) == 0
        assert store.mark_referenced("tenant-1", "thread-1", metadata.attachment_id) is True
        assert store.delete_expired_pending(now=after_expiry) == 0
        assert store.delete_unreferenced("tenant-1", "thread-1", metadata.attachment_id) is False
        assert store.unmark_referenced("tenant-1", "thread-1", metadata.attachment_id) is True
        assert store.delete_expired_pending(now=after_expiry) == 1
        assert store.get("tenant-1", "thread-1", metadata.attachment_id) is None


def test_attachment_stores_report_tenant_statistics_and_cleanup_bytes(tmp_path: Path) -> None:
    stores = [InMemoryAttachmentStore(), SQLiteAttachmentStore(tmp_path / "statistics.db")]
    for store in stores:
        pending = store.put(
            "tenant-1",
            "thread-1",
            mime_type="image/png",
            data=b"pending",
            created_by="user-1",
            pending_ttl_seconds=60,
        )
        referenced = store.put(
            "tenant-1",
            "thread-1",
            mime_type="image/png",
            data=b"referenced",
            created_by="user-1",
            pending_ttl_seconds=60,
        )
        store.put(
            "tenant-1",
            "thread-2",
            mime_type="image/png",
            data=b"exempt",
            created_by="user-1",
        )
        other_tenant = store.put(
            "tenant-2",
            "thread-1",
            mime_type="image/png",
            data=b"other-tenant",
            created_by="user-2",
            pending_ttl_seconds=60,
        )
        assert store.mark_referenced("tenant-1", "thread-1", referenced.attachment_id) is True
        assert store.mark_referenced("tenant-2", "thread-1", other_tenant.attachment_id) is True

        statistics = store.statistics("tenant-1")

        assert statistics.total_count == 3
        assert statistics.total_bytes == len(b"pendingreferencedexempt")
        assert (statistics.pending_count, statistics.pending_bytes) == (1, len(b"pending"))
        assert (statistics.referenced_count, statistics.referenced_bytes) == (
            1,
            len(b"referenced"),
        )
        assert (statistics.exempt_count, statistics.exempt_bytes) == (1, len(b"exempt"))
        assert statistics.oldest_pending_created_at == pending.created_at

        result = store.delete_expired_pending_with_stats(
            now=pending.created_at + timedelta(seconds=61)
        )
        assert (result.deleted_count, result.deleted_bytes) == (1, len(b"pending"))


def test_sqlite_pending_cleanup_is_atomic_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "pending-shared.db"
    stores = [SQLiteAttachmentStore(path), SQLiteAttachmentStore(path)]
    metadata = stores[0].put(
        "tenant-1",
        "thread-1",
        mime_type="image/png",
        data=b"pending-image",
        created_by="user-1",
        pending_ttl_seconds=1,
    )
    barrier = Barrier(2)

    def cleanup(store: SQLiteAttachmentStore) -> int:
        barrier.wait()
        return store.delete_expired_pending(now=metadata.created_at + timedelta(seconds=2))

    with ThreadPoolExecutor(max_workers=2) as executor:
        deleted = list(executor.map(cleanup, stores))

    assert sorted(deleted) == [0, 1]
    assert stores[0].get("tenant-1", "thread-1", metadata.attachment_id) is None


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


def test_attachment_stores_enforce_tenant_limits_across_threads(tmp_path: Path) -> None:
    stores = [InMemoryAttachmentStore(), SQLiteAttachmentStore(tmp_path / "tenant-limits.db")]
    for store in stores:
        for thread_id in ("thread-1", "thread-2"):
            store.put(
                "tenant-1",
                thread_id,
                mime_type="image/png",
                data=b"image",
                created_by="user-1",
                max_per_tenant=2,
                max_bytes_per_tenant=100,
            )
        with pytest.raises(AttachmentLimitExceeded, match="tenant_count"):
            store.put(
                "tenant-1",
                "thread-3",
                mime_type="image/png",
                data=b"image",
                created_by="user-1",
                max_per_tenant=2,
                max_bytes_per_tenant=100,
            )
        store.put(
            "tenant-2",
            "thread-1",
            mime_type="image/png",
            data=b"separate",
            created_by="user-2",
            max_per_tenant=2,
            max_bytes_per_tenant=100,
        )
        with pytest.raises(AttachmentLimitExceeded, match="tenant_bytes"):
            store.put(
                "tenant-2",
                "thread-2",
                mime_type="image/png",
                data=b"more",
                created_by="user-2",
                max_per_tenant=5,
                max_bytes_per_tenant=10,
            )
        assert store.tenant_usage("tenant-1") == (2, 2 * len(b"image"))
        assert store.tenant_usage("tenant-2") == (1, len(b"separate"))


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


def test_sqlite_attachment_store_enforces_tenant_limit_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "shared-tenant-limits.db"
    stores = [SQLiteAttachmentStore(path), SQLiteAttachmentStore(path)]
    barrier = Barrier(2)

    def upload(item: tuple[int, SQLiteAttachmentStore]) -> object:
        index, store = item
        barrier.wait()
        try:
            return store.put(
                "tenant-1",
                f"thread-{index}",
                mime_type="image/png",
                data=b"image",
                created_by="user-1",
                max_per_tenant=1,
                max_bytes_per_tenant=100,
            )
        except AttachmentLimitExceeded as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(upload, enumerate(stores)))

    assert sum(isinstance(result, AttachmentLimitExceeded) for result in results) == 1
    assert stores[0].tenant_usage("tenant-1") == (1, len(b"image"))


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
