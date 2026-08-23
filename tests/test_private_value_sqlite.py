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
ROTATED_KEY = bytes(reversed(range(32)))
ROTATED_KEY_B64 = base64.urlsafe_b64encode(ROTATED_KEY).decode()


def test_private_value_store_factory_defaults_to_memory() -> None:
    store = build_private_value_store_from_env({})

    assert isinstance(store, InMemoryPrivateValueStore)


def test_encrypted_private_value_store_copies_selected_references_with_original_expiry(
    tmp_path: Path,
) -> None:
    now = [100.0]
    store = SQLiteEncryptedPrivateValueStore(
        tmp_path / "private-copy.db",
        KEY,
        ttl_seconds=5,
        clock=lambda: now[0],
    )
    store.add(
        "tenant",
        "source",
        {"copied": "sensitive", "omitted": "other"},
        user_id="user",
        kinds={"copied": "email", "omitted": "phone"},
    )
    copied_placeholder = "{" * 2 + "pii:email:copied" + "}" * 2
    omitted_placeholder = "{" * 2 + "pii:phone:omitted" + "}" * 2
    now[0] = 103.0

    copied = store.copy_references(
        "tenant",
        "source",
        "child",
        {"copied", "missing"},
        user_id="user",
    )

    assert copied == 1
    assert all(
        b"sensitive" not in candidate.read_bytes()
        for candidate in tmp_path.glob("private-copy.db*")
    )
    assert (
        store.resolve_for_tool(
            "tenant",
            "child",
            copied_placeholder,
            user_id="user",
        )
        == "sensitive"
    )
    assert (
        store.render_for_user(
            "tenant",
            "child",
            omitted_placeholder,
            user_id="user",
        )
        == omitted_placeholder
    )
    assert (
        store.render_for_user(
            "tenant",
            "child",
            copied_placeholder,
            user_id="other-user",
        )
        == copied_placeholder
    )
    store.clear_thread("tenant", "source")
    assert (
        store.render_for_user(
            "tenant",
            "child",
            copied_placeholder,
            user_id="user",
        )
        == "sensitive"
    )
    now[0] = 106.0
    assert (
        store.render_for_user(
            "tenant",
            "child",
            copied_placeholder,
            user_id="user",
        )
        == copied_placeholder
    )


