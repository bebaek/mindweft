from __future__ import annotations

import base64
import sqlite3

import pytest
from fastapi import HTTPException

from app.models import ToolCall
from app.private_consent_sqlite import SQLiteEncryptedPrivateValueConsentStore
from app.private_consents import (
    PendingPrivateToolAction,
    PrivateValueDisclosure,
    build_private_value_consent_store_from_env,
)

KEY = b"c" * 32
ROTATED_KEY = b"d" * 32
DISCLOSURES = (
    PrivateValueDisclosure(path="recipient.email", kind="email", reference="private-email-ref"),
)


def _request(store: SQLiteEncryptedPrivateValueConsentStore) -> None:
    store.authorize_or_request(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        tool_name="trusted.send",
        argument_fingerprint="fingerprint-1",
        disclosures=DISCLOSURES,
    )


def test_factory_defaults_to_memory() -> None:
    store = build_private_value_consent_store_from_env({})
    assert store.__class__.__name__ == "InMemoryPrivateValueConsentStore"


def test_encrypted_consent_and_pending_action_survive_restart(tmp_path) -> None:
    db_path = tmp_path / "private-consents.db"
    first = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(first)
    consent_id = str(
        first.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")[0]["consent_id"]
    )
    first.save_pending_action(
        consent_id,
        PendingPrivateToolAction(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            tool_call=ToolCall(
                id="call-1",
                name="trusted.send",
                arguments={"recipient": {"email": "{{pii:email:private-email-ref}}"}},
            ),
        ),
    )
    first.decide(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=consent_id,
        approve=True,
    )

    restarted = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 101.0)
    action = restarted.get_pending_action(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=consent_id,
    )
    assert action is not None
    assert action.tool_call.arguments["recipient"]["email"] == ("{{pii:email:private-email-ref}}")
    _request(restarted)
    assert [
        item["event"]
        for item in restarted.audit_records(
            tenant_id="tenant-1", user_id="user-1", thread_id="thread-1"
        )
    ] == ["requested", "approved", "disclosed"]


def test_encrypted_consent_store_persists_action_claim(tmp_path) -> None:
    db_path = tmp_path / "private-consents.db"
    first = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 100.0)
    action = PendingPrivateToolAction(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        tool_call=ToolCall(name="trusted.send", arguments={"body": "protected"}),
    )
    first.save_pending_action("consent-1", action)
    assert (
        first.claim_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id="consent-1",
        )
        == action
    )

    restarted = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 101.0)
    with pytest.raises(HTTPException, match="already claimed") as exc_info:
        restarted.claim_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id="consent-1",
        )
    assert exc_info.value.status_code == 409


def test_encrypted_consent_store_lists_and_discards_redacted_actions(tmp_path) -> None:
    db_path = tmp_path / "private-consents.db"
    store = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(store)
    consent_id = str(
        store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")[0]["consent_id"]
    )
    store.save_pending_action(
        consent_id,
        PendingPrivateToolAction(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            tool_call=ToolCall(name="trusted.send", arguments={"body": "secret body"}),
        ),
    )

    statuses = store.action_statuses(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    assert statuses[0]["state"] == "pending"
    assert statuses[0]["tool_name"] == "trusted.send"
    assert "secret body" not in str(statuses)
    assert (
        store.discard_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id=consent_id,
        )["discarded"]
        is True
    )
    assert store.action_statuses(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1") == []
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_private_tool_actions").fetchone()[0]
            == 0
        )


def test_encrypted_consent_store_expires_claimed_action_with_consumed_grant(tmp_path) -> None:
    now = [100.0]
    db_path = tmp_path / "private-consents.db"
    store = SQLiteEncryptedPrivateValueConsentStore(
        db_path,
        KEY,
        grant_ttl_seconds=5,
        clock=lambda: now[0],
    )
    with pytest.raises(HTTPException):
        _request(store)
    consent_id = str(
        store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")[0]["consent_id"]
    )
    action = PendingPrivateToolAction(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        tool_call=ToolCall(name="trusted.send"),
    )
    store.save_pending_action(consent_id, action)
    store.decide(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=consent_id,
        approve=True,
    )
    assert (
        store.claim_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id=consent_id,
        )
        == action
    )
    _request(store)
    now[0] = 106.0

    assert store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1") == []
    assert (
        store.get_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id=consent_id,
        )
        is None
    )
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM pending_private_tool_actions").fetchone()[0]
            == 0
        )


def test_encrypted_consent_store_authenticates_action_claim_state(tmp_path) -> None:
    db_path = tmp_path / "private-consents.db"
    store = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 100.0)
    store.save_pending_action(
        "consent-1",
        PendingPrivateToolAction(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            tool_call=ToolCall(name="trusted.send"),
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE pending_private_tool_actions SET state = 'executing' WHERE consent_id = ?",
            ("consent-1",),
        )
        connection.commit()

    with pytest.raises(HTTPException, match="authentication failed"):
        store.claim_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id="consent-1",
        )


