from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Protocol
from uuid import uuid4

from fastapi import HTTPException

from app.message_parts import remap_attachment_ids
from app.models import (
    AuditRecord,
    Message,
    MessageRole,
    Thread,
    ThreadContext,
    ThreadStatus,
    utc_now,
)
from app.thread_titles import generate_thread_title
from mindweft_config.unified_config import preferred_mindweft_env

DEFAULT_RUN_LEASE_SECONDS = 30.0
DEFAULT_ARCHIVE_IMPORT_LEASE_SECONDS = 3600.0
_CURRENT_RUN: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "minigent_current_thread_run", default=None
)


@dataclass(frozen=True)
class ThreadRun:
    tenant_id: str
    thread_id: str
    run_id: str
    owner_instance_id: str
    lease_expires_at: float
    cancellation_requested: bool = False
    peer_name: str | None = None
    peer_base_url: str | None = None
    peer_task_id: str | None = None


@dataclass(frozen=True)
class PeerTaskCancellation:
    cancellation_id: str
    peer_name: str
    peer_base_url: str
    task_id: str
    attempts: int


@dataclass(frozen=True)
class _QueuedPeerTaskCancellation:
    cancellation: PeerTaskCancellation
    claim_owner: str | None = None
    claim_expires_at: float | None = None
    next_attempt_at: float = 0.0


@dataclass(frozen=True)
class ArchiveImportClaim:
    claim_token: str | None
    response_payload: dict[str, Any] | None = None

    @property
    def completed(self) -> bool:
        return self.response_payload is not None


@dataclass(frozen=True)
class _ArchiveImportRecord:
    request_digest: str
    claim_token: str
    lease_expires_at: float
    thread_id: str | None = None
    response_payload: dict[str, Any] | None = None


class ArchiveImportConflictError(ValueError):
    pass


class ArchiveImportInProgressError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThreadStoreSettings:
    db_path: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ThreadStoreSettings:
        db_path = (preferred_mindweft_env("THREAD_DB_PATH", env) or "").strip()
        return cls(db_path=db_path or None)


def thread_store_settings_from_env() -> ThreadStoreSettings:
    return ThreadStoreSettings.from_env()