def test_encrypted_private_value_store_requires_key_when_configured(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY.*required"):
        build_private_value_store_from_env(
            {"MINDWEFT_PRIVATE_VALUE_DB_PATH": str(tmp_path / "private.db")}
        )


def test_encrypted_private_value_store_drops_unowned_legacy_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "private.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE private_values (
                tenant_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                reference TEXT NOT NULL,
                kind TEXT NOT NULL,
                nonce BLOB NOT NULL,
                ciphertext BLOB NOT NULL,
                key_version INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (tenant_id, thread_id, reference)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO private_values VALUES (
                'tenant', 'thread', 'legacy-ref', 'email', X'00', X'00', 1, 0, 9999999999
            )
            """
        )
        connection.commit()

    SQLiteEncryptedPrivateValueStore(db_path, KEY)

    with sqlite3.connect(db_path) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(private_values)")}
        count = connection.execute("SELECT COUNT(*) FROM private_values").fetchone()[0]
    assert "user_id" in columns
    assert count == 0


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
        user_id="user-1",
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
    assert (
        second.render_for_user("tenant-1", "thread-1", placeholder, user_id="user-1")
        == "private@example.com"
    )
    assert (
        second.resolve_for_tool("tenant-1", "thread-1", placeholder, user_id="user-1")
        == "private@example.com"
    )
    assert (
        second.render_for_user("tenant-1", "thread-1", placeholder, user_id="user-2") == placeholder
    )
    assert (
        second.render_for_user("tenant-2", "thread-1", placeholder, user_id="user-1") == placeholder
    )
    assert (
        second.render_for_user("tenant-1", "thread-2", placeholder, user_id="user-1") == placeholder
    )


def test_encrypted_private_value_store_binds_references_to_declared_kinds(
    tmp_path: Path,
) -> None:
    store = SQLiteEncryptedPrivateValueStore(tmp_path / "private.db", KEY)
    store.add(
        "tenant",
        "thread",
        {"ref": "private@example.com"},
        kinds={"ref": "email"},
    )

    assert store.render_for_user("tenant", "thread", "{{pii:phone:ref}}") == "{{pii:phone:ref}}"
    with pytest.raises(HTTPException, match="kind does not match"):
        store.validate_for_tool("tenant", "thread", "{{pii:phone:ref}}")
    with pytest.raises(HTTPException, match="kind does not match"):
        store.resolve_for_tool("tenant", "thread", "{{pii:phone:ref}}")
    with pytest.raises(HTTPException, match="kind collision"):
        store.add(
            "tenant",
            "thread",
            {"ref": "private@example.com"},
            kinds={"ref": "phone"},
        )

    # An update without kind metadata must not downgrade an existing binding.
    store.add("tenant", "thread", {"ref": "private@example.com"})
    with pytest.raises(HTTPException, match="kind does not match"):
        store.resolve_for_tool("tenant", "thread", "{{pii:phone:ref}}")


def test_encrypted_private_value_factory_reads_environment_mapping(tmp_path: Path) -> None:
    store = build_private_value_store_from_env(
        {
            "MINDWEFT_PRIVATE_VALUE_DB_PATH": str(tmp_path / "private.db"),
            "MINDWEFT_PRIVATE_VALUE_ENCRYPTION_KEY": KEY_B64,
            "MINDWEFT_PRIVATE_VALUE_KEY_VERSION": "2",
            "MINDWEFT_PRIVATE_VALUE_TTL_SECONDS": "60",
        }
    )

    assert isinstance(store, SQLiteEncryptedPrivateValueStore)
    store.add("tenant", "thread", {"ref": "value"})
    assert store.render_for_user("tenant", "thread", "{{pii:name:ref}}") == "value"


def test_encrypted_private_value_store_rotates_key_versions(tmp_path: Path) -> None:
    db_path = tmp_path / "private.db"
    placeholder = "{{pii:email:ref}}"
    first = SQLiteEncryptedPrivateValueStore(db_path, KEY)
    first.add("tenant", "thread", {"ref": "private@example.com"}, kinds={"ref": "email"})

    rotating = SQLiteEncryptedPrivateValueStore(
        db_path,
        ROTATED_KEY,
        key_version=2,
        decryption_keys={1: KEY},
    )
    assert rotating.render_for_user("tenant", "thread", placeholder) == "private@example.com"
    assert rotating.rotate_to_active_key() == 1

    with sqlite3.connect(db_path) as connection:
        versions = connection.execute("SELECT DISTINCT key_version FROM private_values").fetchall()
    assert versions == [(2,)]
    restarted = SQLiteEncryptedPrivateValueStore(db_path, ROTATED_KEY, key_version=2)
    assert restarted.render_for_user("tenant", "thread", placeholder) == "private@example.com"


def test_encrypted_private_value_factory_reencrypts_with_keyring(tmp_path: Path) -> None:
    db_path = tmp_path / "private.db"
    first = SQLiteEncryptedPrivateValueStore(db_path, KEY)
    first.add("tenant", "thread", {"ref": "private value"})
    keyring = f'{{"1":"{KEY_B64}","2":"{ROTATED_KEY_B64}"}}'

    rotated = build_private_value_store_from_env(
        {
            "MINDWEFT_PRIVATE_VALUE_DB_PATH": str(db_path),
            "MINDWEFT_PRIVATE_VALUE_ENCRYPTION_KEYS": keyring,
            "MINDWEFT_PRIVATE_VALUE_KEY_VERSION": "2",
            "MINDWEFT_PRIVATE_VALUE_REENCRYPT_ON_STARTUP": "true",
        }
    )

    assert rotated.render_for_user("tenant", "thread", "{{pii:name:ref}}") == "private value"
    SQLiteEncryptedPrivateValueStore(db_path, ROTATED_KEY, key_version=2)


def test_encrypted_private_value_store_fails_closed_with_wrong_key(tmp_path: Path) -> None:
    db_path = tmp_path / "wrong-key.db"
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
        store.validate_for_tool("tenant", "thread", placeholder)
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
