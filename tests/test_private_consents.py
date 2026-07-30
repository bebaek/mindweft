from __future__ import annotations

from typing import cast

import pytest
from fastapi import HTTPException

from app.models import ToolCall
from app.private_consents import (
    InMemoryPrivateValueConsentStore,
    PendingPrivateToolAction,
    PrivateValueDisclosure,
)

DISCLOSURES = (PrivateValueDisclosure(path="recipient.email", kind="email", reference="email-ref"),)


def _request(store: InMemoryPrivateValueConsentStore) -> None:
    store.authorize_or_request(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        tool_name="trusted.send",
        argument_fingerprint="fingerprint-1",
        disclosures=DISCLOSURES,
    )


def test_consent_store_creates_redacted_pending_request() -> None:
    store = InMemoryPrivateValueConsentStore(clock=lambda: 100.0)

    with pytest.raises(HTTPException) as exc_info:
        _request(store)

    assert exc_info.value.status_code == 428
    pending = store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "trusted.send"
    assert pending[0]["disclosures"] == [{"path": "recipient.email", "kind": "email", "count": 1}]
    assert "email-ref" not in str(pending)


def test_consent_store_approves_mutation_without_private_disclosures() -> None:
    store = InMemoryPrivateValueConsentStore(clock=lambda: 100.0)
    arguments = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "thread_id": "thread-1",
        "tool_name": "private-contacts.contacts_delete",
        "argument_fingerprint": "delete-fingerprint",
        "disclosures": (),
    }

    with pytest.raises(HTTPException) as exc_info:
        store.authorize_or_request(**arguments)
    assert exc_info.value.status_code == 428
    detail = cast(dict[str, object], exc_info.value.detail)
    assert detail["message"] == "Tool action requires user approval"
    pending = store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    assert pending[0]["disclosures"] == []

    store.decide(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=str(pending[0]["consent_id"]),
        approve=True,
    )
    store.authorize_or_request(**arguments)
    audit = store.audit_records(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    assert [record["event"] for record in audit] == ["requested", "approved", "authorized"]


def test_consent_store_approves_and_consumes_one_shot_grant() -> None:
    store = InMemoryPrivateValueConsentStore(clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(store)
    pending = store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")

    decision = store.decide(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=str(pending[0]["consent_id"]),
        approve=True,
    )
    _request(store)

    assert decision["status"] == "approved"
    with pytest.raises(HTTPException) as exc_info:
        _request(store)
    assert exc_info.value.status_code == 428
    audit = store.audit_records(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    assert [record["event"] for record in audit] == [
        "requested",
        "approved",
        "disclosed",
        "requested",
    ]
    assert audit[2]["disclosures"] == [
        {"path": "recipient.email", "kind": "email", "reference": "email-ref"}
    ]


def test_consent_store_binds_grant_to_complete_argument_fingerprint() -> None:
    store = InMemoryPrivateValueConsentStore(clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(store)
    pending = store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    store.decide(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=str(pending[0]["consent_id"]),
        approve=True,
        one_shot=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        store.authorize_or_request(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            tool_name="trusted.send",
            argument_fingerprint="changed-fingerprint",
            disclosures=DISCLOSURES,
        )

    assert exc_info.value.status_code == 428


def test_consent_store_denial_blocks_matching_disclosure() -> None:
    store = InMemoryPrivateValueConsentStore(clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(store)
    pending = store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    store.decide(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=str(pending[0]["consent_id"]),
        approve=False,
    )

    with pytest.raises(HTTPException, match="denied by the user") as exc_info:
        _request(store)

    assert exc_info.value.status_code == 403


def test_consent_store_is_scoped_by_user_tenant_and_thread() -> None:
    store = InMemoryPrivateValueConsentStore(clock=lambda: 100.0)
    with pytest.raises(HTTPException):
        _request(store)
    pending = store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    consent_id = str(pending[0]["consent_id"])

    with pytest.raises(HTTPException, match="not found"):
        store.decide(
            tenant_id="tenant-1",
            user_id="other-user",
            thread_id="thread-1",
            consent_id=consent_id,
            approve=True,
        )
    with pytest.raises(HTTPException, match="not found"):
        store.decide(
            tenant_id="other-tenant",
            user_id="user-1",
            thread_id="thread-1",
            consent_id=consent_id,
            approve=True,
        )


def test_consent_store_expires_pending_requests() -> None:
    now = [100.0]
    store = InMemoryPrivateValueConsentStore(
        request_ttl_seconds=5,
        grant_ttl_seconds=5,
        clock=lambda: now[0],
    )
    with pytest.raises(HTTPException):
        _request(store)
    now[0] = 106.0

    assert store.pending(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1") == []
    audit = store.audit_records(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    assert [record["event"] for record in audit] == ["requested", "expired"]


def test_consent_store_claims_pending_action_only_once() -> None:
    store = InMemoryPrivateValueConsentStore(clock=lambda: 100.0)
    action = PendingPrivateToolAction(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        tool_call=ToolCall(name="trusted.send", arguments={"body": "protected"}),
    )
    store.save_pending_action("consent-1", action)

    assert (
        store.claim_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id="consent-1",
        )
        == action
    )
    with pytest.raises(HTTPException, match="already claimed") as exc_info:
        store.claim_pending_action(
            tenant_id="tenant-1",
            user_id="user-1",
            thread_id="thread-1",
            consent_id="consent-1",
        )
    assert exc_info.value.status_code == 409


def test_consent_store_lists_and_discards_redacted_actions() -> None:
    store = InMemoryPrivateValueConsentStore(clock=lambda: 100.0)
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
    assert statuses == [
        {
            "consent_id": consent_id,
            "thread_id": "thread-1",
            "tool_name": "trusted.send",
            "state": "pending",
            "expires_at": 700.0,
        }
    ]
    assert "secret body" not in str(statuses)
    discarded = store.discard_action(
        tenant_id="tenant-1",
        user_id="user-1",
        thread_id="thread-1",
        consent_id=consent_id,
    )
    assert discarded["discarded"] is True
    assert store.action_statuses(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1") == []
    with pytest.raises(HTTPException, match="denied by the user"):
        _request(store)
    audit = store.audit_records(tenant_id="tenant-1", user_id="user-1", thread_id="thread-1")
    assert [record["event"] for record in audit] == ["requested", "discarded"]


def test_consent_store_expires_claimed_action_with_consumed_grant() -> None:
    now = [100.0]
    store = InMemoryPrivateValueConsentStore(
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


def test_consent_store_bounds_and_expires_audit_records() -> None:
    now = [100.0]
    store = InMemoryPrivateValueConsentStore(
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