def test_encrypted_consent_database_contains_no_sensitive_payload_text(tmp_path) -> None:
    db_path = tmp_path / "private-consents.db"
    store = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(store)
    consent_id = str(
        store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")[0]["consent_id"]
    )
    store.save_pending_action(
        consent_id,
        PendingPrivateToolAction(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            tool_call=ToolCall(
                name="trusted.send",
                arguments={"body": "sensitive action body"},
            ),
        ),
    )

    contents = db_path.read_bytes()
    assert b"private-email-ref" not in contents
    assert b"trusted.send" not in contents
    assert b"sensitive action body" not in contents


def test_encrypted_consent_store_authenticates_mutable_state(tmp_path) -> None:
    db_path = tmp_path / "private-consents.db"
    store = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(store)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE private_consent_requests SET status = 'approved'")
        connection.commit()

    with pytest.raises(HTTPException, match="authentication failed"):
        _request(store)


def test_encrypted_consent_store_rejects_wrong_key(tmp_path) -> None:
    db_path = tmp_path / "private-consents.db"
    store = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(store)

    with pytest.raises(RuntimeError, match="configured key"):
        SQLiteEncryptedPrivateValueConsentStore(db_path, b"x" * 32, clock=lambda: 100.0)


def test_encrypted_consent_store_expires_action_and_clear_thread(tmp_path) -> None:
    now = [100.0]
    store = SQLiteEncryptedPrivateValueConsentStore(
        tmp_path / "private-consents.db",
        KEY,
        request_ttl_seconds=5,
        grant_ttl_seconds=5,
        clock=lambda: now[0],
    )
    with pytest.raises(HTTPException):
        _request(store)
    consent_id = str(
        store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")[0]["consent_id"]
    )
    store.save_pending_action(
        consent_id,
        PendingPrivateToolAction(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            tool_call=ToolCall(name="trusted.send"),
        ),
    )
    now[0] = 106.0

    assert store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1") == []
    assert (
        store.get_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id=consent_id,
        )
        is None
    )
    store.clear_thread("tenant-1", "thread-1")
    assert store.audit_records(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1") == []


def test_encrypted_consent_store_bounds_and_expires_audit_records(tmp_path) -> None:
    now = [100.0]
    db_path = tmp_path / "private-consents.db"
    store = SQLiteEncryptedPrivateValueConsentStore(
        db_path,
        KEY,
        audit_ttl_seconds=5,
        max_audit_records_per_scope=2,
        clock=lambda: now[0],
    )
    with pytest.raises(HTTPException):
        _request(store)
    consent_id = str(
        store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")[0]["consent_id"]
    )
    store.decide(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=consent_id,
        approve=True,
    )
    _request(store)

    assert [
        record["event"]
        for record in store.audit_records(
            tenant_id="tenant-1", user_id="user-1", thread_id="thread-1"
        )
    ] == ["approved", "disclosed"]
    now[0] = 106.0
    assert store.audit_records(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1") == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM private_consent_audit").fetchone()[0] == 0


def test_encrypted_consent_store_rotates_all_record_types(tmp_path) -> None:
    db_path = tmp_path / "private-consents.db"
    first = SQLiteEncryptedPrivateValueConsentStore(db_path, KEY, clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(first)
    consent_id = str(
        first.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")[0]["consent_id"]
    )
    first.save_pending_action(
        consent_id,
        PendingPrivateToolAction(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            tool_call=ToolCall(name="trusted.send", arguments={"body": "protected"}),
        ),
    )
    first.decide(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=consent_id,
        approve=True,
    )

    rotating = SQLiteEncryptedPrivateValueConsentStore(
        db_path,
        ROTATED_KEY,
        key_version=2,
        decryption_keys={1: KEY},
        clock=lambda: 101.0,
    )
    assert rotating.rotate_to_active_key() == 4
    with sqlite3.connect(db_path) as connection:
        versions = {
            str(table): connection.execute(
                f"SELECT DISTINCT key_version FROM {table}"  # noqa: S608 - fixed test table names
            ).fetchall()
            for table in (
                "private_consent_requests",
                "pending_private_tool_actions",
                "private_consent_audit",
            )
        }
    assert all(rows == [(2,)] for rows in versions.values())

    restarted = SQLiteEncryptedPrivateValueConsentStore(
        db_path, ROTATED_KEY, key_version=2, clock=lambda: 101.0
    )
    assert (
        restarted.get_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id=consent_id,
        )
        is not None
    )
    assert (
        len(restarted.audit_records(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1"))
        == 2
    )


def test_factory_builds_encrypted_store(tmp_path) -> None:
    encoded_key = base64.urlsafe_b64encode(KEY).decode()
    store = build_private_value_consent_store_from_env(
        {
            "MINIGENT_PRIVATE_CONSENT_DB_PATH": str(tmp_path / "private-consents.db"),
            "MINIGENT_PRIVATE_CONSENT_ENCRYPTION_KEY": encoded_key,
        }
    )
    assert isinstance(store, SQLiteEncryptedPrivateValueConsentStore)
