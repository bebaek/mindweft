from __future__ import annotations

from threading import Lock

from fastapi import HTTPException

from app.models import Message, Thread, ThreadStatus, utc_now


class InMemoryThreadStore:
    def __init__(self) -> None:
        self._threads: dict[str, Thread] = {}
        self._messages: dict[str, list[Message]] = {}
        self._lock = Lock()

    def create_thread(self) -> Thread:
        with self._lock:
            thread = Thread()
            self._threads[thread.thread_id] = thread
            self._messages[thread.thread_id] = []
            return thread

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            self._require_thread(thread_id)
            del self._threads[thread_id]
            del self._messages[thread_id]

    def list_messages(self, thread_id: str) -> list[Message]:
        with self._lock:
            self._require_thread(thread_id)
            return list(self._messages[thread_id])

    def append_message(self, message: Message) -> Message:
        with self._lock:
            thread = self._require_thread(message.thread_id)
            self._messages[message.thread_id].append(message)
            thread.updated_at = utc_now()
            return message

    def set_thread_status(self, thread_id: str, status: ThreadStatus) -> Thread:
        with self._lock:
            thread = self._require_thread(thread_id)
            thread.status = status
            thread.updated_at = utc_now()
            return thread

    def start_run(self, thread_id: str) -> Thread:
        with self._lock:
            thread = self._require_thread(thread_id)
            if thread.status == ThreadStatus.RUNNING:
                raise HTTPException(status_code=409, detail=f"Thread '{thread_id}' is already running")
            thread.status = ThreadStatus.RUNNING
            thread.updated_at = utc_now()
            return thread

    def _require_thread(self, thread_id: str) -> Thread:
        thread = self._threads.get(thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
        return thread
