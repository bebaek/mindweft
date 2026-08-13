from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, Field

from app.private_keyring import load_encryption_keyring, parse_boolean
from minigent_config.constants import ATTACHMENT_DB_PATH_ENV

ATTACHMENT_MAX_PER_THREAD_ENV = "MINIGENT_ATTACHMENT_MAX_PER_THREAD"
ATTACHMENT_MAX_BYTES_PER_THREAD_ENV = "MINIGENT_ATTACHMENT_MAX_BYTES_PER_THREAD"
ATTACHMENT_MAX_PER_TENANT_ENV = "MINIGENT_ATTACHMENT_MAX_PER_TENANT"
ATTACHMENT_MAX_BYTES_PER_TENANT_ENV = "MINIGENT_ATTACHMENT_MAX_BYTES_PER_TENANT"
ATTACHMENT_PENDING_TTL_SECONDS_ENV = "MINIGENT_ATTACHMENT_PENDING_TTL_SECONDS"
ATTACHMENT_CLEANUP_INTERVAL_SECONDS_ENV = "MINIGENT_ATTACHMENT_CLEANUP_INTERVAL_SECONDS"
ATTACHMENT_ENCRYPTION_KEY_ENV = "MINIGENT_ATTACHMENT_ENCRYPTION_KEY"
ATTACHMENT_ENCRYPTION_KEYS_ENV = "MINIGENT_ATTACHMENT_ENCRYPTION_KEYS"
ATTACHMENT_KEY_VERSION_ENV = "MINIGENT_ATTACHMENT_KEY_VERSION"
ATTACHMENT_REENCRYPT_ON_STARTUP_ENV = "MINIGENT_ATTACHMENT_REENCRYPT_ON_STARTUP"
DEFAULT_ATTACHMENT_MAX_PER_THREAD = 100
DEFAULT_ATTACHMENT_MAX_BYTES_PER_THREAD = 256 * 1024 * 1024
DEFAULT_ATTACHMENT_MAX_PER_TENANT = 1_000
DEFAULT_ATTACHMENT_MAX_BYTES_PER_TENANT = 1024 * 1024 * 1024
DEFAULT_ATTACHMENT_PENDING_TTL_SECONDS = 24 * 60 * 60
DEFAULT_ATTACHMENT_CLEANUP_INTERVAL_SECONDS = 60 * 60
_NONCE_BYTES = 12


class AttachmentLimitExceeded(RuntimeError):
    def __init__(self, limit: str) -> None:
        self.limit = limit
        super().__init__(limit)


class UploadAttachmentRequest(BaseModel):
    mime_type: str
    data: str


class AttachmentMetadata(BaseModel):
    attachment_id: str
    thread_id: str
    mime_type: str
    size_bytes: int
    created_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class AttachmentRecord:
    metadata: AttachmentMetadata
    data: bytes


@dataclass(frozen=True)
class AttachmentCleanupResult:
    deleted_count: int = 0
    deleted_bytes: int = 0


@dataclass(frozen=True)
class AttachmentStatistics:
    total_count: int = 0
    total_bytes: int = 0
    pending_count: int = 0
    pending_bytes: int = 0
    referenced_count: int = 0
    referenced_bytes: int = 0
    exempt_count: int = 0
    exempt_bytes: int = 0
    oldest_pending_created_at: datetime | None = None


