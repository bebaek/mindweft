from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, Field

from app.private_keyring import load_encryption_keyring, parse_boolean

ATTACHMENT_DB_PATH_ENV = "MINIGENT_ATTACHMENT_DB_PATH"
ATTACHMENT_MAX_PER_THREAD_ENV = "MINIGENT_ATTACHMENT_MAX_PER_THREAD"
ATTACHMENT_MAX_BYTES_PER_THREAD_ENV = "MINIGENT_ATTACHMENT_MAX_BYTES_PER_THREAD"
ATTACHMENT_ENCRYPTION_KEY_ENV = "MINIGENT_ATTACHMENT_ENCRYPTION_KEY"
ATTACHMENT_ENCRYPTION_KEYS_ENV = "MINIGENT_ATTACHMENT_ENCRYPTION_KEYS"
ATTACHMENT_KEY_VERSION_ENV = "MINIGENT_ATTACHMENT_KEY_VERSION"
ATTACHMENT_REENCRYPT_ON_STARTUP_ENV = "MINIGENT_ATTACHMENT_REENCRYPT_ON_STARTUP"
DEFAULT_ATTACHMENT_MAX_PER_THREAD = 100
DEFAULT_ATTACHMENT_MAX_BYTES_PER_THREAD = 256 * 1024 * 1024
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
class AttachmentStoreSettings:
    db_path: str | None = None
    max_per_thread: int = DEFAULT_ATTACHMENT_MAX_PER_THREAD
    max_bytes_per_thread: int = DEFAULT_ATTACHMENT_MAX_BYTES_PER_THREAD
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
    ) -> AttachmentMetadata: ...

    def get(
        self, tenant_id: str, thread_id: str, attachment_id: str
    ) -> AttachmentRecord | None: ...

    def usage(self, tenant_id: str, thread_id: str) -> tuple[int, int]: ...

    def delete(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool: ...

    def delete_thread(self, tenant_id: str, thread_id: str) -> int: ...


class InMemoryAttachmentStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], AttachmentRecord] = {}
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
            _enforce_limits(
                count=count,
                total_bytes=total_bytes,
                incoming_bytes=len(data),
                max_per_thread=max_per_thread,
                max_bytes_per_thread=max_bytes_per_thread,
            )
            self._records[(tenant_id, thread_id, metadata.attachment_id)] = AttachmentRecord(
                metadata=metadata,
                data=bytes(data),
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

    def delete(self, tenant_id: str, thread_id: str, attachment_id: str) -> bool:
        with self._lock:
            return self._records.pop((tenant_id, thread_id, attachment_id), None) is not None

    def delete_thread(self, tenant_id: str, thread_id: str) -> int:
        with self._lock:
            keys = [key for key in self._records if key[:2] == (tenant_id, thread_id)]
            for key in keys:
                del self._records[key]
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
                    key_version INTEGER
                )
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(attachments)")}
            if "nonce" not in columns:
                connection.execute("ALTER TABLE attachments ADD COLUMN nonce BLOB")
            if "key_version" not in columns:
                connection.execute("ALTER TABLE attachments ADD COLUMN key_version INTEGER")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_scope "
                "ON attachments (tenant_id, thread_id)"
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
    ) -> AttachmentMetadata:
        metadata = AttachmentMetadata(
            attachment_id=str(uuid4()),
            thread_id=thread_id,
            mime_type=mime_type,
            size_bytes=len(data),
            created_by=created_by,
        )
        created_at = metadata.created_at.isoformat()
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
            _enforce_limits(
                count=count,
                total_bytes=total_bytes,
                incoming_bytes=len(data),
                max_per_thread=max_per_thread,
                max_bytes_per_thread=max_bytes_per_thread,
            )
            connection.execute(
                """
                INSERT INTO attachments (
                    attachment_id, tenant_id, thread_id, mime_type,
                    size_bytes, created_by, created_at, data, nonce, key_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    incoming_bytes: int,
    max_per_thread: int | None,
    max_bytes_per_thread: int | None,
) -> None:
    if max_per_thread is not None and count >= max_per_thread:
        raise AttachmentLimitExceeded("count")
    if max_bytes_per_thread is not None and total_bytes + incoming_bytes > max_bytes_per_thread:
        raise AttachmentLimitExceeded("bytes")


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
