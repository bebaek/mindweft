from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Protocol

from fastapi import HTTPException

from app.models import AuditRecord, Message, Thread, ThreadContext, ThreadStatus, utc_now

THREAD_DB_PATH_ENV = "MINIGENT_THREAD_DB_PATH"


@dataclass(frozen=True)
class ThreadStoreSettings:
    db_path: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ThreadStoreSettings:
        lookup = os.environ if env is None else env
        db_path = lookup.get(THREAD_DB_PATH_ENV, "").strip()
        return cls(db_path=db_path or None)


def thread_store_settings_from_env() -> ThreadStoreSettings:
    return ThreadStoreSettings.from_env()


class ThreadStore(Protocol):
    def create_thread(
        self,
        tenant_id: str,
        *,
        skill_name: str | None = None,
        skill_names: list[str] | None = None,
        capability_profile: str | None = None,
        llm_profile: str | None = None,
    ) -> Thread: ...

    def delete_thread(self, tenant_id: str, thread_id: str) -> None: ...

    def prune_threads(
        self,
        tenant_id: str,
        *,
        updated_before: datetime,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
    ) -> int: ...

    def list_prunable_threads(
        self,
        tenant_id: str,
        *,
        updated_before: datetime,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
    ) -> list[Thread]: ...

    def append_audit_record(self, record: AuditRecord) -> AuditRecord: ...

    def list_audit_records(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AuditRecord]: ...

    def count_audit_records(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> int: ...

    def list_threads(
        self,
        tenant_id: str,
        *,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
        created_after: datetime | None = None,
        updated_after: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Thread]: ...

    def count_threads(
        self,
        tenant_id: str,
        *,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
        created_after: datetime | None = None,
        updated_after: datetime | None = None,
    ) -> int: ...

    def count_messages(self, tenant_id: str, thread_id: str) -> int: ...

    def list_messages(self, tenant_id: str, thread_id: str) -> list[Message]: ...

    def append_message(self, tenant_id: str, message: Message) -> Message: ...

    def set_thread_status(self, tenant_id: str, thread_id: str, status: ThreadStatus) -> Thread: ...

    def get_thread(self, tenant_id: str, thread_id: str) -> Thread: ...

    def get_thread_context(self, tenant_id: str, thread_id: str) -> ThreadContext: ...

    def update_thread_context(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        summary: str,
        summarized_message_count: int,
    ) -> ThreadContext: ...

    def compact_thread_messages(self, tenant_id: str, thread_id: str) -> ThreadContext: ...

    def start_run(self, tenant_id: str, thread_id: str) -> Thread: ...


class InMemoryThreadStore:
    def __init__(self) -> None:
        self._threads: dict[str, Thread] = {}
        self._contexts: dict[str, ThreadContext] = {}
        self._messages: dict[str, list[Message]] = {}
        self._audit_records: list[AuditRecord] = []
        self._lock = Lock()

    def create_thread(
        self,
        tenant_id: str,
        *,
        skill_name: str | None = None,
        skill_names: list[str] | None = None,
        capability_profile: str | None = None,
        llm_profile: str | None = None,
    ) -> Thread:
        with self._lock:
            normalized_skill_names = list(skill_names) if skill_names is not None else None
            thread = Thread(
                tenant_id=tenant_id,
                skill_name=skill_name,
                skill_names=normalized_skill_names,
                capability_profile=capability_profile,
                llm_profile=llm_profile,
            )
            self._threads[thread.thread_id] = thread
            self._contexts[thread.thread_id] = ThreadContext(thread_id=thread.thread_id)
            self._messages[thread.thread_id] = []
            return thread

    def delete_thread(self, tenant_id: str, thread_id: str) -> None:
        with self._lock:
            self._require_thread(tenant_id, thread_id)
            del self._threads[thread_id]
            del self._contexts[thread_id]
            del self._messages[thread_id]

    def prune_threads(
        self,
        tenant_id: str,
        *,
        updated_before: datetime,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
    ) -> int:
        with self._lock:
            threads = self._list_prunable_threads_unlocked(
                tenant_id,
                updated_before=updated_before,
                status=status,
                capability_profile=capability_profile,
                skill=skill,
            )
            for thread in threads:
                del self._threads[thread.thread_id]
                del self._contexts[thread.thread_id]
                del self._messages[thread.thread_id]
            return len(threads)

    def list_prunable_threads(
        self,
        tenant_id: str,
        *,
        updated_before: datetime,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
    ) -> list[Thread]:
        with self._lock:
            return [
                thread.model_copy(deep=True)
                for thread in self._list_prunable_threads_unlocked(
                    tenant_id,
                    updated_before=updated_before,
                    status=status,
                    capability_profile=capability_profile,
                    skill=skill,
                )
            ]

    def append_audit_record(self, record: AuditRecord) -> AuditRecord:
        with self._lock:
            self._audit_records.append(record)
            return record

    def list_audit_records(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AuditRecord]:
        with self._lock:
            records = [
                record.model_copy(deep=True)
                for record in self._audit_records
                if _audit_record_matches_filters(
                    record,
                    tenant_id,
                    action=action,
                    actor_user_id=actor_user_id,
                    created_after=created_after,
                    created_before=created_before,
                )
            ]
            records.sort(key=lambda record: record.created_at, reverse=True)
            return _paginate_audit_records(records, limit=limit, offset=offset)

    def count_audit_records(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> int:
        with self._lock:
            return sum(
                1
                for record in self._audit_records
                if _audit_record_matches_filters(
                    record,
                    tenant_id,
                    action=action,
                    actor_user_id=actor_user_id,
                    created_after=created_after,
                    created_before=created_before,
                )
            )

    def list_threads(
        self,
        tenant_id: str,
        *,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
        created_after: datetime | None = None,
        updated_after: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Thread]:
        with self._lock:
            threads = [
                thread.model_copy(deep=True)
                for thread in self._threads.values()
                if _thread_matches_filters(
                    thread,
                    tenant_id,
                    status=status,
                    capability_profile=capability_profile,
                    skill=skill,
                    created_after=created_after,
                    updated_after=updated_after,
                )
            ]
            sorted_threads = sorted(threads, key=lambda thread: thread.updated_at, reverse=True)
            return _paginate_threads(sorted_threads, limit=limit, offset=offset)

    def count_threads(
        self,
        tenant_id: str,
        *,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
        created_after: datetime | None = None,
        updated_after: datetime | None = None,
    ) -> int:
        with self._lock:
            return sum(
                1
                for thread in self._threads.values()
                if _thread_matches_filters(
                    thread,
                    tenant_id,
                    status=status,
                    capability_profile=capability_profile,
                    skill=skill,
                    created_after=created_after,
                    updated_after=updated_after,
                )
            )

    def count_messages(self, tenant_id: str, thread_id: str) -> int:
        with self._lock:
            self._require_thread(tenant_id, thread_id)
            return len(self._messages[thread_id])

    def list_messages(self, tenant_id: str, thread_id: str) -> list[Message]:
        with self._lock:
            self._require_thread(tenant_id, thread_id)
            return list(self._messages[thread_id])

    def append_message(self, tenant_id: str, message: Message) -> Message:
        with self._lock:
            thread = self._require_thread(tenant_id, message.thread_id)
            self._messages[message.thread_id].append(message)
            thread.updated_at = utc_now()
            return message

    def set_thread_status(self, tenant_id: str, thread_id: str, status: ThreadStatus) -> Thread:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            thread.status = status
            thread.updated_at = utc_now()
            return thread

    def get_thread(self, tenant_id: str, thread_id: str) -> Thread:
        with self._lock:
            return self._require_thread(tenant_id, thread_id)

    def get_thread_context(self, tenant_id: str, thread_id: str) -> ThreadContext:
        with self._lock:
            self._require_thread(tenant_id, thread_id)
            return self._contexts[thread_id].model_copy(deep=True)

    def update_thread_context(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        summary: str,
        summarized_message_count: int,
    ) -> ThreadContext:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            context = self._contexts[thread_id]
            context.summary = summary
            context.summarized_message_count = summarized_message_count
            context.updated_at = utc_now()
            thread.updated_at = utc_now()
            return context.model_copy(deep=True)

    def compact_thread_messages(self, tenant_id: str, thread_id: str) -> ThreadContext:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            context = self._contexts[thread_id]
            if context.summarized_message_count <= 0:
                return context.model_copy(deep=True)
            self._messages[thread_id] = self._messages[thread_id][
                context.summarized_message_count :
            ]
            context.summarized_message_count = 0
            context.updated_at = utc_now()
            thread.updated_at = utc_now()
            return context.model_copy(deep=True)

    def start_run(self, tenant_id: str, thread_id: str) -> Thread:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            if thread.status == ThreadStatus.RUNNING:
                raise HTTPException(
                    status_code=409, detail=f"Thread '{thread_id}' is already running"
                )
            thread.status = ThreadStatus.RUNNING
            thread.updated_at = utc_now()
            return thread

    def _require_thread(self, tenant_id: str, thread_id: str) -> Thread:
        thread = self._threads.get(thread_id)
        if thread is None or thread.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
        return thread

    def _list_prunable_threads_unlocked(
        self,
        tenant_id: str,
        *,
        updated_before: datetime,
        status: ThreadStatus | None,
        capability_profile: str | None,
        skill: str | None,
    ) -> list[Thread]:
        return [
            thread
            for thread in self._threads.values()
            if _thread_matches_filters(
                thread,
                tenant_id,
                status=status,
                capability_profile=capability_profile,
                skill=skill,
                created_after=None,
                updated_after=None,
            )
            and thread.updated_at < updated_before
        ]


class SQLiteThreadStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        if self._db_path.parent != Path(""):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    def create_thread(
        self,
        tenant_id: str,
        *,
        skill_name: str | None = None,
        skill_names: list[str] | None = None,
        capability_profile: str | None = None,
        llm_profile: str | None = None,
    ) -> Thread:
        with self._lock, self._connection() as conn:
            normalized_skill_names = list(skill_names) if skill_names is not None else None
            thread = Thread(
                tenant_id=tenant_id,
                skill_name=skill_name,
                skill_names=normalized_skill_names,
                capability_profile=capability_profile,
                llm_profile=llm_profile,
            )
            context = ThreadContext(thread_id=thread.thread_id)
            conn.execute(
                "INSERT INTO threads (thread_id, tenant_id, payload) VALUES (?, ?, ?)",
                (thread.thread_id, tenant_id, _dump_model(thread)),
            )
            conn.execute(
                "INSERT INTO thread_contexts (thread_id, payload) VALUES (?, ?)",
                (thread.thread_id, _dump_model(context)),
            )
            return thread

    def delete_thread(self, tenant_id: str, thread_id: str) -> None:
        with self._lock, self._connection() as conn:
            self._require_thread(conn, tenant_id, thread_id)
            conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))

    def prune_threads(
        self,
        tenant_id: str,
        *,
        updated_before: datetime,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
    ) -> int:
        with self._lock, self._connection() as conn:
            threads = self._load_prunable_threads(
                conn,
                tenant_id,
                updated_before=updated_before,
                status=status,
                capability_profile=capability_profile,
                skill=skill,
            )
            conn.executemany(
                "DELETE FROM threads WHERE thread_id = ?",
                [(thread.thread_id,) for thread in threads],
            )
            return len(threads)

    def list_prunable_threads(
        self,
        tenant_id: str,
        *,
        updated_before: datetime,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
    ) -> list[Thread]:
        with self._lock, self._connection() as conn:
            return self._load_prunable_threads(
                conn,
                tenant_id,
                updated_before=updated_before,
                status=status,
                capability_profile=capability_profile,
                skill=skill,
            )

    def append_audit_record(self, record: AuditRecord) -> AuditRecord:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_records (
                    audit_id, tenant_id, created_at, payload,
                    resource_type, resource_id, old_values_json, new_values_json, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.audit_id,
                    record.tenant_id,
                    record.created_at.isoformat(),
                    _dump_model(record),
                    record.resource_type,
                    record.resource_id,
                    _dump_optional_json(record.old_values),
                    _dump_optional_json(record.new_values),
                    _dump_optional_json(record.metadata),
                ),
            )
            return record

    def list_audit_records(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AuditRecord]:
        with self._lock, self._connection() as conn:
            records = self._load_matching_audit_records(
                conn,
                tenant_id,
                action=action,
                actor_user_id=actor_user_id,
                created_after=created_after,
                created_before=created_before,
            )
            return _paginate_audit_records(records, limit=limit, offset=offset)

    def count_audit_records(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> int:
        with self._lock, self._connection() as conn:
            return len(
                self._load_matching_audit_records(
                    conn,
                    tenant_id,
                    action=action,
                    actor_user_id=actor_user_id,
                    created_after=created_after,
                    created_before=created_before,
                )
            )

    def list_threads(
        self,
        tenant_id: str,
        *,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
        created_after: datetime | None = None,
        updated_after: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Thread]:
        with self._lock, self._connection() as conn:
            threads = self._load_matching_threads(
                conn,
                tenant_id,
                status=status,
                capability_profile=capability_profile,
                skill=skill,
                created_after=created_after,
                updated_after=updated_after,
            )
            sorted_threads = sorted(threads, key=lambda thread: thread.updated_at, reverse=True)
            return _paginate_threads(sorted_threads, limit=limit, offset=offset)

    def count_threads(
        self,
        tenant_id: str,
        *,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
        created_after: datetime | None = None,
        updated_after: datetime | None = None,
    ) -> int:
        with self._lock, self._connection() as conn:
            return len(
                self._load_matching_threads(
                    conn,
                    tenant_id,
                    status=status,
                    capability_profile=capability_profile,
                    skill=skill,
                    created_after=created_after,
                    updated_after=updated_after,
                )
            )

    def count_messages(self, tenant_id: str, thread_id: str) -> int:
        with self._lock, self._connection() as conn:
            self._require_thread(conn, tenant_id, thread_id)
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def list_messages(self, tenant_id: str, thread_id: str) -> list[Message]:
        with self._lock, self._connection() as conn:
            self._require_thread(conn, tenant_id, thread_id)
            rows = conn.execute(
                "SELECT payload FROM messages WHERE thread_id = ? ORDER BY position ASC",
                (thread_id,),
            ).fetchall()
            return [Message.model_validate(json.loads(row[0])) for row in rows]

    def append_message(self, tenant_id: str, message: Message) -> Message:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, message.thread_id)
            conn.execute(
                "INSERT INTO messages (thread_id, payload) VALUES (?, ?)",
                (message.thread_id, _dump_model(message)),
            )
            thread.updated_at = utc_now()
            self._save_thread(conn, thread)
            return message

    def set_thread_status(self, tenant_id: str, thread_id: str, status: ThreadStatus) -> Thread:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            thread.status = status
            thread.updated_at = utc_now()
            self._save_thread(conn, thread)
            return thread

    def get_thread(self, tenant_id: str, thread_id: str) -> Thread:
        with self._lock, self._connection() as conn:
            return self._require_thread(conn, tenant_id, thread_id)

    def get_thread_context(self, tenant_id: str, thread_id: str) -> ThreadContext:
        with self._lock, self._connection() as conn:
            self._require_thread(conn, tenant_id, thread_id)
            return self._require_context(conn, thread_id)

    def update_thread_context(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        summary: str,
        summarized_message_count: int,
    ) -> ThreadContext:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            context = self._require_context(conn, thread_id)
            context.summary = summary
            context.summarized_message_count = summarized_message_count
            context.updated_at = utc_now()
            thread.updated_at = utc_now()
            self._save_context(conn, context)
            self._save_thread(conn, thread)
            return context

    def compact_thread_messages(self, tenant_id: str, thread_id: str) -> ThreadContext:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            context = self._require_context(conn, thread_id)
            if context.summarized_message_count <= 0:
                return context
            rows = conn.execute(
                "SELECT position FROM messages WHERE thread_id = ? ORDER BY position ASC LIMIT ?",
                (thread_id, context.summarized_message_count),
            ).fetchall()
            if rows:
                max_deleted_position = rows[-1][0]
                conn.execute(
                    "DELETE FROM messages WHERE thread_id = ? AND position <= ?",
                    (thread_id, max_deleted_position),
                )
            context.summarized_message_count = 0
            context.updated_at = utc_now()
            thread.updated_at = utc_now()
            self._save_context(conn, context)
            self._save_thread(conn, thread)
            return context

    def start_run(self, tenant_id: str, thread_id: str) -> Thread:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            if thread.status == ThreadStatus.RUNNING:
                raise HTTPException(
                    status_code=409, detail=f"Thread '{thread_id}' is already running"
                )
            thread.status = ThreadStatus.RUNNING
            thread.updated_at = utc_now()
            self._save_thread(conn, thread)
            return thread

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_threads_tenant_id ON threads (tenant_id);
                CREATE TABLE IF NOT EXISTS thread_contexts (
                    thread_id TEXT PRIMARY KEY REFERENCES threads(thread_id) ON DELETE CASCADE,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_thread_position
                    ON messages (thread_id, position);
                CREATE TABLE IF NOT EXISTS audit_records (
                    audit_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    payload TEXT NOT NULL,
                    resource_type TEXT,
                    resource_id TEXT,
                    old_values_json TEXT,
                    new_values_json TEXT,
                    metadata_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_records_tenant_created
                    ON audit_records (tenant_id, created_at DESC);
                """
            )
            _ensure_audit_record_columns(conn)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _load_matching_threads(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        *,
        status: ThreadStatus | None,
        capability_profile: str | None,
        skill: str | None,
        created_after: datetime | None,
        updated_after: datetime | None,
    ) -> list[Thread]:
        rows = conn.execute(
            "SELECT payload FROM threads WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchall()
        return [
            thread
            for thread in (Thread.model_validate(json.loads(row[0])) for row in rows)
            if _thread_matches_filters(
                thread,
                tenant_id,
                status=status,
                capability_profile=capability_profile,
                skill=skill,
                created_after=created_after,
                updated_after=updated_after,
            )
        ]

    def _load_matching_audit_records(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        *,
        action: str | None,
        actor_user_id: str | None,
        created_after: datetime | None,
        created_before: datetime | None,
    ) -> list[AuditRecord]:
        rows = conn.execute(
            "SELECT payload FROM audit_records WHERE tenant_id = ? ORDER BY created_at DESC",
            (tenant_id,),
        ).fetchall()
        return [
            record
            for record in (AuditRecord.model_validate(json.loads(row[0])) for row in rows)
            if _audit_record_matches_filters(
                record,
                tenant_id,
                action=action,
                actor_user_id=actor_user_id,
                created_after=created_after,
                created_before=created_before,
            )
        ]

    def _load_prunable_threads(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        *,
        updated_before: datetime,
        status: ThreadStatus | None,
        capability_profile: str | None,
        skill: str | None,
    ) -> list[Thread]:
        threads = self._load_matching_threads(
            conn,
            tenant_id,
            status=status,
            capability_profile=capability_profile,
            skill=skill,
            created_after=None,
            updated_after=None,
        )
        return [thread for thread in threads if thread.updated_at < updated_before]

    def _require_thread(self, conn: sqlite3.Connection, tenant_id: str, thread_id: str) -> Thread:
        row = conn.execute(
            "SELECT payload FROM threads WHERE thread_id = ? AND tenant_id = ?",
            (thread_id, tenant_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
        return Thread.model_validate(json.loads(row[0]))

    def _require_context(self, conn: sqlite3.Connection, thread_id: str) -> ThreadContext:
        row = conn.execute(
            "SELECT payload FROM thread_contexts WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
        return ThreadContext.model_validate(json.loads(row[0]))

    def _save_thread(self, conn: sqlite3.Connection, thread: Thread) -> None:
        conn.execute(
            "UPDATE threads SET payload = ? WHERE thread_id = ?",
            (_dump_model(thread), thread.thread_id),
        )

    def _save_context(self, conn: sqlite3.Connection, context: ThreadContext) -> None:
        conn.execute(
            "UPDATE thread_contexts SET payload = ? WHERE thread_id = ?",
            (_dump_model(context), context.thread_id),
        )


def build_thread_store_from_env() -> ThreadStore:
    settings = thread_store_settings_from_env()
    if settings.db_path is not None:
        return SQLiteThreadStore(settings.db_path)
    return InMemoryThreadStore()


def _thread_matches_filters(
    thread: Thread,
    tenant_id: str,
    *,
    status: ThreadStatus | None,
    capability_profile: str | None,
    skill: str | None,
    created_after: datetime | None,
    updated_after: datetime | None,
) -> bool:
    if thread.tenant_id != tenant_id:
        return False
    if status is not None and thread.status != status:
        return False
    if capability_profile is not None and thread.capability_profile != capability_profile:
        return False
    if skill is not None and thread.skill_name != skill and skill not in (thread.skill_names or []):
        return False
    if created_after is not None and thread.created_at <= created_after:
        return False
    if updated_after is not None and thread.updated_at <= updated_after:
        return False
    return True


def _audit_record_matches_filters(
    record: AuditRecord,
    tenant_id: str,
    *,
    action: str | None,
    actor_user_id: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
) -> bool:
    if record.tenant_id != tenant_id:
        return False
    if action is not None and record.action != action:
        return False
    if actor_user_id is not None and record.actor_user_id != actor_user_id:
        return False
    if created_after is not None and record.created_at <= created_after:
        return False
    if created_before is not None and record.created_at >= created_before:
        return False
    return True


def _paginate_threads(threads: list[Thread], *, limit: int | None, offset: int) -> list[Thread]:
    start = max(offset, 0)
    if limit is None:
        return threads[start:]
    return threads[start : start + max(limit, 0)]


def _paginate_audit_records(
    records: list[AuditRecord], *, limit: int | None, offset: int
) -> list[AuditRecord]:
    start = max(offset, 0)
    if limit is None:
        return records[start:]
    return records[start : start + max(limit, 0)]


def _dump_optional_json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _ensure_audit_record_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(audit_records)").fetchall()
    columns = {str(row[1]) for row in rows}
    for column_name in [
        "resource_type",
        "resource_id",
        "old_values_json",
        "new_values_json",
        "metadata_json",
    ]:
        if column_name not in columns:
            conn.execute(f"ALTER TABLE audit_records ADD COLUMN {column_name} TEXT")


def _dump_model(model: AuditRecord | Message | Thread | ThreadContext) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