@dataclass(frozen=True)
class AttachmentStoreSettings:
    db_path: str | None = None
    max_per_thread: int = DEFAULT_ATTACHMENT_MAX_PER_THREAD
    max_bytes_per_thread: int = DEFAULT_ATTACHMENT_MAX_BYTES_PER_THREAD
    max_per_tenant: int = DEFAULT_ATTACHMENT_MAX_PER_TENANT
    max_bytes_per_tenant: int = DEFAULT_ATTACHMENT_MAX_BYTES_PER_TENANT
    pending_ttl_seconds: int = DEFAULT_ATTACHMENT_PENDING_TTL_SECONDS
    cleanup_interval_seconds: int = DEFAULT_ATTACHMENT_CLEANUP_INTERVAL_SECONDS
    encryption_key: bytes | None = field(default=None, repr=False)
    decryption_keys: Mapping[int, bytes] = field(default_factory=dict, repr=False)
    key_version: int = 1
    reencrypt_on_startup: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AttachmentStoreSettings:
        lookup = os.environ if env is None else env
        value = lookup.get(ATTACHMENT_DB_PATH_ENV, "").strip()
        has_key_material = bool(
            lookup.get(ATTACHMENT_ENCRYPTION_KEY_ENV, "").strip()
            or lookup.get(ATTACHMENT_ENCRYPTION_KEYS_ENV, "").strip()
        )
        key_version_configured = bool(lookup.get(ATTACHMENT_KEY_VERSION_ENV, "").strip())
        if key_version_configured and not has_key_material:
            raise RuntimeError(f"{ATTACHMENT_KEY_VERSION_ENV} requires attachment encryption keys")
        encryption_key: bytes | None = None
        decryption_keys: dict[int, bytes] = {}
        key_version = _positive_int_env(lookup, ATTACHMENT_KEY_VERSION_ENV, 1)
        if has_key_material:
            if not value:
                raise RuntimeError(
                    f"{ATTACHMENT_DB_PATH_ENV} is required when attachment encryption is configured"
                )
            encryption_key, decryption_keys, key_version = load_encryption_keyring(
                lookup,
                single_key_env=ATTACHMENT_ENCRYPTION_KEY_ENV,
                keyring_env=ATTACHMENT_ENCRYPTION_KEYS_ENV,
                key_version_env=ATTACHMENT_KEY_VERSION_ENV,
                database_env=ATTACHMENT_DB_PATH_ENV,
            )
        reencrypt_on_startup = parse_boolean(
            lookup.get(ATTACHMENT_REENCRYPT_ON_STARTUP_ENV, ""),
            ATTACHMENT_REENCRYPT_ON_STARTUP_ENV,
        )
        if reencrypt_on_startup and encryption_key is None:
            raise RuntimeError(
                f"{ATTACHMENT_REENCRYPT_ON_STARTUP_ENV} requires attachment encryption keys"
            )
        return cls(
            db_path=value or None,
            max_per_thread=_positive_int_env(
                lookup, ATTACHMENT_MAX_PER_THREAD_ENV, DEFAULT_ATTACHMENT_MAX_PER_THREAD
            ),
            max_bytes_per_thread=_positive_int_env(
                lookup,
                ATTACHMENT_MAX_BYTES_PER_THREAD_ENV,
                DEFAULT_ATTACHMENT_MAX_BYTES_PER_THREAD,
            ),
            max_per_tenant=_positive_int_env(
                lookup, ATTACHMENT_MAX_PER_TENANT_ENV, DEFAULT_ATTACHMENT_MAX_PER_TENANT
            ),
            max_bytes_per_tenant=_positive_int_env(
                lookup,
                ATTACHMENT_MAX_BYTES_PER_TENANT_ENV,
                DEFAULT_ATTACHMENT_MAX_BYTES_PER_TENANT,
            ),
            pending_ttl_seconds=_positive_int_env(
                lookup,
                ATTACHMENT_PENDING_TTL_SECONDS_ENV,
                DEFAULT_ATTACHMENT_PENDING_TTL_SECONDS,
            ),
            cleanup_interval_seconds=_positive_int_env(
                lookup,
                ATTACHMENT_CLEANUP_INTERVAL_SECONDS_ENV,
                DEFAULT_ATTACHMENT_CLEANUP_INTERVAL_SECONDS,
            ),
            encryption_key=encryption_key,
            decryption_keys=decryption_keys,
            key_version=key_version,
            reencrypt_on_startup=reencrypt_on_startup,
        )


