import importlib
from pathlib import Path

import pytest

import app.store as store_module
from app.store import ThreadStoreSettings, thread_store_settings_from_env


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
