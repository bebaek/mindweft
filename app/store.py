from __future__ import annotations

from threading import Lock

from fastapi import HTTPException

from app.models import Message, Thread, ThreadStatus, utc_now


class InMemoryThreadStore:
    def __init__(self) -> None:
        self._threads: dict[str, Thread] = {}
        self._messages: dict[str, list[Message]] = {}
        self._lock = Lock()

    def create_thread(self, tenant_id: str, *, skill_name: str | None = None) -> Thread:
        with self._lock:
            thread = Thread(tenant_id=tenant_id, skill_name=skill_name)
            self._threads[thread.thread_id] = thread
            self._messages[thread.thread_id] = []
            return thread

    def delete_thread(self, tenant_id: str, thread_id: str) -> None:
        with self._lock:
            self._require_thread(tenant_id, thread_id)
            del self._threads[thread_id]
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
