import asyncio
import importlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

import app.store as store_module
from app.models import Message, MessageRole, ThreadStatus
from app.peer_agents import PeerAgentRegistry
from app.store import (
    ArchiveImportConflictError,
    ArchiveImportInProgressError,
    InMemoryThreadStore,
    SQLiteThreadStore,
    ThreadStoreSettings,
    thread_store_settings_from_env,
)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_archive_import_claims_are_idempotent_and_tenant_scoped(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        InMemoryThreadStore()
        if store_kind == "memory"
        else SQLiteThreadStore(tmp_path / "archive-imports.db")
    )

    assert store.lookup_archive_import("tenant-a", "archive-1", "digest-1") is None
    first = store.begin_archive_import("tenant-a", "archive-1", "digest-1")
    assert first.claim_token is not None
    assert first.completed is False
    with pytest.raises(ArchiveImportInProgressError):
        store.begin_archive_import("tenant-a", "archive-1", "digest-1")
    with pytest.raises(ArchiveImportInProgressError):
        store.lookup_archive_import("tenant-a", "archive-1", "digest-1")
    with pytest.raises(ArchiveImportConflictError):
        store.begin_archive_import("tenant-a", "archive-1", "different-digest")

    store.abandon_archive_import("tenant-a", "archive-1", first.claim_token)
    replacement = store.begin_archive_import("tenant-a", "archive-1", "digest-1")
    assert replacement.claim_token is not None
    thread = store.create_thread("tenant-a")
    response = {"thread_id": thread.thread_id, "message_count": 2}
    store.complete_archive_import(
        "tenant-a",
        "archive-1",
        replacement.claim_token,
        thread_id=thread.thread_id,
        response_payload=response,
    )

    replay = store.lookup_archive_import("tenant-a", "archive-1", "digest-1")
    assert replay is not None
    assert replay.completed is True
    assert replay.claim_token is None
    assert replay.response_payload == response
    other_tenant = store.begin_archive_import("tenant-b", "archive-1", "digest-1")
    assert other_tenant.claim_token is not None

    store.delete_thread("tenant-a", thread.thread_id)
    after_delete = store.begin_archive_import("tenant-a", "archive-1", "digest-1")
    assert after_delete.claim_token is not None


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_archive_import_claim_can_recover_after_lease_expiry(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        InMemoryThreadStore()
        if store_kind == "memory"
        else SQLiteThreadStore(tmp_path / "archive-import-leases.db")
    )
    stale = store.begin_archive_import(
        "tenant-a",
        "archive-1",
        "digest-1",
        lease_seconds=0,
    )
    replacement = store.begin_archive_import("tenant-a", "archive-1", "digest-1")
    assert stale.claim_token is not None
    assert replacement.claim_token is not None
    assert replacement.claim_token != stale.claim_token
    thread = store.create_thread("tenant-a")
    with pytest.raises(RuntimeError, match="no longer active"):
        store.complete_archive_import(
            "tenant-a",
            "archive-1",
            stale.claim_token,
            thread_id=thread.thread_id,
            response_payload={"thread_id": thread.thread_id},
        )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_thread_search_only_indexes_user_and_assistant_content(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        InMemoryThreadStore()
        if store_kind == "memory"
        else SQLiteThreadStore(tmp_path / "search-threads.db")
    )
    thread = store.create_thread("tenant-a")
    for role, content in (
        (MessageRole.SYSTEM, "private system lighthouse"),
        (MessageRole.TOOL, "private tool lighthouse"),
        (MessageRole.USER, "user-visible lighthouse"),
        (MessageRole.ASSISTANT, "assistant-visible lighthouse"),
    ):
        store.append_message(
            "tenant-a",
            Message(thread_id=thread.thread_id, role=role, content=content),
        )
    store.append_message(
        "tenant-a",
        Message(
            thread_id=thread.thread_id,
            role=MessageRole.ASSISTANT,
            content="private assistant tool lighthouse",
            tool_name="lookup",
            tool_call_id="call-1",
            tool_arguments={"query": "lighthouse"},
        ),
    )
    other = store.create_thread("tenant-b")
    store.append_message(
        "tenant-b",
        Message(thread_id=other.thread_id, role=MessageRole.USER, content="other lighthouse"),
    )

    matches = store.search_messages("tenant-a", query="lighthouse")

    assert [message.role for message in matches] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert all(message.thread_id == thread.thread_id for message in matches)
    if isinstance(store, SQLiteThreadStore):
        store._fts_available = False
        fallback_matches = store.search_messages("tenant-a", query="lighthouse")
        assert [message.role for message in fallback_matches] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
        ]


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_thread_store_fork_copies_prefix_and_preserves_source(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        InMemoryThreadStore()
        if store_kind == "memory"
        else SQLiteThreadStore(tmp_path / "fork-threads.db")
    )
    source = store.create_thread(
        "tenant-a",
        execution_user_id="user-a",
        skill_names=["research"],
        capability_profile="tools",
        llm_profile="primary",
    )
    first = store.append_message(
        "tenant-a",
        Message(thread_id=source.thread_id, role=MessageRole.USER, content="first"),
    )
    store.append_message(
        "tenant-a",
        Message(thread_id=source.thread_id, role=MessageRole.ASSISTANT, content="answer"),
    )
    store.append_message(
        "tenant-a",
        Message(thread_id=source.thread_id, role=MessageRole.USER, content="later"),
    )
    store.update_thread_context(
        "tenant-a",
        source.thread_id,
        summary="Earlier imported context.",
        summarized_message_count=0,
    )

    child = store.fork_thread(
        "tenant-a",
        source.thread_id,
        at_message_id=first.id,
    )

    assert child.parent_thread_id == source.thread_id
    assert child.fork_message_id == first.id
    assert child.execution_user_id == "user-a"
    assert child.skill_names == ["research"]
    assert child.capability_profile == "tools"
    assert child.llm_profile == "primary"
    child_messages = store.list_messages("tenant-a", child.thread_id)
    assert [message.content for message in child_messages] == ["first"]
    assert child_messages[0].id != first.id
    assert child_messages[0].source_message_id == first.id
    assert child_messages[0].thread_id == child.thread_id
    store.append_message(
        "tenant-a",
        Message(thread_id=child.thread_id, role=MessageRole.USER, content="child only"),
    )
    assert [message.content for message in store.list_messages("tenant-a", child.thread_id)] == [
        "first",
        "child only",
    ]
    assert [message.content for message in store.list_messages("tenant-a", source.thread_id)] == [
        "first",
        "answer",
        "later",
    ]
    assert store.get_thread_context("tenant-a", child.thread_id).summary == (
        "Earlier imported context."
    )
    assert child.thread_id in {
        message.thread_id for message in store.search_messages("tenant-a", query="first")
    }


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_thread_store_fork_rejects_split_tool_call(store_kind: str, tmp_path: Path) -> None:
    store = (
        InMemoryThreadStore()
        if store_kind == "memory"
        else SQLiteThreadStore(tmp_path / "fork-tool-threads.db")
    )
    source = store.create_thread("tenant-a")
    tool_call = store.append_message(
        "tenant-a",
        Message(
            thread_id=source.thread_id,
            role=MessageRole.ASSISTANT,
            content="",
            tool_name="echo",
            tool_call_id="call-1",
            tool_arguments={"text": "hello"},
        ),
    )
    tool_result = store.append_message(
        "tenant-a",
        Message(
            thread_id=source.thread_id,
            role=MessageRole.TOOL,
            content='{"echo":"hello"}',
            tool_name="echo",
            tool_call_id="call-1",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        store.fork_thread("tenant-a", source.thread_id, at_message_id=tool_call.id)

    assert exc_info.value.status_code == 422
    child = store.fork_thread("tenant-a", source.thread_id, at_message_id=tool_result.id)
    assert [message.role for message in store.list_messages("tenant-a", child.thread_id)] == [
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_thread_store_creates_compacted_fork_from_retained_suffix(
    store_kind: str, tmp_path: Path
) -> None:
    store = (
        InMemoryThreadStore()
        if store_kind == "memory"
        else SQLiteThreadStore(tmp_path / "compacted-fork.db")
    )
    source = store.create_thread("tenant-a")
    messages = [
        store.append_message(
            "tenant-a",
            Message(thread_id=source.thread_id, role=MessageRole.USER, content=f"message-{index}"),
        )
        for index in range(10)
    ]

    child = store.fork_compacted_thread(
        "tenant-a",
        source.thread_id,
        fork_message_id=messages[-1].id,
        compacted_through_message_id=messages[1].id,
        summary="User: message-0\nUser: message-1",
    )

    assert child.parent_thread_id == source.thread_id
    assert child.fork_message_id == messages[-1].id
    assert child.compacted_through_message_id == messages[1].id
    assert [message.content for message in store.list_messages("tenant-a", child.thread_id)] == [
        f"message-{index}" for index in range(2, 10)
    ]
    assert [message.content for message in store.list_messages("tenant-a", source.thread_id)] == [
        f"message-{index}" for index in range(10)
    ]
    assert store.get_thread_context("tenant-a", child.thread_id).summary == (
        "User: message-0\nUser: message-1"
    )


def test_thread_store_settings_from_env_mapping_uses_defaults() -> None:
    assert ThreadStoreSettings.from_env({}) == ThreadStoreSettings(db_path=None)


def test_thread_store_settings_from_env_mapping_prefers_mindweft_path() -> None:
    assert ThreadStoreSettings.from_env(
        {
            "MINDWEFT_THREAD_DB_PATH": " .data/threads.db ",
            "MINIGENT_THREAD_DB_PATH": ".data/legacy.db",
        }
    ) == ThreadStoreSettings(db_path=".data/threads.db")


def test_thread_store_settings_from_env_mapping_treats_blank_as_none() -> None:
    assert ThreadStoreSettings.from_env({"MINIGENT_THREAD_DB_PATH": "\t"}) == ThreadStoreSettings(
        db_path=None
    )


def test_thread_store_settings_from_env_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDWEFT_THREAD_DB_PATH", ".data/threads.db")

    assert thread_store_settings_from_env() == ThreadStoreSettings(db_path=".data/threads.db")


def test_build_thread_store_from_env_defaults_to_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDWEFT_THREAD_DB_PATH", raising=False)
    monkeypatch.delenv("MINIGENT_THREAD_DB_PATH", raising=False)
    reloaded_store = importlib.reload(store_module)

    store = reloaded_store.build_thread_store_from_env()

    assert isinstance(store, reloaded_store.InMemoryThreadStore)


def test_build_thread_store_from_env_uses_sqlite_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "threads.db"
    monkeypatch.setenv("MINDWEFT_THREAD_DB_PATH", str(db_path))
    reloaded_store = importlib.reload(store_module)

    store = reloaded_store.build_thread_store_from_env()

    assert isinstance(store, reloaded_store.SQLiteThreadStore)
    assert store._db_path == db_path


def test_sqlite_start_run_is_atomic_across_store_instances(tmp_path: Path) -> None:
    database = tmp_path / "threads.db"
    first = SQLiteThreadStore(database)
    second = SQLiteThreadStore(database)
    thread_id = first.create_thread("tenant-a").thread_id
    barrier = threading.Barrier(2)

    def start(store: SQLiteThreadStore) -> int:
        barrier.wait()
        try:
            store.start_run("tenant-a", thread_id)
        except HTTPException as exc:
            return exc.status_code
        return 200

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(start, (first, second)))

    assert outcomes == [200, 409]


def test_sqlite_run_cancellation_crosses_store_instances(tmp_path: Path) -> None:
    database = tmp_path / "threads.db"
    owner = SQLiteThreadStore(database)
    remote = SQLiteThreadStore(database)
    thread_id = owner.create_thread("tenant-a").thread_id
    owner.start_run("tenant-a", thread_id)

    run_id = owner.owned_run_id("tenant-a", thread_id)
    assert run_id is not None
    assert remote.request_run_cancellation("tenant-a", thread_id) is True
    assert owner.heartbeat_run("tenant-a", thread_id, run_id=run_id, lease_seconds=30) is True
    assert owner.run_cancellation_requested("tenant-a", thread_id, run_id=run_id) is True

    owner.set_thread_status("tenant-a", thread_id, ThreadStatus.IDLE)
    assert remote.get_thread("tenant-a", thread_id).status == ThreadStatus.IDLE


def test_sqlite_stale_run_recovery_fences_old_owner(tmp_path: Path) -> None:
    database = tmp_path / "threads.db"
    owner = SQLiteThreadStore(database)
    recovery = SQLiteThreadStore(database)
    thread_id = owner.create_thread("tenant-a").thread_id
    owner.start_run("tenant-a", thread_id)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE thread_runs SET lease_expires_at = 0")
        connection.commit()

    assert recovery.recover_stale_runs() == 1
    assert recovery.get_thread("tenant-a", thread_id).status == ThreadStatus.ERROR

    owner.set_thread_status("tenant-a", thread_id, ThreadStatus.IDLE)
    assert recovery.get_thread("tenant-a", thread_id).status == ThreadStatus.ERROR


def test_periodic_recovery_marks_expired_run_error_without_restart(tmp_path: Path) -> None:
    from app.main import _recover_stale_runs_periodically

    async def scenario() -> None:
        database = tmp_path / "threads.db"
        owner = SQLiteThreadStore(database)
        recovery = SQLiteThreadStore(database)
        thread_id = owner.create_thread("tenant-a").thread_id
        owner.start_run("tenant-a", thread_id)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE thread_runs SET lease_expires_at = 0")
            connection.commit()

        task = asyncio.create_task(
            _recover_stale_runs_periodically(recovery, interval_seconds=0.01)
        )
        try:
            for _ in range(100):
                if recovery.get_thread("tenant-a", thread_id).status == ThreadStatus.ERROR:
                    break
                await asyncio.sleep(0.01)
            assert recovery.get_thread("tenant-a", thread_id).status == ThreadStatus.ERROR
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_periodic_recovery_cancels_persisted_peer_task(tmp_path: Path) -> None:
    from app.main import _recover_stale_runs_periodically

    async def scenario() -> None:
        requests: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(200, json={"status": "canceled"})

        database = tmp_path / "threads.db"
        owner = SQLiteThreadStore(database)
        recovery = SQLiteThreadStore(database)
        thread_id = owner.create_thread("tenant-a").thread_id
        owner.start_run("tenant-a", thread_id)
        assert owner.attach_peer_task(
            "tenant-a",
            thread_id,
            peer_name="peer-a",
            peer_base_url="http://peer.test",
            task_id="task-1",
        )
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE thread_runs SET lease_expires_at = 0")
            connection.commit()
        registry = PeerAgentRegistry([], transport=httpx.MockTransport(handler))
        task = asyncio.create_task(
            _recover_stale_runs_periodically(
                recovery,
                registry,
                interval_seconds=0.01,
            )
        )
        try:
            for _ in range(100):
                if requests:
                    break
                await asyncio.sleep(0.01)
            assert requests == ["http://peer.test/tasks/task-1/cancel"]
            assert recovery.get_thread("tenant-a", thread_id).status == ThreadStatus.ERROR
            assert recovery.claim_peer_task_cancellations(lease_seconds=30, limit=10) == []
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_sqlite_owner_can_enqueue_peer_cancellation_before_finishing_run(tmp_path: Path) -> None:
    database = tmp_path / "threads.db"
    owner = SQLiteThreadStore(database)
    recovery = SQLiteThreadStore(database)
    thread_id = owner.create_thread("tenant-a").thread_id
    owner.start_run("tenant-a", thread_id)
    assert owner.attach_peer_task(
        "tenant-a",
        thread_id,
        peer_name="peer-a",
        peer_base_url="http://peer.test",
        task_id="task-1",
    )

    assert owner.enqueue_owned_peer_task_cancellation("tenant-a", thread_id)
    owner.set_thread_status("tenant-a", thread_id, ThreadStatus.IDLE)
    claimed = recovery.claim_peer_task_cancellations(lease_seconds=30, limit=1)

    assert len(claimed) == 1
    assert claimed[0].peer_name == "peer-a"
    assert claimed[0].peer_base_url == "http://peer.test"
    assert claimed[0].task_id == "task-1"
    assert recovery.get_thread("tenant-a", thread_id).status == ThreadStatus.IDLE


def test_sqlite_stale_peer_run_enqueues_single_claimed_cancellation(tmp_path: Path) -> None:
    database = tmp_path / "threads.db"
    owner = SQLiteThreadStore(database)
    recovery = SQLiteThreadStore(database)
    competing_recovery = SQLiteThreadStore(database)
    thread_id = owner.create_thread("tenant-a").thread_id
    owner.start_run("tenant-a", thread_id)
    assert owner.attach_peer_task(
        "tenant-a",
        thread_id,
        peer_name="peer-a",
        peer_base_url="http://peer.test",
        task_id="task-1",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE thread_runs SET lease_expires_at = 0")
        connection.commit()

    assert recovery.recover_stale_runs() == 1
    claimed = recovery.claim_peer_task_cancellations(lease_seconds=30, limit=10)

    assert len(claimed) == 1
    assert claimed[0].peer_name == "peer-a"
    assert claimed[0].peer_base_url == "http://peer.test"
    assert claimed[0].task_id == "task-1"
    assert claimed[0].attempts == 1
    assert competing_recovery.claim_peer_task_cancellations(lease_seconds=30, limit=10) == []
    assert recovery.complete_peer_task_cancellation(claimed[0].cancellation_id)
    assert recovery.claim_peer_task_cancellations(lease_seconds=30, limit=10) == []


def test_sqlite_peer_cancellation_claim_can_be_retried_by_another_instance(tmp_path: Path) -> None:
    database = tmp_path / "threads.db"
    owner = SQLiteThreadStore(database)
    first_recovery = SQLiteThreadStore(database)
    second_recovery = SQLiteThreadStore(database)
    thread_id = owner.create_thread("tenant-a").thread_id
    owner.start_run("tenant-a", thread_id)
    assert owner.attach_peer_task(
        "tenant-a",
        thread_id,
        peer_name="peer-a",
        peer_base_url="http://peer.test",
        task_id="task-1",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE thread_runs SET lease_expires_at = 0")
        connection.commit()
    first_recovery.recover_stale_runs()
    first_claim = first_recovery.claim_peer_task_cancellations(lease_seconds=30, limit=1)[0]

    assert first_recovery.release_peer_task_cancellation(
        first_claim.cancellation_id, retry_delay_seconds=0
    )
    second_claim = second_recovery.claim_peer_task_cancellations(lease_seconds=30, limit=1)

    assert len(second_claim) == 1
    assert second_claim[0].cancellation_id == first_claim.cancellation_id
    assert second_claim[0].attempts == 2


def test_sqlite_store_migrates_existing_thread_run_schema_for_peer_tasks(tmp_path: Path) -> None:
    database = tmp_path / "threads.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE threads (
                thread_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE thread_runs (
                thread_id TEXT PRIMARY KEY REFERENCES threads(thread_id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE,
                owner_instance_id TEXT NOT NULL,
                lease_expires_at REAL NOT NULL,
                cancellation_requested INTEGER NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL
            );
            """
        )

    SQLiteThreadStore(database)

    with sqlite3.connect(database) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(thread_runs)").fetchall()
        }
        outbox = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'peer_task_cancellations'"
        ).fetchone()
    assert {"peer_name", "peer_base_url", "peer_task_id"}.issubset(columns)
    assert outbox is not None