class ThreadStore(Protocol):
    def create_thread(
        self,
        tenant_id: str,
        *,
        execution_user_id: str | None = None,
        skill_name: str | None = None,
        skill_names: list[str] | None = None,
        capability_profile: str | None = None,
        llm_profile: str | None = None,
        import_source_archive_id: str | None = None,
        import_source_thread_id: str | None = None,
        imported_at: datetime | None = None,
    ) -> Thread: ...

    def delete_thread(self, tenant_id: str, thread_id: str) -> None: ...

    def lookup_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        request_digest: str,
    ) -> ArchiveImportClaim | None: ...

    def begin_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        request_digest: str,
        *,
        lease_seconds: float = DEFAULT_ARCHIVE_IMPORT_LEASE_SECONDS,
    ) -> ArchiveImportClaim: ...

    def complete_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        claim_token: str,
        *,
        thread_id: str,
        response_payload: Mapping[str, Any],
    ) -> None: ...

    def abandon_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        claim_token: str,
    ) -> None: ...

    def fork_thread(
        self,
        tenant_id: str,
        source_thread_id: str,
        *,
        at_message_id: str,
        child_thread_id: str | None = None,
        attachment_id_map: Mapping[str, str] | None = None,
    ) -> Thread: ...

    def fork_compacted_thread(
        self,
        tenant_id: str,
        source_thread_id: str,
        *,
        fork_message_id: str,
        compacted_through_message_id: str,
        summary: str,
        child_thread_id: str | None = None,
        attachment_id_map: Mapping[str, str] | None = None,
    ) -> Thread: ...

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
        q: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
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
        q: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
        created_after: datetime | None = None,
        updated_after: datetime | None = None,
    ) -> int: ...

    def count_messages(self, tenant_id: str, thread_id: str) -> int: ...

    def list_messages(self, tenant_id: str, thread_id: str) -> list[Message]: ...

    def search_messages(
        self,
        tenant_id: str,
        *,
        query: str,
        archived: bool = False,
    ) -> list[Message]: ...

    def append_message(self, tenant_id: str, message: Message) -> Message: ...

    def set_thread_title(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        title: str,
        source: str,
    ) -> Thread: ...

    def set_semantic_thread_title(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        title: str,
    ) -> Thread: ...

    def set_thread_status(self, tenant_id: str, thread_id: str, status: ThreadStatus) -> Thread: ...

    def set_thread_organization(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> Thread: ...

    def restore_thread_timestamps(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        created_at: datetime,
        updated_at: datetime,
    ) -> Thread: ...

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

    def start_run(self, tenant_id: str, thread_id: str) -> Thread: ...

    def owned_run_id(self, tenant_id: str, thread_id: str) -> str | None: ...

    def heartbeat_run(
        self, tenant_id: str, thread_id: str, *, run_id: str, lease_seconds: float
    ) -> bool: ...

    def run_cancellation_requested(
        self, tenant_id: str, thread_id: str, *, run_id: str
    ) -> bool: ...

    def request_run_cancellation(self, tenant_id: str, thread_id: str) -> bool: ...

    def attach_peer_task(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        peer_name: str,
        peer_base_url: str,
        task_id: str,
    ) -> bool: ...

    def enqueue_owned_peer_task_cancellation(self, tenant_id: str, thread_id: str) -> bool: ...

    def claim_peer_task_cancellations(
        self, *, lease_seconds: float, limit: int
    ) -> list[PeerTaskCancellation]: ...

    def complete_peer_task_cancellation(self, cancellation_id: str) -> bool: ...

    def release_peer_task_cancellation(
        self, cancellation_id: str, *, retry_delay_seconds: float
    ) -> bool: ...

    def recover_stale_runs(self) -> int: ...


class InMemoryThreadStore:
    def __init__(self) -> None:
        self._threads: dict[str, Thread] = {}
        self._contexts: dict[str, ThreadContext] = {}
        self._messages: dict[str, list[Message]] = {}
        self._audit_records: list[AuditRecord] = []
        self._lock = Lock()
        self._instance_id = uuid4().hex
        self._runs: dict[tuple[str, str], ThreadRun] = {}
        self._peer_task_cancellations: dict[str, _QueuedPeerTaskCancellation] = {}
        self._archive_imports: dict[tuple[str, str], _ArchiveImportRecord] = {}

    def create_thread(
        self,
        tenant_id: str,
        *,
        execution_user_id: str | None = None,
        skill_name: str | None = None,
        skill_names: list[str] | None = None,
        capability_profile: str | None = None,
        llm_profile: str | None = None,
        import_source_archive_id: str | None = None,
        import_source_thread_id: str | None = None,
        imported_at: datetime | None = None,
    ) -> Thread:
        with self._lock:
            normalized_skill_names = list(skill_names) if skill_names is not None else None
            thread = Thread(
                tenant_id=tenant_id,
                execution_user_id=execution_user_id,
                skill_name=skill_name,
                skill_names=normalized_skill_names,
                capability_profile=capability_profile,
                llm_profile=llm_profile,
                import_source_archive_id=import_source_archive_id,
                import_source_thread_id=import_source_thread_id,
                imported_at=imported_at,
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
            self._archive_imports = {
                key: record
                for key, record in self._archive_imports.items()
                if record.thread_id != thread_id
            }

    def lookup_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        request_digest: str,
    ) -> ArchiveImportClaim | None:
        with self._lock:
            record = self._archive_imports.get((tenant_id, archive_id))
            if record is None:
                return None
            if record.request_digest != request_digest:
                raise ArchiveImportConflictError(
                    "archive_id was already used with different archive content or import options"
                )
            if record.response_payload is not None:
                return ArchiveImportClaim(
                    claim_token=None,
                    response_payload=dict(record.response_payload),
                )
            if record.lease_expires_at > time.time():
                raise ArchiveImportInProgressError("archive import is already in progress")
            return None

    def begin_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        request_digest: str,
        *,
        lease_seconds: float = DEFAULT_ARCHIVE_IMPORT_LEASE_SECONDS,
    ) -> ArchiveImportClaim:
        with self._lock:
            key = (tenant_id, archive_id)
            now = time.time()
            record = self._archive_imports.get(key)
            if record is not None:
                if record.request_digest != request_digest:
                    raise ArchiveImportConflictError(
                        "archive_id was already used with different archive content or import options"
                    )
                if record.response_payload is not None:
                    return ArchiveImportClaim(
                        claim_token=None,
                        response_payload=dict(record.response_payload),
                    )
                if record.lease_expires_at > now:
                    raise ArchiveImportInProgressError("archive import is already in progress")
            claim_token = uuid4().hex
            self._archive_imports[key] = _ArchiveImportRecord(
                request_digest=request_digest,
                claim_token=claim_token,
                lease_expires_at=now + lease_seconds,
            )
            return ArchiveImportClaim(claim_token=claim_token)

    def complete_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        claim_token: str,
        *,
        thread_id: str,
        response_payload: Mapping[str, Any],
    ) -> None:
        with self._lock:
            key = (tenant_id, archive_id)
            record = self._archive_imports.get(key)
            if (
                record is None
                or record.claim_token != claim_token
                or record.response_payload is not None
            ):
                raise RuntimeError("archive import claim is no longer active")
            self._require_thread(tenant_id, thread_id)
            self._archive_imports[key] = replace(
                record,
                thread_id=thread_id,
                response_payload=dict(response_payload),
            )

    def abandon_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        claim_token: str,
    ) -> None:
        with self._lock:
            key = (tenant_id, archive_id)
            record = self._archive_imports.get(key)
            if (
                record is not None
                and record.claim_token == claim_token
                and record.response_payload is None
            ):
                del self._archive_imports[key]

    def fork_thread(
        self,
        tenant_id: str,
        source_thread_id: str,
        *,
        at_message_id: str,
        child_thread_id: str | None = None,
        attachment_id_map: Mapping[str, str] | None = None,
    ) -> Thread:
        with self._lock:
            source = self._require_thread(tenant_id, source_thread_id)
            source_messages = self._messages[source_thread_id]
            prefix = _fork_message_prefix(source_messages, at_message_id)
            resolved_child_id = child_thread_id or str(uuid4())
            if resolved_child_id in self._threads:
                raise HTTPException(status_code=409, detail="Fork thread ID already exists")
            child = _forked_thread(source, resolved_child_id, at_message_id)
            source_context = self._contexts[source_thread_id]
            self._threads[resolved_child_id] = child
            self._contexts[resolved_child_id] = ThreadContext(
                thread_id=resolved_child_id,
                summary=source_context.summary,
            )
            self._messages[resolved_child_id] = _copy_fork_messages(
                prefix,
                resolved_child_id,
                attachment_id_map=attachment_id_map,
            )
            return child.model_copy(deep=True)

    def fork_compacted_thread(
        self,
        tenant_id: str,
        source_thread_id: str,
        *,
        fork_message_id: str,
        compacted_through_message_id: str,
        summary: str,
        child_thread_id: str | None = None,
        attachment_id_map: Mapping[str, str] | None = None,
    ) -> Thread:
        with self._lock:
            source = self._require_thread(tenant_id, source_thread_id)
            if source.status == ThreadStatus.RUNNING:
                raise HTTPException(status_code=409, detail="Cannot compact a running thread")
            retained = _compacted_fork_suffix(
                self._messages[source_thread_id],
                fork_message_id,
                compacted_through_message_id,
            )
            resolved_child_id = child_thread_id or str(uuid4())
            if resolved_child_id in self._threads:
                raise HTTPException(status_code=409, detail="Fork thread ID already exists")
            child = _forked_thread(
                source,
                resolved_child_id,
                fork_message_id,
                compacted_through_message_id=compacted_through_message_id,
            )
            self._threads[resolved_child_id] = child
            self._contexts[resolved_child_id] = ThreadContext(
                thread_id=resolved_child_id,
                summary=summary,
            )
            self._messages[resolved_child_id] = _copy_fork_messages(
                retained,
                resolved_child_id,
                attachment_id_map=attachment_id_map,
            )
            return child.model_copy(deep=True)

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
        q: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
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
                    q=q,
                    archived=archived,
                    pinned=pinned,
                    created_after=created_after,
                    updated_after=updated_after,
                )
            ]
            sorted_threads = sorted(
                threads,
                key=lambda thread: (
                    thread.pinned_at is not None,
                    thread.pinned_at or thread.updated_at,
                    thread.updated_at,
                ),
                reverse=True,
            )
            return _paginate_threads(sorted_threads, limit=limit, offset=offset)

    def count_threads(
        self,
        tenant_id: str,
        *,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
        q: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
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
                    q=q,
                    archived=archived,
                    pinned=pinned,
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

    def search_messages(
        self,
        tenant_id: str,
        *,
        query: str,
        archived: bool = False,
    ) -> list[Message]:
        terms = _search_terms(query)
        if not terms:
            return []
        with self._lock:
            eligible_thread_ids = {
                thread.thread_id
                for thread in self._threads.values()
                if thread.tenant_id == tenant_id and (thread.archived_at is not None) == archived
            }
            return [
                message.model_copy(deep=True)
                for thread_id in eligible_thread_ids
                for message in self._messages[thread_id]
                if _message_matches_search(message, terms)
            ]

    def append_message(self, tenant_id: str, message: Message) -> Message:
        with self._lock:
            self._require_current_run(tenant_id, message.thread_id)
            thread = self._require_thread(tenant_id, message.thread_id)
            self._messages[message.thread_id].append(message)
            now = utc_now()
            thread.updated_at = now
            if (
                message.role == MessageRole.USER
                and thread.title is None
                and message.content.strip()
            ):
                thread.title = generate_thread_title(message.content)
                thread.title_source = "generated"
                thread.title_updated_at = now
            return message

    def set_thread_title(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        title: str,
        source: str,
    ) -> Thread:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            thread.title = title
            thread.title_source = "manual" if source == "manual" else "generated"
            thread.title_updated_at = utc_now()
            return thread.model_copy(deep=True)

    def set_semantic_thread_title(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        title: str,
    ) -> Thread:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            if thread.title_source in {"manual", "semantic"}:
                return thread.model_copy(deep=True)
            thread.title = title
            thread.title_source = "semantic"
            thread.title_updated_at = utc_now()
            return thread.model_copy(deep=True)

    def set_thread_status(self, tenant_id: str, thread_id: str, status: ThreadStatus) -> Thread:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            current = _CURRENT_RUN.get()
            run = self._runs.get((tenant_id, thread_id))
            if current is not None and current[:2] == (tenant_id, thread_id):
                if run is None or run.run_id != current[2]:
                    return thread
                if status != ThreadStatus.RUNNING:
                    self._runs.pop((tenant_id, thread_id), None)
                    _CURRENT_RUN.set(None)
            thread.status = status
            thread.updated_at = utc_now()
            return thread

    def set_thread_organization(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> Thread:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            now = utc_now()
            if pinned is not None and pinned != (thread.pinned_at is not None):
                thread.pinned_at = now if pinned else None
            if archived is not None and archived != (thread.archived_at is not None):
                thread.archived_at = now if archived else None
            return thread.model_copy(deep=True)

    def restore_thread_timestamps(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        created_at: datetime,
        updated_at: datetime,
    ) -> Thread:
        with self._lock:
            thread = self._require_thread(tenant_id, thread_id)
            thread.created_at = created_at
            thread.updated_at = updated_at
            return thread.model_copy(deep=True)

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
            self._require_current_run(tenant_id, thread_id)
            thread = self._require_thread(tenant_id, thread_id)
            context = self._contexts[thread_id]
            context.summary = summary
            context.summarized_message_count = summarized_message_count
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
            run_id = uuid4().hex
            now = time.time()
            self._runs[(tenant_id, thread_id)] = ThreadRun(
                tenant_id=tenant_id,
                thread_id=thread_id,
                run_id=run_id,
                owner_instance_id=self._instance_id,
                lease_expires_at=now + DEFAULT_RUN_LEASE_SECONDS,
            )
            _CURRENT_RUN.set((tenant_id, thread_id, run_id))
            thread.status = ThreadStatus.RUNNING
            thread.updated_at = utc_now()
            return thread

    def owned_run_id(self, tenant_id: str, thread_id: str) -> str | None:
        with self._lock:
            run = self._runs.get((tenant_id, thread_id))
            if run is None or run.owner_instance_id != self._instance_id:
                return None
            return run.run_id

    def heartbeat_run(
        self, tenant_id: str, thread_id: str, *, run_id: str, lease_seconds: float
    ) -> bool:
        with self._lock:
            key = (tenant_id, thread_id)
            run = self._runs.get(key)
            now = time.time()
            if (
                run is None
                or run.run_id != run_id
                or run.owner_instance_id != self._instance_id
                or run.lease_expires_at <= now
            ):
                return False
            self._runs[key] = replace(run, lease_expires_at=now + lease_seconds)
            return True

    def run_cancellation_requested(self, tenant_id: str, thread_id: str, *, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get((tenant_id, thread_id))
            return bool(
                run is not None
                and run.run_id == run_id
                and run.owner_instance_id == self._instance_id
                and run.cancellation_requested
            )

    def request_run_cancellation(self, tenant_id: str, thread_id: str) -> bool:
        with self._lock:
            key = (tenant_id, thread_id)
            run = self._runs.get(key)
            if run is None:
                return False
            self._runs[key] = replace(run, cancellation_requested=True)
            return True

    def attach_peer_task(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        peer_name: str,
        peer_base_url: str,
        task_id: str,
    ) -> bool:
        with self._lock:
            current = _CURRENT_RUN.get()
            run = self._runs.get((tenant_id, thread_id))
            if (
                current is None
                or current[:2] != (tenant_id, thread_id)
                or run is None
                or run.run_id != current[2]
                or run.owner_instance_id != self._instance_id
                or run.lease_expires_at <= time.time()
            ):
                return False
            self._runs[(tenant_id, thread_id)] = replace(
                run,
                peer_name=peer_name,
                peer_base_url=peer_base_url,
                peer_task_id=task_id,
            )
            return True

    def enqueue_owned_peer_task_cancellation(self, tenant_id: str, thread_id: str) -> bool:
        with self._lock:
            current = _CURRENT_RUN.get()
            run = self._runs.get((tenant_id, thread_id))
            if (
                current is None
                or current[:2] != (tenant_id, thread_id)
                or run is None
                or run.run_id != current[2]
                or run.owner_instance_id != self._instance_id
                or run.peer_name is None
                or run.peer_base_url is None
                or run.peer_task_id is None
            ):
                return False
            self._peer_task_cancellations.setdefault(
                run.run_id,
                _QueuedPeerTaskCancellation(
                    cancellation=PeerTaskCancellation(
                        cancellation_id=run.run_id,
                        peer_name=run.peer_name,
                        peer_base_url=run.peer_base_url,
                        task_id=run.peer_task_id,
                        attempts=0,
                    ),
                    next_attempt_at=time.time(),
                ),
            )
            return True

    def claim_peer_task_cancellations(
        self, *, lease_seconds: float, limit: int
    ) -> list[PeerTaskCancellation]:
        now = time.time()
        claimed: list[PeerTaskCancellation] = []
        with self._lock:
            for cancellation_id in sorted(self._peer_task_cancellations):
                queued = self._peer_task_cancellations[cancellation_id]
                if queued.next_attempt_at > now or (
                    queued.claim_owner is not None
                    and queued.claim_expires_at is not None
                    and queued.claim_expires_at > now
                ):
                    continue
                cancellation = replace(
                    queued.cancellation, attempts=queued.cancellation.attempts + 1
                )
                self._peer_task_cancellations[cancellation_id] = replace(
                    queued,
                    cancellation=cancellation,
                    claim_owner=self._instance_id,
                    claim_expires_at=now + lease_seconds,
                )
                claimed.append(cancellation)
                if len(claimed) >= limit:
                    break
        return claimed

    def complete_peer_task_cancellation(self, cancellation_id: str) -> bool:
        with self._lock:
            queued = self._peer_task_cancellations.get(cancellation_id)
            if queued is None or queued.claim_owner != self._instance_id:
                return False
            self._peer_task_cancellations.pop(cancellation_id, None)
            return True

    def release_peer_task_cancellation(
        self, cancellation_id: str, *, retry_delay_seconds: float
    ) -> bool:
        with self._lock:
            queued = self._peer_task_cancellations.get(cancellation_id)
            if queued is None or queued.claim_owner != self._instance_id:
                return False
            self._peer_task_cancellations[cancellation_id] = replace(
                queued,
                claim_owner=None,
                claim_expires_at=None,
                next_attempt_at=time.time() + retry_delay_seconds,
            )
            return True

    def recover_stale_runs(self) -> int:
        with self._lock:
            now = time.time()
            stale = [key for key, run in self._runs.items() if run.lease_expires_at <= now]
            for tenant_id, thread_id in stale:
                run = self._runs.pop((tenant_id, thread_id), None)
                if (
                    run is not None
                    and run.peer_name is not None
                    and run.peer_base_url is not None
                    and run.peer_task_id is not None
                ):
                    self._peer_task_cancellations.setdefault(
                        run.run_id,
                        _QueuedPeerTaskCancellation(
                            cancellation=PeerTaskCancellation(
                                cancellation_id=run.run_id,
                                peer_name=run.peer_name,
                                peer_base_url=run.peer_base_url,
                                task_id=run.peer_task_id,
                                attempts=0,
                            ),
                            next_attempt_at=now,
                        ),
                    )
                thread = self._require_thread(tenant_id, thread_id)
                if thread.status == ThreadStatus.RUNNING:
                    thread.status = ThreadStatus.ERROR
                    thread.updated_at = utc_now()
            return len(stale)

    def _require_current_run(self, tenant_id: str, thread_id: str) -> None:
        current = _CURRENT_RUN.get()
        if current is None or current[:2] != (tenant_id, thread_id):
            return
        run = self._runs.get((tenant_id, thread_id))
        if run is None or run.run_id != current[2] or run.lease_expires_at <= time.time():
            raise HTTPException(status_code=409, detail="Thread run lease was lost")

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
        self._instance_id = uuid4().hex
        self._fts_available = False
        self._initialize()

    def create_thread(
        self,
        tenant_id: str,
        *,
        execution_user_id: str | None = None,
        skill_name: str | None = None,
        skill_names: list[str] | None = None,
        capability_profile: str | None = None,
        llm_profile: str | None = None,
        import_source_archive_id: str | None = None,
        import_source_thread_id: str | None = None,
        imported_at: datetime | None = None,
    ) -> Thread:
        with self._lock, self._connection() as conn:
            normalized_skill_names = list(skill_names) if skill_names is not None else None
            thread = Thread(
                tenant_id=tenant_id,
                execution_user_id=execution_user_id,
                skill_name=skill_name,
                skill_names=normalized_skill_names,
                capability_profile=capability_profile,
                llm_profile=llm_profile,
                import_source_archive_id=import_source_archive_id,
                import_source_thread_id=import_source_thread_id,
                imported_at=imported_at,
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
            if self._fts_available:
                conn.execute("DELETE FROM message_search WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))

    def lookup_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        request_digest: str,
    ) -> ArchiveImportClaim | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT request_digest, lease_expires_at, response_payload
                FROM archive_imports
                WHERE tenant_id = ? AND archive_id = ?
                """,
                (tenant_id, archive_id),
            ).fetchone()
            if row is None:
                return None
            stored_digest, lease_expires_at, response_payload = row
            if str(stored_digest) != request_digest:
                raise ArchiveImportConflictError(
                    "archive_id was already used with different archive content or import options"
                )
            if response_payload is not None:
                parsed_payload = json.loads(str(response_payload))
                if not isinstance(parsed_payload, dict):
                    raise RuntimeError("stored archive import response is invalid")
                return ArchiveImportClaim(
                    claim_token=None,
                    response_payload=parsed_payload,
                )
            if float(lease_expires_at) > time.time():
                raise ArchiveImportInProgressError("archive import is already in progress")
            return None

    def begin_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        request_digest: str,
        *,
        lease_seconds: float = DEFAULT_ARCHIVE_IMPORT_LEASE_SECONDS,
    ) -> ArchiveImportClaim:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = conn.execute(
                """
                SELECT request_digest, claim_token, lease_expires_at, response_payload
                FROM archive_imports
                WHERE tenant_id = ? AND archive_id = ?
                """,
                (tenant_id, archive_id),
            ).fetchone()
            if row is not None:
                stored_digest, _stored_token, lease_expires_at, response_payload = row
                if str(stored_digest) != request_digest:
                    raise ArchiveImportConflictError(
                        "archive_id was already used with different archive content or import options"
                    )
                if response_payload is not None:
                    parsed_payload = json.loads(str(response_payload))
                    if not isinstance(parsed_payload, dict):
                        raise RuntimeError("stored archive import response is invalid")
                    return ArchiveImportClaim(
                        claim_token=None,
                        response_payload=parsed_payload,
                    )
                if float(lease_expires_at) > now:
                    raise ArchiveImportInProgressError("archive import is already in progress")
            claim_token = uuid4().hex
            conn.execute(
                """
                INSERT INTO archive_imports (
                    tenant_id,
                    archive_id,
                    request_digest,
                    claim_token,
                    lease_expires_at,
                    thread_id,
                    response_payload
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT (tenant_id, archive_id) DO UPDATE SET
                    request_digest = excluded.request_digest,
                    claim_token = excluded.claim_token,
                    lease_expires_at = excluded.lease_expires_at,
                    thread_id = NULL,
                    response_payload = NULL
                """,
                (tenant_id, archive_id, request_digest, claim_token, now + lease_seconds),
            )
            return ArchiveImportClaim(claim_token=claim_token)

    def complete_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        claim_token: str,
        *,
        thread_id: str,
        response_payload: Mapping[str, Any],
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_thread(conn, tenant_id, thread_id)
            cursor = conn.execute(
                """
                UPDATE archive_imports
                SET thread_id = ?, response_payload = ?
                WHERE tenant_id = ?
                  AND archive_id = ?
                  AND claim_token = ?
                  AND response_payload IS NULL
                """,
                (
                    thread_id,
                    json.dumps(dict(response_payload), separators=(",", ":"), sort_keys=True),
                    tenant_id,
                    archive_id,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("archive import claim is no longer active")

    def abandon_archive_import(
        self,
        tenant_id: str,
        archive_id: str,
        claim_token: str,
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                DELETE FROM archive_imports
                WHERE tenant_id = ?
                  AND archive_id = ?
                  AND claim_token = ?
                  AND response_payload IS NULL
                """,
                (tenant_id, archive_id, claim_token),
            )

    def fork_thread(
        self,
        tenant_id: str,
        source_thread_id: str,
        *,
        at_message_id: str,
        child_thread_id: str | None = None,
        attachment_id_map: Mapping[str, str] | None = None,
    ) -> Thread:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._require_thread(conn, tenant_id, source_thread_id)
            if source.status == ThreadStatus.RUNNING:
                raise HTTPException(status_code=409, detail="Cannot compact a running thread")
            rows = conn.execute(
                "SELECT payload FROM messages WHERE thread_id = ? ORDER BY position ASC",
                (source_thread_id,),
            ).fetchall()
            source_messages = [Message.model_validate(json.loads(row[0])) for row in rows]
            prefix = _fork_message_prefix(source_messages, at_message_id)
            resolved_child_id = child_thread_id or str(uuid4())
            if (
                conn.execute(
                    "SELECT 1 FROM threads WHERE thread_id = ?", (resolved_child_id,)
                ).fetchone()
                is not None
            ):
                raise HTTPException(status_code=409, detail="Fork thread ID already exists")
            child = _forked_thread(source, resolved_child_id, at_message_id)
            source_context = self._require_context(conn, source_thread_id)
            child_context = ThreadContext(
                thread_id=resolved_child_id,
                summary=source_context.summary,
            )
            conn.execute(
                "INSERT INTO threads (thread_id, tenant_id, payload) VALUES (?, ?, ?)",
                (resolved_child_id, tenant_id, _dump_model(child)),
            )
            conn.execute(
                "INSERT INTO thread_contexts (thread_id, payload) VALUES (?, ?)",
                (resolved_child_id, _dump_model(child_context)),
            )
            copied_messages = _copy_fork_messages(
                prefix,
                resolved_child_id,
                attachment_id_map=attachment_id_map,
            )
            self._insert_messages(conn, tenant_id, resolved_child_id, copied_messages)
            return child

    def fork_compacted_thread(
        self,
        tenant_id: str,
        source_thread_id: str,
        *,
        fork_message_id: str,
        compacted_through_message_id: str,
        summary: str,
        child_thread_id: str | None = None,
        attachment_id_map: Mapping[str, str] | None = None,
    ) -> Thread:
        with self._lock, self._connection() as conn:
            source = self._require_thread(conn, tenant_id, source_thread_id)
            rows = conn.execute(
                "SELECT payload FROM messages WHERE thread_id = ? ORDER BY position ASC",
                (source_thread_id,),
            ).fetchall()
            source_messages = [Message.model_validate(json.loads(row[0])) for row in rows]
            retained = _compacted_fork_suffix(
                source_messages,
                fork_message_id,
                compacted_through_message_id,
            )
            resolved_child_id = child_thread_id or str(uuid4())
            if (
                conn.execute(
                    "SELECT 1 FROM threads WHERE thread_id = ?", (resolved_child_id,)
                ).fetchone()
                is not None
            ):
                raise HTTPException(status_code=409, detail="Fork thread ID already exists")
            child = _forked_thread(
                source,
                resolved_child_id,
                fork_message_id,
                compacted_through_message_id=compacted_through_message_id,
            )
            child_context = ThreadContext(thread_id=resolved_child_id, summary=summary)
            conn.execute(
                "INSERT INTO threads (thread_id, tenant_id, payload) VALUES (?, ?, ?)",
                (resolved_child_id, tenant_id, _dump_model(child)),
            )
            conn.execute(
                "INSERT INTO thread_contexts (thread_id, payload) VALUES (?, ?)",
                (resolved_child_id, _dump_model(child_context)),
            )
            copied_messages = _copy_fork_messages(
                retained,
                resolved_child_id,
                attachment_id_map=attachment_id_map,
            )
            self._insert_messages(conn, tenant_id, resolved_child_id, copied_messages)
            return child

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
            if self._fts_available:
                conn.executemany(
                    "DELETE FROM message_search WHERE thread_id = ?",
                    [(thread.thread_id,) for thread in threads],
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
        q: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
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
                q=q,
                archived=archived,
                pinned=pinned,
                created_after=created_after,
                updated_after=updated_after,
            )
            sorted_threads = sorted(
                threads,
                key=lambda thread: (
                    thread.pinned_at is not None,
                    thread.pinned_at or thread.updated_at,
                    thread.updated_at,
                ),
                reverse=True,
            )
            return _paginate_threads(sorted_threads, limit=limit, offset=offset)

    def count_threads(
        self,
        tenant_id: str,
        *,
        status: ThreadStatus | None = None,
        capability_profile: str | None = None,
        skill: str | None = None,
        q: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
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
                    q=q,
                    archived=archived,
                    pinned=pinned,
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

    def search_messages(
        self,
        tenant_id: str,
        *,
        query: str,
        archived: bool = False,
    ) -> list[Message]:
        terms = _search_terms(query)
        if not terms:
            return []
        with self._lock, self._connection() as conn:
            eligible_thread_ids = {
                thread.thread_id
                for thread in self._load_matching_threads(
                    conn,
                    tenant_id,
                    status=None,
                    capability_profile=None,
                    skill=None,
                    created_after=None,
                    updated_after=None,
                    archived=archived,
                )
            }
            if not eligible_thread_ids:
                return []
            if self._fts_available:
                match_query = " AND ".join(f'"{term}"*' for term in terms)
                rows = conn.execute(
                    """
                    SELECT messages.payload
                    FROM message_search
                    JOIN messages ON messages.position = CAST(message_search.position AS INTEGER)
                    WHERE message_search MATCH ? AND message_search.tenant_id = ?
                    ORDER BY bm25(message_search), messages.position
                    """,
                    (match_query, tenant_id),
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in eligible_thread_ids)
                rows = conn.execute(
                    f"SELECT payload FROM messages WHERE thread_id IN ({placeholders}) ORDER BY position",
                    tuple(eligible_thread_ids),
                ).fetchall()
            return [
                message
                for row in rows
                if (message := Message.model_validate(json.loads(row[0]))).thread_id
                in eligible_thread_ids
                and _message_matches_search(message, terms)
            ]

    def append_message(self, tenant_id: str, message: Message) -> Message:
        with self._lock, self._connection() as conn:
            self._require_current_run(conn, tenant_id, message.thread_id)
            thread = self._require_thread(conn, tenant_id, message.thread_id)
            cursor = conn.execute(
                "INSERT INTO messages (thread_id, payload) VALUES (?, ?)",
                (message.thread_id, _dump_model(message)),
            )
            self._index_message(conn, tenant_id, _last_insert_position(cursor), message)
            now = utc_now()
            thread.updated_at = now
            if (
                message.role == MessageRole.USER
                and thread.title is None
                and message.content.strip()
            ):
                thread.title = generate_thread_title(message.content)
                thread.title_source = "generated"
                thread.title_updated_at = now
            self._save_thread(conn, thread)
            return message

    def set_thread_title(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        title: str,
        source: str,
    ) -> Thread:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            thread.title = title
            thread.title_source = "manual" if source == "manual" else "generated"
            thread.title_updated_at = utc_now()
            self._save_thread(conn, thread)
            return thread

    def set_semantic_thread_title(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        title: str,
    ) -> Thread:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            if thread.title_source in {"manual", "semantic"}:
                return thread
            thread.title = title
            thread.title_source = "semantic"
            thread.title_updated_at = utc_now()
            self._save_thread(conn, thread)
            return thread

    def set_thread_status(self, tenant_id: str, thread_id: str, status: ThreadStatus) -> Thread:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            current = _CURRENT_RUN.get()
            if current is not None and current[:2] == (tenant_id, thread_id):
                owned = conn.execute(
                    """
                    SELECT 1 FROM thread_runs
                    WHERE tenant_id = ? AND thread_id = ? AND run_id = ? AND owner_instance_id = ?
                    """,
                    (tenant_id, thread_id, current[2], self._instance_id),
                ).fetchone()
                if owned is None:
                    return thread
                if status != ThreadStatus.RUNNING:
                    conn.execute(
                        "DELETE FROM thread_runs WHERE tenant_id = ? AND thread_id = ? AND run_id = ?",
                        (tenant_id, thread_id, current[2]),
                    )
                    _CURRENT_RUN.set(None)
            thread.status = status
            thread.updated_at = utc_now()
            self._save_thread(conn, thread)
            return thread

    def set_thread_organization(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        pinned: bool | None = None,
        archived: bool | None = None,
    ) -> Thread:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            now = utc_now()
            if pinned is not None and pinned != (thread.pinned_at is not None):
                thread.pinned_at = now if pinned else None
            if archived is not None and archived != (thread.archived_at is not None):
                thread.archived_at = now if archived else None
            self._save_thread(conn, thread)
            return thread

    def restore_thread_timestamps(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        created_at: datetime,
        updated_at: datetime,
    ) -> Thread:
        with self._lock, self._connection() as conn:
            thread = self._require_thread(conn, tenant_id, thread_id)
            thread.created_at = created_at
            thread.updated_at = updated_at
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
            self._require_current_run(conn, tenant_id, thread_id)
            thread = self._require_thread(conn, tenant_id, thread_id)
            context = self._require_context(conn, thread_id)
            context.summary = summary
            context.summarized_message_count = summarized_message_count
            context.updated_at = utc_now()
            thread.updated_at = utc_now()
            self._save_context(conn, context)
            self._save_thread(conn, thread)
            return context

    def start_run(self, tenant_id: str, thread_id: str) -> Thread:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            thread = self._require_thread(conn, tenant_id, thread_id)
            if thread.status == ThreadStatus.RUNNING:
                raise HTTPException(
                    status_code=409, detail=f"Thread '{thread_id}' is already running"
                )
            run_id = uuid4().hex
            now = time.time()
            conn.execute(
                """
                INSERT INTO thread_runs (
                  tenant_id, thread_id, run_id, owner_instance_id, lease_expires_at,
                  cancellation_requested, started_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    tenant_id,
                    thread_id,
                    run_id,
                    self._instance_id,
                    now + DEFAULT_RUN_LEASE_SECONDS,
                    now,
                    now,
                ),
            )
            thread.status = ThreadStatus.RUNNING
            thread.updated_at = utc_now()
            self._save_thread(conn, thread)
            _CURRENT_RUN.set((tenant_id, thread_id, run_id))
            return thread

    def owned_run_id(self, tenant_id: str, thread_id: str) -> str | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT run_id FROM thread_runs
                WHERE tenant_id = ? AND thread_id = ? AND owner_instance_id = ?
                """,
                (tenant_id, thread_id, self._instance_id),
            ).fetchone()
            return str(row[0]) if row is not None else None

    def heartbeat_run(
        self, tenant_id: str, thread_id: str, *, run_id: str, lease_seconds: float
    ) -> bool:
        now = time.time()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE thread_runs
                SET lease_expires_at = ?, heartbeat_at = ?
                WHERE tenant_id = ? AND thread_id = ? AND run_id = ? AND owner_instance_id = ?
                  AND lease_expires_at > ?
                """,
                (
                    now + lease_seconds,
                    now,
                    tenant_id,
                    thread_id,
                    run_id,
                    self._instance_id,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def run_cancellation_requested(self, tenant_id: str, thread_id: str, *, run_id: str) -> bool:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT cancellation_requested FROM thread_runs
                WHERE tenant_id = ? AND thread_id = ? AND run_id = ? AND owner_instance_id = ?
                """,
                (tenant_id, thread_id, run_id, self._instance_id),
            ).fetchone()
            return bool(row is not None and row[0])

    def request_run_cancellation(self, tenant_id: str, thread_id: str) -> bool:
        with self._lock, self._connection() as conn:
            self._require_thread(conn, tenant_id, thread_id)
            cursor = conn.execute(
                """
                UPDATE thread_runs SET cancellation_requested = 1
                WHERE tenant_id = ? AND thread_id = ?
                """,
                (tenant_id, thread_id),
            )
            return cursor.rowcount == 1

    def attach_peer_task(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        peer_name: str,
        peer_base_url: str,
        task_id: str,
    ) -> bool:
        current = _CURRENT_RUN.get()
        if current is None or current[:2] != (tenant_id, thread_id):
            return False
        now = time.time()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE thread_runs
                SET peer_name = ?, peer_base_url = ?, peer_task_id = ?
                WHERE tenant_id = ? AND thread_id = ? AND run_id = ?
                  AND owner_instance_id = ? AND lease_expires_at > ?
                """,
                (
                    peer_name,
                    peer_base_url,
                    task_id,
                    tenant_id,
                    thread_id,
                    current[2],
                    self._instance_id,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def enqueue_owned_peer_task_cancellation(self, tenant_id: str, thread_id: str) -> bool:
        current = _CURRENT_RUN.get()
        if current is None or current[:2] != (tenant_id, thread_id):
            return False
        now = time.time()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO peer_task_cancellations (
                  cancellation_id, tenant_id, thread_id, peer_name, peer_base_url,
                  peer_task_id, attempts, next_attempt_at, created_at
                )
                SELECT run_id, tenant_id, thread_id, peer_name, peer_base_url,
                       peer_task_id, 0, ?, ?
                FROM thread_runs
                WHERE tenant_id = ? AND thread_id = ? AND run_id = ?
                  AND owner_instance_id = ?
                  AND peer_name IS NOT NULL AND peer_base_url IS NOT NULL
                  AND peer_task_id IS NOT NULL
                """,
                (
                    now,
                    now,
                    tenant_id,
                    thread_id,
                    current[2],
                    self._instance_id,
                ),
            )
            return cursor.rowcount == 1

    def claim_peer_task_cancellations(
        self, *, lease_seconds: float, limit: int
    ) -> list[PeerTaskCancellation]:
        now = time.time()
        claimed: list[PeerTaskCancellation] = []
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT cancellation_id, peer_name, peer_base_url, peer_task_id, attempts
                FROM peer_task_cancellations
                WHERE next_attempt_at <= ?
                  AND (claim_owner IS NULL OR claim_expires_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (now, now, max(0, limit)),
            ).fetchall()
            for row in rows:
                cancellation_id = str(row[0])
                attempts = int(row[4]) + 1
                conn.execute(
                    """
                    UPDATE peer_task_cancellations
                    SET claim_owner = ?, claim_expires_at = ?, attempts = ?
                    WHERE cancellation_id = ?
                    """,
                    (self._instance_id, now + lease_seconds, attempts, cancellation_id),
                )
                claimed.append(
                    PeerTaskCancellation(
                        cancellation_id=cancellation_id,
                        peer_name=str(row[1]),
                        peer_base_url=str(row[2]),
                        task_id=str(row[3]),
                        attempts=attempts,
                    )
                )
        return claimed

    def complete_peer_task_cancellation(self, cancellation_id: str) -> bool:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM peer_task_cancellations
                WHERE cancellation_id = ? AND claim_owner = ?
                """,
                (cancellation_id, self._instance_id),
            )
            return cursor.rowcount == 1

    def release_peer_task_cancellation(
        self, cancellation_id: str, *, retry_delay_seconds: float
    ) -> bool:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE peer_task_cancellations
                SET claim_owner = NULL, claim_expires_at = NULL, next_attempt_at = ?
                WHERE cancellation_id = ? AND claim_owner = ?
                """,
                (time.time() + retry_delay_seconds, cancellation_id, self._instance_id),
            )
            return cursor.rowcount == 1

    def recover_stale_runs(self) -> int:
        now = time.time()
        recovered = 0
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale_rows = conn.execute(
                """
                SELECT tenant_id, thread_id, run_id, peer_name, peer_base_url, peer_task_id
                FROM thread_runs WHERE lease_expires_at <= ?
                """,
                (now,),
            ).fetchall()
            stale_keys = {(str(row[0]), str(row[1])) for row in stale_rows}
            for row in stale_rows:
                tenant_id = str(row[0])
                thread_id = str(row[1])
                run_id = str(row[2])
                peer_name = str(row[3]) if row[3] is not None else None
                peer_base_url = str(row[4]) if row[4] is not None else None
                peer_task_id = str(row[5]) if row[5] is not None else None
                if peer_name and peer_base_url and peer_task_id:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO peer_task_cancellations (
                          cancellation_id, tenant_id, thread_id, peer_name, peer_base_url,
                          peer_task_id, attempts, next_attempt_at, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            run_id,
                            tenant_id,
                            thread_id,
                            peer_name,
                            peer_base_url,
                            peer_task_id,
                            now,
                            now,
                        ),
                    )
                thread = self._require_thread(conn, tenant_id, thread_id)
                if thread.status == ThreadStatus.RUNNING:
                    thread.status = ThreadStatus.ERROR
                    thread.updated_at = utc_now()
                    self._save_thread(conn, thread)
                conn.execute(
                    "DELETE FROM thread_runs WHERE tenant_id = ? AND thread_id = ?",
                    (tenant_id, thread_id),
                )
                recovered += 1
            rows = conn.execute("SELECT tenant_id, thread_id, payload FROM threads").fetchall()
            for tenant_id_value, thread_id_value, payload in rows:
                thread = Thread.model_validate(json.loads(payload))
                key = (str(tenant_id_value), str(thread_id_value))
                if thread.status != ThreadStatus.RUNNING or key in stale_keys:
                    continue
                active = conn.execute(
                    "SELECT 1 FROM thread_runs WHERE tenant_id = ? AND thread_id = ?",
                    key,
                ).fetchone()
                if active is None:
                    thread.status = ThreadStatus.ERROR
                    thread.updated_at = utc_now()
                    self._save_thread(conn, thread)
                    recovered += 1
            return recovered

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;
                PRAGMA journal_mode = WAL;
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
                CREATE TABLE IF NOT EXISTS thread_runs (
                    thread_id TEXT PRIMARY KEY REFERENCES threads(thread_id) ON DELETE CASCADE,
                    tenant_id TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    owner_instance_id TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    cancellation_requested INTEGER NOT NULL CHECK (cancellation_requested IN (0, 1)),
                    started_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    peer_name TEXT,
                    peer_base_url TEXT,
                    peer_task_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_thread_runs_lease
                    ON thread_runs (lease_expires_at);
                CREATE TABLE IF NOT EXISTS archive_imports (
                    tenant_id TEXT NOT NULL,
                    archive_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    claim_token TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    thread_id TEXT REFERENCES threads(thread_id) ON DELETE CASCADE,
                    response_payload TEXT,
                    PRIMARY KEY (tenant_id, archive_id)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_imports_thread_id
                    ON archive_imports (thread_id);
                CREATE TABLE IF NOT EXISTS peer_task_cancellations (
                    cancellation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    peer_name TEXT NOT NULL,
                    peer_base_url TEXT NOT NULL,
                    peer_task_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    claim_owner TEXT,
                    claim_expires_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_peer_task_cancellations_due
                    ON peer_task_cancellations (next_attempt_at, claim_expires_at);
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
            _ensure_thread_run_columns(conn)
            self._initialize_message_search(conn)

    def _initialize_message_search(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(
                    position UNINDEXED,
                    thread_id UNINDEXED,
                    tenant_id UNINDEXED,
                    content,
                    tokenize = 'unicode61'
                )
                """
            )
        except sqlite3.OperationalError:
            self._fts_available = False
            return
        self._fts_available = True
        conn.execute("DELETE FROM message_search")
        rows = conn.execute(
            """
            SELECT messages.position, threads.tenant_id, messages.payload
            FROM messages
            JOIN threads ON threads.thread_id = messages.thread_id
            ORDER BY messages.position
            """
        ).fetchall()
        for position, tenant_id, payload in rows:
            self._index_message(
                conn,
                str(tenant_id),
                int(position),
                Message.model_validate(json.loads(payload)),
            )

    def _index_message(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        position: int,
        message: Message,
    ) -> None:
        if (
            not self._fts_available
            or message.role not in {MessageRole.USER, MessageRole.ASSISTANT}
            or message.tool_name is not None
            or message.tool_call_id is not None
            or message.tool_arguments is not None
        ):
            return
        conn.execute(
            """
            INSERT INTO message_search (position, thread_id, tenant_id, content)
            VALUES (?, ?, ?, ?)
            """,
            (str(position), message.thread_id, tenant_id, message.content),
        )

    def _insert_messages(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        thread_id: str,
        messages: list[Message],
    ) -> None:
        for message in messages:
            cursor = conn.execute(
                "INSERT INTO messages (thread_id, payload) VALUES (?, ?)",
                (thread_id, _dump_model(message)),
            )
            self._index_message(conn, tenant_id, _last_insert_position(cursor), message)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
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
        q: str | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
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
                q=q,
                archived=archived,
                pinned=pinned,
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

    def _require_current_run(
        self, conn: sqlite3.Connection, tenant_id: str, thread_id: str
    ) -> None:
        current = _CURRENT_RUN.get()
        if current is None or current[:2] != (tenant_id, thread_id):
            return
        row = conn.execute(
            """
            SELECT 1 FROM thread_runs
            WHERE tenant_id = ? AND thread_id = ? AND run_id = ?
              AND owner_instance_id = ? AND lease_expires_at > ?
            """,
            (tenant_id, thread_id, current[2], self._instance_id, time.time()),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="Thread run lease was lost")

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


def _fork_message_prefix(messages: list[Message], at_message_id: str) -> list[Message]:
    boundary = next(
        (index for index, message in enumerate(messages) if message.id == at_message_id),
        None,
    )
    if boundary is None:
        raise HTTPException(status_code=404, detail=f"Message '{at_message_id}' not found")

    boundary_message = messages[boundary]
    if (
        boundary_message.role == MessageRole.ASSISTANT
        and boundary_message.tool_name
        and boundary_message.tool_call_id
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot fork after tool call '{boundary_message.tool_call_id}' without its result"
            ),
        )
    if boundary_message.role == MessageRole.TOOL and (
        boundary == 0 or not _is_completed_tool_pair(messages[boundary - 1], boundary_message)
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot fork after an orphaned tool result",
        )
    return messages[: boundary + 1]


def _compacted_fork_suffix(
    messages: list[Message],
    fork_message_id: str,
    compacted_through_message_id: str,
) -> list[Message]:
    head = next(
        (index for index, message in enumerate(messages) if message.id == fork_message_id),
        None,
    )
    if head is None:
        raise HTTPException(status_code=404, detail=f"Message '{fork_message_id}' not found")
    if head != len(messages) - 1:
        raise HTTPException(status_code=409, detail="Source thread changed during compaction")
    cutoff = next(
        (
            index
            for index, message in enumerate(messages[:head])
            if message.id == compacted_through_message_id
        ),
        None,
    )
    if cutoff is None:
        raise HTTPException(
            status_code=404,
            detail=f"Message '{compacted_through_message_id}' not found",
        )
    if cutoff + 1 <= head and _is_completed_tool_pair(messages[cutoff], messages[cutoff + 1]):
        raise HTTPException(
            status_code=422, detail="Compaction cannot split a tool call and result"
        )
    return messages[cutoff + 1 : head + 1]


def _is_completed_tool_pair(assistant_message: Message, tool_message: Message) -> bool:
    return (
        assistant_message.role == MessageRole.ASSISTANT
        and tool_message.role == MessageRole.TOOL
        and bool(assistant_message.tool_name)
        and bool(assistant_message.tool_call_id)
        and assistant_message.tool_call_id == tool_message.tool_call_id
    )


def _forked_thread(
    source: Thread,
    child_thread_id: str,
    at_message_id: str,
    *,
    compacted_through_message_id: str | None = None,
) -> Thread:
    return Thread(
        thread_id=child_thread_id,
        tenant_id=source.tenant_id,
        execution_user_id=source.execution_user_id,
        title=source.title,
        title_source=source.title_source,
        title_updated_at=source.title_updated_at,
        skill_name=source.skill_name,
        skill_names=list(source.skill_names) if source.skill_names is not None else None,
        capability_profile=source.capability_profile,
        llm_profile=source.llm_profile,
        parent_thread_id=source.thread_id,
        fork_message_id=at_message_id,
        compacted_through_message_id=compacted_through_message_id,
    )


def _copy_fork_messages(
    messages: list[Message],
    child_thread_id: str,
    *,
    attachment_id_map: Mapping[str, str] | None,
) -> list[Message]:
    copied: list[Message] = []
    attachment_id_map = attachment_id_map or {}
    for message in messages:
        parts = remap_attachment_ids(message.parts, attachment_id_map)
        copied.append(
            message.model_copy(
                deep=True,
                update={
                    "id": str(uuid4()),
                    "thread_id": child_thread_id,
                    "source_message_id": message.id,
                    "parts": parts,
                },
            )
        )
    return copied


def build_thread_store_from_env() -> ThreadStore:
    settings = thread_store_settings_from_env()
    if settings.db_path is not None:
        return SQLiteThreadStore(settings.db_path)
    return InMemoryThreadStore()


def _last_insert_position(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite message insert did not return a row ID")
    return cursor.lastrowid


def _search_terms(query: str) -> list[str]:
    return list(dict.fromkeys(term.casefold() for term in re.findall(r"\w+", query, re.UNICODE)))


def _message_matches_search(message: Message, terms: list[str]) -> bool:
    if message.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
        return False
    if (
        message.tool_name is not None
        or message.tool_call_id is not None
        or message.tool_arguments is not None
    ):
        return False
    content = message.content.casefold()
    return all(term in content for term in terms)


def _thread_matches_filters(
    thread: Thread,
    tenant_id: str,
    *,
    status: ThreadStatus | None,
    capability_profile: str | None,
    skill: str | None,
    created_after: datetime | None,
    updated_after: datetime | None,
    q: str | None = None,
    archived: bool | None = None,
    pinned: bool | None = None,
) -> bool:
    if thread.tenant_id != tenant_id:
        return False
    if status is not None and thread.status != status:
        return False
    if capability_profile is not None and thread.capability_profile != capability_profile:
        return False
    if skill is not None and thread.skill_name != skill and skill not in (thread.skill_names or []):
        return False
    if q is not None and q.casefold() not in (thread.title or "New conversation").casefold():
        return False
    if archived is not None and (thread.archived_at is not None) != archived:
        return False
    if pinned is not None and (thread.pinned_at is not None) != pinned:
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


def _ensure_thread_run_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(thread_runs)").fetchall()
    columns = {str(row[1]) for row in rows}
    for column_name in ["peer_name", "peer_base_url", "peer_task_id"]:
        if column_name not in columns:
            conn.execute(f"ALTER TABLE thread_runs ADD COLUMN {column_name} TEXT")


def _dump_model(model: AuditRecord | Message | Thread | ThreadContext) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
