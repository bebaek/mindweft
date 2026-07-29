from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.private_value_sqlite import SQLiteEncryptedPrivateValueStore
from app.private_values import (
    InMemoryPrivateValueStore,
    build_private_value_store_from_env,
)

KEY = bytes(range(32))
KEY_B64 = base64.urlsafe_b64encode(KEY).decode()


def test_private_value_store_factory_defaults_to_memory() -> None:
    store = build_private_value_store_from_env({})

    assert isinstance(store, InMemoryPrivateValueStore)


def test_encrypted_private_value_store_requires_key_when_configured(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY.*required"):
        build_private_value_store_from_env(
            {"MINIGENT_PRIVATE_VALUE_DB_PATH": str(tmp_path / "private.db")}
        )


def test_encrypted_private_values_survive_restart_without_plaintext_on_disk(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "private.db"
    first = SQLiteEncryptedPrivateValueStore(db_path, KEY)
    placeholder = "{{pii:email:email-ref}}"
    first.add(
        "tenant-1",
        "thread-1",
        {"email-ref": "private@example.com"},
        kinds={"email-ref": "email"},
    )

    assert all(
        b"private@example.com" not in candidate.read_bytes()
        for candidate in tmp_path.glob("private.db*")
    )
    with sqlite3.connect(db_path) as connection:
        kind, key_version = connection.execute(
            "SELECT kind, key_version FROM private_values WHERE reference = 'email-ref'"
        ).fetchone()
    assert (kind, key_version) == ("email", 1)
    second = SQLiteEncryptedPrivateValueStore(db_path, KEY)
    assert second.render_for_user("tenant-1", "thread-1", placeholder) == "private@example.com"
    assert second.resolve_for_tool("tenant-1", "thread-1", placeholder) == "private@example.com"
    assert second.render_for_user("tenant-2", "thread-1", placeholder) == placeholder
    assert second.render_for_user("tenant-1", "thread-2", placeholder) == placeholder


def test_encrypted_private_value_factory_reads_environment_mapping(tmp_path: Path) -> None:
    store = build_private_value_store_from_env(
        {
            "MINIGENT_PRIVATE_VALUE_DB_PATH": str(tmp_path / "private.db"),
            "MINIGENT_PRIVATE_VALUE_ENCRYPTION_KEY": KEY_B64,
            "MINIGENT_PRIVATE_VALUE_KEY_VERSION": "2",
            "MINIGENT_PRIVATE_VALUE_TTL_SECONDS": "60",
        }
    )

    assert isinstance(store, SQLiteEncryptedPrivateValueStore)
    store.add("tenant", "thread", {"ref": "value"})
    assert store.render_for_user("tenant", "thread", "{{pii:name:ref}}") == "value"


def test_encrypted_private_value_store_fails_closed_with_wrong_key(tmp_path: Path) -> None:
    db_path = tmp_path / "private.db"
    first = SQLiteEncryptedPrivateValueStore(db_path, KEY)
    first.add("tenant", "thread", {"ref": "private value"})
    with pytest.raises(RuntimeError, match="could not be opened"):
        SQLiteEncryptedPrivateValueStore(db_path, bytes(reversed(KEY)))


def test_encrypted_private_value_store_authenticates_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "private.db"
    store = SQLiteEncryptedPrivateValueStore(db_path, KEY)
    store.add(
        "tenant",
        "thread",
        {"ref": "private value"},
        kinds={"ref": "name"},
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE private_values SET kind = 'email' WHERE reference = 'ref'")
        connection.commit()

    with pytest.raises(HTTPException, match="authentication failed"):
        store.render_for_user("tenant", "thread", "{{pii:name:ref}}")


def test_encrypted_private_value_store_expires_and_clears_values(tmp_path: Path) -> None:
    now = [100.0]
    store = SQLiteEncryptedPrivateValueStore(
        tmp_path / "private.db",
        KEY,
        ttl_seconds=5,
        clock=lambda: now[0],
    )
    placeholder = "{{pii:name:ref}}"
    store.add("tenant", "thread", {"ref": "private value"})
    now[0] = 106.0

    assert store.render_for_user("tenant", "thread", placeholder) == placeholder
    with pytest.raises(HTTPException, match="missing or expired"):
        store.resolve_for_tool("tenant", "thread", placeholder)
    store.add("tenant", "thread", {"ref": "private value"})
    store.clear_thread("tenant", "thread")
    assert store.render_for_user("tenant", "thread", placeholder) == placeholder


def test_encrypted_private_value_store_enforces_collision_and_limits(tmp_path: Path) -> None:
    store = SQLiteEncryptedPrivateValueStore(
        tmp_path / "private.db",
        KEY,
        max_refs_per_thread=1,
        max_value_chars=5,
    )
    store.add("tenant", "thread", {"ref": "value"})

    with pytest.raises(HTTPException, match="collision"):
        store.add("tenant", "thread", {"ref": "other"})
    with pytest.raises(HTTPException, match="reference limit"):
        store.add("tenant", "thread", {"other": "value"})
    with pytest.raises(HTTPException, match="character limit"):
        store.add("tenant", "other-thread", {"ref": "too long"})
