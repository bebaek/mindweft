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
from app.models import ThreadStatus
from app.peer_agents import PeerAgentRegistry
from app.store import SQLiteThreadStore, ThreadStoreSettings, thread_store_settings_from_env


def test_thread_store_settings_from_env_mapping_uses_defaults() -> None:
    assert ThreadStoreSettings.from_env({}) == ThreadStoreSettings(db_path=None)


def test_thread_store_settings_from_env_mapping_parses_path() -> None:
    assert ThreadStoreSettings.from_env(
        {"MINIGENT_THREAD_DB_PATH": " .data/threads.db "}
    ) == ThreadStoreSettings(db_path=".data/threads.db")


def test_thread_store_settings_from_env_mapping_treats_blank_as_none() -> None:
    assert ThreadStoreSettings.from_env({"MINIGENT_THREAD_DB_PATH": "\t"}) == ThreadStoreSettings(
        db_path=None
    )


def test_thread_store_settings_from_env_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_THREAD_DB_PATH", ".data/threads.db")

    assert thread_store_settings_from_env() == ThreadStoreSettings(db_path=".data/threads.db")


def test_build_thread_store_from_env_defaults_to_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINIGENT_THREAD_DB_PATH", raising=False)
    reloaded_store = importlib.reload(store_module)

    store = reloaded_store.build_thread_store_from_env()

    assert isinstance(store, reloaded_store.InMemoryThreadStore)


def test_build_thread_store_from_env_uses_sqlite_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "threads.db"
    monkeypatch.setenv("MINIGENT_THREAD_DB_PATH", str(db_path))
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
