from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Mapping, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

ATTACHMENT_DB_PATH_ENV = "MINIGENT_ATTACHMENT_DB_PATH"
ATTACHMENT_MAX_PER_THREAD_ENV = "MINIGENT_ATTACHMENT_MAX_PER_THREAD"
ATTACHMENT_MAX_BYTES_PER_THREAD_ENV = "MINIGENT_ATTACHMENT_MAX_BYTES_PER_THREAD"
DEFAULT_ATTACHMENT_MAX_PER_THREAD = 100
DEFAULT_ATTACHMENT_MAX_BYTES_PER_THREAD = 256 * 1024 * 1024


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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AttachmentStoreSettings:
        lookup = os.environ if env is None else env
        value = lookup.get(ATTACHMENT_DB_PATH_ENV, "").strip()
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
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = RLock()
        Path(self._db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
                    data BLOB NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_attachments_scope "
                "ON attachments (tenant_id, thread_id)"
            )

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
                    size_bytes, created_by, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata.attachment_id,
                    tenant_id,
                    thread_id,
                    metadata.mime_type,
                    metadata.size_bytes,
                    metadata.created_by,
                    metadata.created_at.isoformat(),
                    data,
                ),
            )
        return metadata

    def get(self, tenant_id: str, thread_id: str, attachment_id: str) -> AttachmentRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT mime_type, size_bytes, created_by, created_at, data
                FROM attachments
                WHERE tenant_id = ? AND thread_id = ? AND attachment_id = ?
                """,
                (tenant_id, thread_id, attachment_id),
            ).fetchone()
        if row is None:
            return None
        return AttachmentRecord(
            metadata=AttachmentMetadata(
                attachment_id=attachment_id,
                thread_id=thread_id,
                mime_type=str(row[0]),
                size_bytes=int(row[1]),
                created_by=str(row[2]) if row[2] is not None else None,
                created_at=datetime.fromisoformat(str(row[3])),
            ),
            data=bytes(row[4]),
        )

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
        return SQLiteAttachmentStore(settings.db_path)
    return InMemoryAttachmentStore()
