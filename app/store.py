from __future__ import annotations

from threading import Lock

from fastapi import HTTPException

from app.models import Message, Thread, ThreadContext, ThreadStatus, utc_now


class InMemoryThreadStore:
    def __init__(self) -> None:
        self._threads: dict[str, Thread] = {}
        self._contexts: dict[str, ThreadContext] = {}
        self._messages: dict[str, list[Message]] = {}
        self._lock = Lock()

    def create_thread(
        self,
        tenant_id: str,
        *,
        skill_name: str | None = None,
        skill_names: list[str] | None = None,
        capability_profile: str | None = None,
    ) -> Thread:
        with self._lock:
            normalized_skill_names = list(skill_names) if skill_names is not None else None
            thread = Thread(
                tenant_id=tenant_id,
                skill_name=skill_name,
                skill_names=normalized_skill_names,
                capability_profile=capability_profile,
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
            self._messages[thread_id] = self._messages[thread_id][context.summarized_message_count :]
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
