import importlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.store as store_module
from app.models import ThreadStatus
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