class AttachmentStore(Protocol):
    def put(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        mime_type: str,
        data: bytes,
        created_by: str | None,
        max_per_thread: int | None = None,
        max_bytes_per_thread: int | None = None,
        max_per_tenant: int | None = None,
        max_bytes_per_tenant: int | None = None,
        pending_ttl_seconds: int | None = None,
    ) -> AttachmentMetadata: ...

    def get(
        self, tenant_id: str, thread_id: str, attachment_id: str
    ) -> AttachmentRecord | None: ...

    def usage(self, tenant_id: str, thread_id: str) -> tuple[int, int]: ...

    def tenant_usage(self, tenant_id: str) -> tuple[int, int]: ...

    def mark_referenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool: ...

    def unmark_referenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool: ...

    def delete_unreferenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool: ...

    def delete_expired_pending(self, *, now: datetime | None = None) -> int: ...

    def delete_expired_pending_with_stats(
        self, *, now: datetime | None = None
    ) -> AttachmentCleanupResult: ...

    def statistics(
        self, tenant_id: str, *, now: datetime | None = None
    ) -> AttachmentStatistics: ...

    def delete(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool: ...

    def delete_thread(self, tenant_id: str, thread_id: str) -> int: ...


class InMemoryAttachmentStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], AttachmentRecord] = {}
        self._reference_counts: dict[tuple[str, str, str], int] = {}
        self._expires_at: dict[tuple[str, str, str], datetime | None] = {}
        self._lock = RLock()

    def put(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        mime_type: str,
        data: bytes,
        created_by: str | None,
        max_per_thread: int | None = None,
        max_bytes_per_thread: int | None = None,
        max_per_tenant: int | None = None,
        max_bytes_per_tenant: int | None = None,
        pending_ttl_seconds: int | None = None,
    ) -> AttachmentMetadata:
        metadata = AttachmentMetadata(
            attachment_id=str(uuid4()),
            thread_id=thread_id,
            mime_type=mime_type,
            size_bytes=len(data),
            created_by=created_by,
        )
        with self._lock:
            count, total_bytes = self._usage_unlocked(tenant_id, thread_id)
            tenant_count, tenant_total_bytes = self._tenant_usage_unlocked(tenant_id)
            _enforce_limits(
                count=count,
                total_bytes=total_bytes,
                tenant_count=tenant_count,
                tenant_total_bytes=tenant_total_bytes,
                incoming_bytes=len(data),
                max_per_thread=max_per_thread,
                max_bytes_per_thread=max_bytes_per_thread,
                max_per_tenant=max_per_tenant,
                max_bytes_per_tenant=max_bytes_per_tenant,
            )
            key = (tenant_id, thread_id, metadata.attachment_id)
            self._records[key] = AttachmentRecord(
                metadata=metadata,
                data=bytes(data),
            )
            self._reference_counts[key] = 0
            self._expires_at[key] = _pending_expiration(
                metadata.created_at,
                pending_ttl_seconds,
            )
        return metadata

    def get(self, tenant_id: str, thread_id: str, attachment_id: str) -> AttachmentRecord | None:
        with self._lock:
            return self._records.get((tenant_id, thread_id, attachment_id))

    def _usage_unlocked(self, tenant_id: str, thread_id: str) -> tuple[int, int]:
        records = [
            record for key, record in self._records.items() if key[:2] == (tenant_id, thread_id)
        ]
        return len(records), sum(record.metadata.size_bytes for record in records)

    def usage(self, tenant_id: str, thread_id: str) -> tuple[int, int]:
        with self._lock:
            return self._usage_unlocked(tenant_id, thread_id)

    def _tenant_usage_unlocked(self, tenant_id: str) -> tuple[int, int]:
        records = [record for key, record in self._records.items() if key[0] == tenant_id]
        return len(records), sum(record.metadata.size_bytes for record in records)

    def tenant_usage(self, tenant_id: str) -> tuple[int, int]:
        with self._lock:
            return self._tenant_usage_unlocked(tenant_id)

    def mark_referenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        key = (tenant_id, thread_id, attachment_id)
        with self._lock:
            if key not in self._records:
                return False
            self._reference_counts[key] += 1
            return True

    def unmark_referenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        key = (tenant_id, thread_id, attachment_id)
        with self._lock:
            if key not in self._records:
                return False
            if self._reference_counts[key] > 0:
                self._reference_counts[key] -= 1
            return True

    def delete_unreferenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        key = (tenant_id, thread_id, attachment_id)
        with self._lock:
            if key not in self._records or self._reference_counts[key] != 0:
                return False
            self._delete_key_unlocked(key)
            return True

    def delete_expired_pending(self, *, now: datetime | None = None) -> int:
        return self.delete_expired_pending_with_stats(now=now).deleted_count

    def delete_expired_pending_with_stats(
        self, *, now: datetime | None = None
    ) -> AttachmentCleanupResult:
        cutoff = _utc_now() if now is None else _ensure_utc(now)
        with self._lock:
            keys = [
                key
                for key, expires_at in self._expires_at.items()
                if self._reference_counts[key] == 0
                and expires_at is not None
                and expires_at <= cutoff
            ]
            deleted_bytes = sum(self._records[key].metadata.size_bytes for key in keys)
            for key in keys:
                self._delete_key_unlocked(key)
            return AttachmentCleanupResult(
                deleted_count=len(keys),
                deleted_bytes=deleted_bytes,
            )

    def statistics(self, tenant_id: str, *, now: datetime | None = None) -> AttachmentStatistics:
        _ = now
        with self._lock:
            total_count = total_bytes = 0
            pending_count = pending_bytes = 0
            referenced_count = referenced_bytes = 0
            exempt_count = exempt_bytes = 0
            oldest_pending_created_at: datetime | None = None
            for key, record in self._records.items():
                if key[0] != tenant_id:
                    continue
                size_bytes = record.metadata.size_bytes
                total_count += 1
                total_bytes += size_bytes
                reference_count = self._reference_counts[key]
                expires_at = self._expires_at[key]
                if reference_count == 0 and expires_at is not None:
                    pending_count += 1
                    pending_bytes += size_bytes
                    created_at = _ensure_utc(record.metadata.created_at)
                    if oldest_pending_created_at is None or created_at < oldest_pending_created_at:
                        oldest_pending_created_at = created_at
                elif reference_count > 0:
                    referenced_count += 1
                    referenced_bytes += size_bytes
                else:
                    exempt_count += 1
                    exempt_bytes += size_bytes
            return AttachmentStatistics(
                total_count=total_count,
                total_bytes=total_bytes,
                pending_count=pending_count,
                pending_bytes=pending_bytes,
                referenced_count=referenced_count,
                referenced_bytes=referenced_bytes,
                exempt_count=exempt_count,
                exempt_bytes=exempt_bytes,
                oldest_pending_created_at=oldest_pending_created_at,
            )

    def _delete_key_unlocked(self, key: tuple[str, str, str]) -> None:
        del self._records[key]
        self._reference_counts.pop(key, None)
        self._expires_at.pop(key, None)

    def delete(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        key = (tenant_id, thread_id, attachment_id)
        with self._lock:
            if key not in self._records:
                return False
            self._delete_key_unlocked(key)
            return True

    def delete_thread(self, tenant_id: str, thread_id: str) -> int:
        with self._lock:
            keys = [key for key in self._records if key[:2] == (tenant_id, thread_id)]
            for key in keys:
                self._delete_key_unlocked(key)
            return len(keys)


class SQLiteAttachmentStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        encryption_key: bytes | None = None,
        key_version: int = 1,
        decryption_keys: Mapping[int, bytes] | None = None,
        reencrypt_on_startup: bool = False,
    ) -> None:
        self._db_path = str(db_path)
        self._lock = RLock()
        keys = dict(decryption_keys or {})
        if encryption_key is not None:
            if len(encryption_key) != 32:
                raise ValueError("attachment encryption key must contain exactly 32 bytes")
            if key_version < 1:
                raise ValueError("attachment encryption key version must be positive")
            existing = keys.get(key_version)
            if existing is not None and existing != encryption_key:
                raise ValueError("active attachment key conflicts with decryption keyring")
            keys[key_version] = encryption_key
            self._active_key_version: int | None = key_version
        else:
            self._active_key_version = None
        for version, key in keys.items():
            if version < 1 or len(key) != 32:
                raise ValueError("attachment decryption keys must be versioned 32-byte keys")
        self._aesgcms = {version: AESGCM(key) for version, key in keys.items()}
        Path(self._db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if reencrypt_on_startup:
            self.rotate_to_active_key()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    data BLOB NOT NULL,
                    nonce BLOB,
                    key_version INTEGER,
                    reference_count INTEGER,
                    expires_at TEXT
                )
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(attachments)")}
            if "nonce" not in columns:
                connection.execute("ALTER TABLE attachments ADD COLUMN nonce BLOB")
            if "key_version" not in columns:
                connection.execute("ALTER TABLE attachments ADD COLUMN key_version INTEGER")
            if "reference_count" not in columns:
                connection.execute("ALTER TABLE attachments ADD COLUMN reference_count INTEGER")
            if "expires_at" not in columns:
                connection.execute("ALTER TABLE attachments ADD COLUMN expires_at TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_scope "
                "ON attachments (tenant_id, thread_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_pending "
                "ON attachments (expires_at) WHERE reference_count = 0"
            )
            versions = {
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT key_version FROM attachments WHERE key_version IS NOT NULL"
                )
            }
            missing_versions = versions - self._aesgcms.keys()
            if missing_versions:
                missing = ", ".join(str(version) for version in sorted(missing_versions))
                raise RuntimeError(
                    f"Encrypted attachment database requires unavailable key version(s): {missing}"
                )
            sample = connection.execute(
                """
                SELECT attachment_id, tenant_id, thread_id, mime_type, size_bytes,
                       created_by, created_at, data, nonce, key_version
                FROM attachments
                WHERE key_version IS NOT NULL
                LIMIT 1
                """
            ).fetchone()
            if sample is not None:
                self._decrypt_row(sample)

    def put(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        mime_type: str,
        data: bytes,
        created_by: str | None,
        max_per_thread: int | None = None,
        max_bytes_per_thread: int | None = None,
        max_per_tenant: int | None = None,
        max_bytes_per_tenant: int | None = None,
        pending_ttl_seconds: int | None = None,
    ) -> AttachmentMetadata:
        metadata = AttachmentMetadata(
            attachment_id=str(uuid4()),
            thread_id=thread_id,
            mime_type=mime_type,
            size_bytes=len(data),
            created_by=created_by,
        )
        created_at = metadata.created_at.isoformat()
        expires_at = _pending_expiration(metadata.created_at, pending_ttl_seconds)
        stored_data = data
        nonce: bytes | None = None
        key_version = self._active_key_version
        if key_version is not None:
            nonce = os.urandom(_NONCE_BYTES)
            stored_data = self._aesgcms[key_version].encrypt(
                nonce,
                data,
                _attachment_associated_data(
                    tenant_id,
                    thread_id,
                    metadata.attachment_id,
                    mime_type,
                    metadata.size_bytes,
                    created_by,
                    created_at,
                    key_version,
                ),
            )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count, total_bytes = self._usage_with_connection(connection, tenant_id, thread_id)
            tenant_count, tenant_total_bytes = self._tenant_usage_with_connection(
                connection, tenant_id
            )
            _enforce_limits(
                count=count,
                total_bytes=total_bytes,
                tenant_count=tenant_count,
                tenant_total_bytes=tenant_total_bytes,
                incoming_bytes=len(data),
                max_per_thread=max_per_thread,
                max_bytes_per_thread=max_bytes_per_thread,
                max_per_tenant=max_per_tenant,
                max_bytes_per_tenant=max_bytes_per_tenant,
            )
            connection.execute(
                """
                INSERT INTO attachments (
                    attachment_id, tenant_id, thread_id, mime_type,
                    size_bytes, created_by, created_at, data, nonce, key_version,
                    reference_count, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata.attachment_id,
                    tenant_id,
                    thread_id,
                    metadata.mime_type,
                    metadata.size_bytes,
                    metadata.created_by,
                    created_at,
                    stored_data,
                    nonce,
                    key_version,
                    0,
                    expires_at.isoformat() if expires_at is not None else None,
                ),
            )
        return metadata

    def get(self, tenant_id: str, thread_id: str, attachment_id: str) -> AttachmentRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT attachment_id, tenant_id, thread_id, mime_type, size_bytes,
                       created_by, created_at, data, nonce, key_version
                FROM attachments
                WHERE tenant_id = ? AND thread_id = ? AND attachment_id = ?
                """,
                (tenant_id, thread_id, attachment_id),
            ).fetchone()
        if row is None:
            return None
        data = self._decrypt_row(row)
        return AttachmentRecord(
            metadata=AttachmentMetadata(
                attachment_id=attachment_id,
                thread_id=thread_id,
                mime_type=str(row[3]),
                size_bytes=int(row[4]),
                created_by=str(row[5]) if row[5] is not None else None,
                created_at=datetime.fromisoformat(str(row[6])),
            ),
            data=data,
        )

    def _decrypt_row(self, row: tuple[Any, ...]) -> bytes:
        key_version = int(row[9]) if row[9] is not None else None
        if key_version is None:
            return bytes(row[7])
        aesgcm = self._aesgcms.get(key_version)
        if aesgcm is None:
            raise RuntimeError(f"Attachment encryption key version {key_version} is unavailable")
        if row[8] is None:
            raise RuntimeError("Encrypted attachment nonce is missing")
        try:
            return aesgcm.decrypt(
                bytes(row[8]),
                bytes(row[7]),
                _attachment_associated_data(
                    str(row[1]),
                    str(row[2]),
                    str(row[0]),
                    str(row[3]),
                    int(row[4]),
                    str(row[5]) if row[5] is not None else None,
                    str(row[6]),
                    key_version,
                ),
            )
        except InvalidTag as exc:
            raise RuntimeError("Attachment authentication failed") from exc

    def rotate_to_active_key(self) -> int:
        key_version = self._active_key_version
        if key_version is None:
            raise RuntimeError("Attachment rotation requires an active encryption key")
        rotated = 0
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT attachment_id, tenant_id, thread_id, mime_type, size_bytes,
                       created_by, created_at, data, nonce, key_version
                FROM attachments
                WHERE key_version IS NULL OR key_version != ?
                """,
                (key_version,),
            ).fetchall()
            for row in rows:
                plaintext = self._decrypt_row(row)
                nonce = os.urandom(_NONCE_BYTES)
                ciphertext = self._aesgcms[key_version].encrypt(
                    nonce,
                    plaintext,
                    _attachment_associated_data(
                        str(row[1]),
                        str(row[2]),
                        str(row[0]),
                        str(row[3]),
                        int(row[4]),
                        str(row[5]) if row[5] is not None else None,
                        str(row[6]),
                        key_version,
                    ),
                )
                connection.execute(
                    """
                    UPDATE attachments
                    SET data = ?, nonce = ?, key_version = ?
                    WHERE attachment_id = ?
                    """,
                    (ciphertext, nonce, key_version, str(row[0])),
                )
                rotated += 1
        return rotated

    @staticmethod
    def _usage_with_connection(
        connection: sqlite3.Connection,
        tenant_id: str,
        thread_id: str,
    ) -> tuple[int, int]:
        row = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
            FROM attachments
            WHERE tenant_id = ? AND thread_id = ?
            """,
            (tenant_id, thread_id),
        ).fetchone()
        return int(row[0]), int(row[1])

    def usage(self, tenant_id: str, thread_id: str) -> tuple[int, int]:
        with self._lock, self._connection() as connection:
            return self._usage_with_connection(connection, tenant_id, thread_id)

    @staticmethod
    def _tenant_usage_with_connection(
        connection: sqlite3.Connection,
        tenant_id: str,
    ) -> tuple[int, int]:
        row = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
            FROM attachments
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def tenant_usage(self, tenant_id: str) -> tuple[int, int]:
        with self._lock, self._connection() as connection:
            return self._tenant_usage_with_connection(connection, tenant_id)

    def mark_referenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE attachments
                SET reference_count = CASE
                    WHEN reference_count IS NULL THEN -1
                    WHEN reference_count < 0 THEN reference_count
                    ELSE reference_count + 1
                END
                WHERE tenant_id = ? AND thread_id = ? AND attachment_id = ?
                """,
                (tenant_id, thread_id, attachment_id),
            )
            return cursor.rowcount > 0

    def unmark_referenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE attachments
                SET reference_count = CASE
                    WHEN reference_count > 0 THEN reference_count - 1
                    ELSE reference_count
                END
                WHERE tenant_id = ? AND thread_id = ? AND attachment_id = ?
                """,
                (tenant_id, thread_id, attachment_id),
            )
            return cursor.rowcount > 0

    def delete_unreferenced(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM attachments
                WHERE tenant_id = ? AND thread_id = ? AND attachment_id = ?
                  AND reference_count = 0
                """,
                (tenant_id, thread_id, attachment_id),
            )
            return cursor.rowcount > 0

    def delete_expired_pending(self, *, now: datetime | None = None) -> int:
        return self.delete_expired_pending_with_stats(now=now).deleted_count

    def delete_expired_pending_with_stats(
        self, *, now: datetime | None = None
    ) -> AttachmentCleanupResult:
        cutoff = _utc_now() if now is None else _ensure_utc(now)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            aggregate = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM attachments
                WHERE reference_count = 0 AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (cutoff.isoformat(),),
            ).fetchone()
            connection.execute(
                """
                DELETE FROM attachments
                WHERE reference_count = 0 AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (cutoff.isoformat(),),
            )
            return AttachmentCleanupResult(
                deleted_count=int(aggregate[0]),
                deleted_bytes=int(aggregate[1]),
            )

    def statistics(self, tenant_id: str, *, now: datetime | None = None) -> AttachmentStatistics:
        _ = now
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(size_bytes), 0),
                    COALESCE(SUM(CASE
                        WHEN reference_count = 0 AND expires_at IS NOT NULL THEN 1 ELSE 0
                    END), 0),
                    COALESCE(SUM(CASE
                        WHEN reference_count = 0 AND expires_at IS NOT NULL
                        THEN size_bytes ELSE 0
                    END), 0),
                    COALESCE(SUM(CASE WHEN reference_count > 0 THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE
                        WHEN reference_count > 0 THEN size_bytes ELSE 0
                    END), 0),
                    COALESCE(SUM(CASE
                        WHEN reference_count = 0 AND expires_at IS NOT NULL THEN 0
                        WHEN reference_count > 0 THEN 0
                        ELSE 1
                    END), 0),
                    COALESCE(SUM(CASE
                        WHEN reference_count = 0 AND expires_at IS NOT NULL THEN 0
                        WHEN reference_count > 0 THEN 0
                        ELSE size_bytes
                    END), 0),
                    MIN(CASE
                        WHEN reference_count = 0 AND expires_at IS NOT NULL THEN created_at
                    END)
                FROM attachments
                WHERE tenant_id = ?
                """,
                (tenant_id,),
            ).fetchone()
        oldest = datetime.fromisoformat(row[8]) if row[8] is not None else None
        return AttachmentStatistics(
            total_count=int(row[0]),
            total_bytes=int(row[1]),
            pending_count=int(row[2]),
            pending_bytes=int(row[3]),
            referenced_count=int(row[4]),
            referenced_bytes=int(row[5]),
            exempt_count=int(row[6]),
            exempt_bytes=int(row[7]),
            oldest_pending_created_at=_ensure_utc(oldest) if oldest is not None else None,
        )

    def delete(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM attachments
                WHERE tenant_id = ? AND thread_id = ? AND attachment_id = ?
                """,
                (tenant_id, thread_id, attachment_id),
            )
            return cursor.rowcount > 0

    def delete_thread(self, tenant_id: str, thread_id: str) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM attachments WHERE tenant_id = ? AND thread_id = ?",
                (tenant_id, thread_id),
            )
            return cursor.rowcount


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _pending_expiration(created_at: datetime, ttl_seconds: int | None) -> datetime | None:
    if ttl_seconds is None:
        return None
    if ttl_seconds < 1:
        raise ValueError("attachment pending TTL must be positive")
    return _ensure_utc(created_at) + timedelta(seconds=ttl_seconds)


