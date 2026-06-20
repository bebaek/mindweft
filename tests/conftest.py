import os
from pathlib import Path

import pytest

import app.store
from app.store import InMemoryThreadStore

_ORIGINAL_CONFIG_FILE = os.environ.get("MINIGENT_CONFIG_FILE")
_ORIGINAL_DOTENV_FILE = os.environ.get("MINIGENT_DOTENV_FILE")
_TEST_CONFIG_FILE = str(Path(__file__).with_name("__no_default_minigent.toml"))
_TEST_DOTENV_FILE = str(Path(__file__).with_name("__no_default_minigent.env"))

# Patch build_thread_store_from_env at module level so that even the
# module-level ``create_app()`` call at the bottom of app/main.py
# (which runs at import time, before any fixture) uses InMemoryThreadStore.
app.store.build_thread_store_from_env = lambda: InMemoryThreadStore()


def pytest_configure(config: pytest.Config) -> None:
    """Remove MINIGENT_THREAD_DB_PATH before any test module import.

    Belt-and-suspenders: even though build_thread_store_from_env is
    already patched above, we also clear the env var so no other code
    path can accidentally reach the developer's SQLite database.
    """
    if _ORIGINAL_CONFIG_FILE is None:
        os.environ["MINIGENT_CONFIG_FILE"] = _TEST_CONFIG_FILE
    if _ORIGINAL_DOTENV_FILE is None:
        os.environ["MINIGENT_DOTENV_FILE"] = _TEST_DOTENV_FILE
    os.environ.pop("MINIGENT_THREAD_DB_PATH", None)


@pytest.fixture(autouse=True)
def isolate_thread_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force InMemoryThreadStore for all tests so they never touch the developer's DB.

    1. Remove MINIGENT_THREAD_DB_PATH so the env-based fallback never picks SQLite.
    2. Patch build_thread_store_from_env so any code path that calls it
       (including create_app() without an explicit thread_store) gets InMemoryThreadStore.
    """
    if _ORIGINAL_CONFIG_FILE is None:
        monkeypatch.delenv("MINIGENT_CONFIG_FILE", raising=False)
    if _ORIGINAL_DOTENV_FILE is None:
        monkeypatch.delenv("MINIGENT_DOTENV_FILE", raising=False)
    monkeypatch.delenv("MINIGENT_THREAD_DB_PATH", raising=False)
    monkeypatch.setattr(
        "app.main.build_thread_store_from_env",
        lambda: InMemoryThreadStore(),
    )
    # Also patch the store module level reference
    monkeypatch.setattr(
        "app.store.build_thread_store_from_env",
        lambda: InMemoryThreadStore(),
    )
