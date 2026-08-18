import os
from pathlib import Path

import pytest

import app.store
from app.store import InMemoryThreadStore

_TEST_CONFIG_FILE = str(Path(__file__).with_name("__no_default_minigent.toml"))
_TEST_DOTENV_FILE = str(Path(__file__).with_name("__no_default_minigent.env"))

# Patch build_thread_store_from_env at module level so that even the
# module-level ``create_app()`` call at the bottom of app/main.py
# (which runs at import time, before any fixture) uses InMemoryThreadStore.
app.store.build_thread_store_from_env = lambda: InMemoryThreadStore()


def pytest_configure(config: pytest.Config) -> None:
    """Remove Mindweft storage settings before any test module import.

    Belt-and-suspenders: even though build_thread_store_from_env is
    already patched above, we also clear the env var so no other code
    path can accidentally reach the developer's SQLite database.
    """
    os.environ["MINDWEFT_CONFIG_FILE"] = _TEST_CONFIG_FILE
    os.environ["MINIGENT_CONFIG_FILE"] = _TEST_CONFIG_FILE
    os.environ["MINDWEFT_DOTENV_FILE"] = _TEST_DOTENV_FILE
    os.environ["MINIGENT_DOTENV_FILE"] = _TEST_DOTENV_FILE
    for prefix in ("MINDWEFT", "MINIGENT"):
        os.environ.pop(f"{prefix}_THREAD_DB_PATH", None)
        os.environ.pop(f"{prefix}_PRIVATE_VALUE_DB_PATH", None)
        os.environ.pop(f"{prefix}_PRIVATE_CONSENT_DB_PATH", None)
        os.environ.pop(f"{prefix}_SESSION_CREDENTIALS", None)
        os.environ.pop(f"{prefix}_SESSION_SECRET", None)


@pytest.fixture(autouse=True)
def isolate_test_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep test configuration, client state, and databases out of developer storage.

    Each test gets an ephemeral HOME/XDG config root, cannot discover the repository's local
    config files through the explicit config variables, and uses InMemoryThreadStore unless it
    deliberately supplies its own temporary store.
    """
    test_home = tmp_path / "home"
    test_home.mkdir()
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(test_home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(test_home / ".local" / "state"))
    monkeypatch.setenv("MINDWEFT_CONFIG_FILE", _TEST_CONFIG_FILE)
    monkeypatch.setenv("MINIGENT_CONFIG_FILE", _TEST_CONFIG_FILE)
    monkeypatch.setenv("MINDWEFT_DOTENV_FILE", _TEST_DOTENV_FILE)
    monkeypatch.setenv("MINIGENT_DOTENV_FILE", _TEST_DOTENV_FILE)
    for prefix in ("MINDWEFT", "MINIGENT"):
        monkeypatch.delenv(f"{prefix}_THREAD_DB_PATH", raising=False)
        monkeypatch.delenv(f"{prefix}_PRIVATE_VALUE_DB_PATH", raising=False)
        monkeypatch.delenv(f"{prefix}_PRIVATE_CONSENT_DB_PATH", raising=False)
        monkeypatch.delenv(f"{prefix}_SESSION_CREDENTIALS", raising=False)
        monkeypatch.delenv(f"{prefix}_SESSION_SECRET", raising=False)
    monkeypatch.setattr(
        "app.main.build_thread_store_from_env",
        lambda: InMemoryThreadStore(),
    )
    # Also patch the store module level reference
    monkeypatch.setattr(
        "app.store.build_thread_store_from_env",
        lambda: InMemoryThreadStore(),
    )