def _attachment_associated_data(
    tenant_id: str,
    thread_id: str,
    attachment_id: str,
    mime_type: str,
    size_bytes: int,
    created_by: str | None,
    created_at: str,
    key_version: int,
) -> bytes:
    return json.dumps(
        [
            tenant_id,
            thread_id,
            attachment_id,
            mime_type,
            size_bytes,
            created_by,
            created_at,
            key_version,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _enforce_limits(
    *,
    count: int,
    total_bytes: int,
    tenant_count: int,
    tenant_total_bytes: int,
    incoming_bytes: int,
    max_per_thread: int | None,
    max_bytes_per_thread: int | None,
    max_per_tenant: int | None,
    max_bytes_per_tenant: int | None,
) -> None:
    if max_per_thread is not None and count >= max_per_thread:
        raise AttachmentLimitExceeded("count")
    if max_bytes_per_thread is not None and total_bytes + incoming_bytes > max_bytes_per_thread:
        raise AttachmentLimitExceeded("bytes")
    if max_per_tenant is not None and tenant_count >= max_per_tenant:
        raise AttachmentLimitExceeded("tenant_count")
    if max_bytes_per_tenant is not None and (
        tenant_total_bytes + incoming_bytes > max_bytes_per_tenant
    ):
        raise AttachmentLimitExceeded("tenant_bytes")


def _positive_int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def build_attachment_store(settings: AttachmentStoreSettings) -> AttachmentStore:
    if settings.db_path:
        return SQLiteAttachmentStore(
            settings.db_path,
            encryption_key=settings.encryption_key,
            key_version=settings.key_version,
            decryption_keys=settings.decryption_keys,
            reencrypt_on_startup=settings.reencrypt_on_startup,
        )
    return InMemoryAttachmentStore()
