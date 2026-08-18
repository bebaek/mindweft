from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Protocol
from uuid import uuid4

from fastapi import HTTPException

from app.models import ToolCall
from mindweft_config.unified_config import normalize_mindweft_env

DEFAULT_CONSENT_REQUEST_TTL_SECONDS = 600.0
DEFAULT_CONSENT_GRANT_TTL_SECONDS = 300.0
DEFAULT_CONSENT_AUDIT_TTL_SECONDS = 2_592_000.0
DEFAULT_CONSENT_MAX_AUDIT_RECORDS_PER_SCOPE = 1_000
PRIVATE_CONSENT_DB_PATH_ENV = "MINIGENT_PRIVATE_CONSENT_DB_PATH"
PRIVATE_CONSENT_ENCRYPTION_KEY_ENV = "MINIGENT_PRIVATE_CONSENT_ENCRYPTION_KEY"
PRIVATE_CONSENT_ENCRYPTION_KEYS_ENV = "MINIGENT_PRIVATE_CONSENT_ENCRYPTION_KEYS"
PRIVATE_CONSENT_KEY_VERSION_ENV = "MINIGENT_PRIVATE_CONSENT_KEY_VERSION"
PRIVATE_CONSENT_REENCRYPT_ON_STARTUP_ENV = "MINIGENT_PRIVATE_CONSENT_REENCRYPT_ON_STARTUP"
PRIVATE_CONSENT_REQUEST_TTL_ENV = "MINIGENT_PRIVATE_CONSENT_REQUEST_TTL_SECONDS"
PRIVATE_CONSENT_GRANT_TTL_ENV = "MINIGENT_PRIVATE_CONSENT_GRANT_TTL_SECONDS"
PRIVATE_CONSENT_AUDIT_TTL_ENV = "MINIGENT_PRIVATE_CONSENT_AUDIT_TTL_SECONDS"
PRIVATE_CONSENT_MAX_AUDIT_RECORDS_ENV = "MINIGENT_PRIVATE_CONSENT_MAX_AUDIT_RECORDS_PER_SCOPE"


@dataclass(frozen=True, order=True)
class PrivateValueDisclosure:
    path: str
    kind: str
    reference: str


@dataclass
class PrivateValueConsentRequest:
    consent_id: str
    tenant_id: str
    user_id: str
    thread_id: str
    tool_name: str
    argument_fingerprint: str
    disclosures: tuple[PrivateValueDisclosure, ...]
    status: str
    created_at: float
    expires_at: float
    one_shot: bool = True

    def public_dict(self) -> dict[str, object]:
        grouped: dict[tuple[str, str], int] = {}
        for disclosure in self.disclosures:
            key = (disclosure.path, disclosure.kind)
            grouped[key] = grouped.get(key, 0) + 1
        return {
            "consent_id": self.consent_id,
            "thread_id": self.thread_id,
            "tool_name": self.tool_name,
            "argument_fingerprint": self.argument_fingerprint,
            "status": self.status,
            "one_shot": self.one_shot,
            "expires_at": self.expires_at,
            "disclosures": [
                {"path": path, "kind": kind, "count": count}
                for (path, kind), count in sorted(grouped.items())
            ],
        }


@dataclass(frozen=True)
class PrivateValueDisclosureAuditRecord:
    event: str
    tenant_id: str
    user_id: str
    thread_id: str
    consent_id: str
    tool_name: str
    argument_fingerprint: str
    disclosures: tuple[PrivateValueDisclosure, ...]
    occurred_at: float

    def public_dict(self) -> dict[str, object]:
        return {
            "event": self.event,
            "consent_id": self.consent_id,
            "thread_id": self.thread_id,
            "tool_name": self.tool_name,
            "argument_fingerprint": self.argument_fingerprint,
            "occurred_at": self.occurred_at,
            "disclosures": [
                {
                    "path": item.path,
                    "kind": item.kind,
                    "reference": item.reference,
                }
                for item in self.disclosures
            ],
        }


@dataclass(frozen=True)
class PendingPrivateToolAction:
    tenant_id: str
    user_id: str
    thread_id: str
    tool_call: ToolCall


class PrivateValueConsentStore(Protocol):
    def authorize_or_request(
        self,
        *,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        tool_name: str,
        argument_fingerprint: str,
        disclosures: tuple[PrivateValueDisclosure, ...],
    ) -> None: ...

    def decide(
        self,
        *,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        consent_id: str,
        approve: bool,
        one_shot: bool = True,
    ) -> dict[str, object]: ...

    def pending(
        self, *, tenant_id: str, user_id: str, thread_id: str
    ) -> list[dict[str, object]]: ...

    def audit_records(
        self, *, tenant_id: str, user_id: str, thread_id: str
    ) -> list[dict[str, object]]: ...

    def save_pending_action(self, consent_id: str, action: PendingPrivateToolAction) -> None: ...

    def get_pending_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> PendingPrivateToolAction | None: ...

    def claim_pending_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> PendingPrivateToolAction | None: ...

    def action_statuses(
        self, *, tenant_id: str, user_id: str, thread_id: str
    ) -> list[dict[str, object]]: ...

    def discard_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> dict[str, object]: ...

    def delete_pending_action(self, consent_id: str) -> None: ...

    def clear_thread(self, tenant_id: str, thread_id: str) -> None: ...


class InMemoryPrivateValueConsentStore:
    """Thread-scoped pending requests, one-shot grants, and redacted disclosure audit data."""

    def __init__(
        self,
        *,
        request_ttl_seconds: float = DEFAULT_CONSENT_REQUEST_TTL_SECONDS,
        grant_ttl_seconds: float = DEFAULT_CONSENT_GRANT_TTL_SECONDS,
        audit_ttl_seconds: float = DEFAULT_CONSENT_AUDIT_TTL_SECONDS,
        max_audit_records_per_scope: int = DEFAULT_CONSENT_MAX_AUDIT_RECORDS_PER_SCOPE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._request_ttl_seconds = request_ttl_seconds
        self._grant_ttl_seconds = grant_ttl_seconds
        if audit_ttl_seconds <= 0:
            raise ValueError("private consent audit TTL must be positive")
        if max_audit_records_per_scope < 1:
            raise ValueError("private consent audit record limit must be positive")
        self._audit_ttl_seconds = audit_ttl_seconds
        self._max_audit_records_per_scope = max_audit_records_per_scope
        self._clock = clock
        self._requests: dict[str, PrivateValueConsentRequest] = {}
        self._audit: list[PrivateValueDisclosureAuditRecord] = []
        self._pending_actions: dict[str, PendingPrivateToolAction] = {}
        self._pending_action_states: dict[str, str] = {}
        self._lock = RLock()

    def authorize_or_request(
        self,
        *,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        tool_name: str,
        argument_fingerprint: str,
        disclosures: tuple[PrivateValueDisclosure, ...],
    ) -> None:
        normalized = tuple(sorted(set(disclosures)))
        now = self._clock()
        with self._lock:
            self._expire(now)
            matching = [
                request
                for request in self._requests.values()
                if request.tenant_id == tenant_id
                and request.user_id == user_id
                and request.thread_id == thread_id
                and request.tool_name == tool_name
                and request.argument_fingerprint == argument_fingerprint
                and request.disclosures == normalized
            ]
            approved = next(
                (request for request in matching if request.status == "approved"),
                None,
            )
            denied = next(
                (request for request in matching if request.status == "denied"),
                None,
            )
            if denied is not None:
                raise HTTPException(
                    status_code=403,
                    detail="Tool action was denied by the user",
                )
            if approved is not None:
                if approved.one_shot:
                    approved.status = "consumed"
                self._record("disclosed" if approved.disclosures else "authorized", approved, now)
                return
            pending = next(
                (request for request in matching if request.status == "pending"),
                None,
            )
            if pending is None:
                pending = PrivateValueConsentRequest(
                    consent_id=str(uuid4()),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    tool_name=tool_name,
                    argument_fingerprint=argument_fingerprint,
                    disclosures=normalized,
                    status="pending",
                    created_at=now,
                    expires_at=now + self._request_ttl_seconds,
                )
                self._requests[pending.consent_id] = pending
                self._record("requested", pending, now)
            raise HTTPException(
                status_code=428,
                detail={
                    "message": (
                        "Private-value disclosure requires user approval"
                        if normalized
                        else "Tool action requires user approval"
                    ),
                    **pending.public_dict(),
                },
            )

    def decide(
        self,
        *,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        consent_id: str,
        approve: bool,
        one_shot: bool = True,
    ) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            request = self._owned_request(tenant_id, user_id, thread_id, consent_id)
            if request.status != "pending":
                raise HTTPException(status_code=409, detail="Consent request is not pending")
            request.status = "approved" if approve else "denied"
            request.one_shot = one_shot
            request.expires_at = now + self._grant_ttl_seconds
            self._record(request.status, request, now)
            if not approve:
                self._pending_actions.pop(consent_id, None)
                self._pending_action_states.pop(consent_id, None)
            return request.public_dict()

    def pending(self, *, tenant_id: str, user_id: str, thread_id: str) -> list[dict[str, object]]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            return [
                request.public_dict()
                for request in self._requests.values()
                if request.tenant_id == tenant_id
                and request.user_id == user_id
                and request.thread_id == thread_id
                and request.status == "pending"
            ]

    def audit_records(
        self, *, tenant_id: str, user_id: str, thread_id: str
    ) -> list[dict[str, object]]:
        with self._lock:
            self._prune_audit(self._clock())
            return [
                record.public_dict()
                for record in self._audit
                if record.tenant_id == tenant_id
                and record.user_id == user_id
                and record.thread_id == thread_id
            ]

    def save_pending_action(self, consent_id: str, action: PendingPrivateToolAction) -> None:
        with self._lock:
            self._pending_actions[consent_id] = action
            self._pending_action_states[consent_id] = "pending"

    def get_pending_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> PendingPrivateToolAction | None:
        with self._lock:
            action = self._pending_actions.get(consent_id)
            if (
                action is None
                or action.tenant_id != tenant_id
                or action.user_id != user_id
                or action.thread_id != thread_id
            ):
                return None
            return action

    def claim_pending_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> PendingPrivateToolAction | None:
        with self._lock:
            action = self.get_pending_action(
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=thread_id,
                consent_id=consent_id,
            )
            if action is None:
                return None
            if self._pending_action_states.get(consent_id) != "pending":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Pending private tool action was already claimed; its outcome may be unknown"
                    ),
                )
            self._pending_action_states[consent_id] = "executing"
            return action

    def action_statuses(
        self, *, tenant_id: str, user_id: str, thread_id: str
    ) -> list[dict[str, object]]:
        with self._lock:
            self._expire(self._clock())
            statuses: list[dict[str, object]] = []
            for consent_id, action in self._pending_actions.items():
                if (
                    action.tenant_id != tenant_id
                    or action.user_id != user_id
                    or action.thread_id != thread_id
                ):
                    continue
                request = self._requests.get(consent_id)
                statuses.append(
                    {
                        "consent_id": consent_id,
                        "thread_id": thread_id,
                        "tool_name": action.tool_call.name,
                        "state": self._pending_action_states.get(consent_id, "pending"),
                        "expires_at": request.expires_at if request is not None else None,
                    }
                )
            return statuses

    def discard_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> dict[str, object]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            action = self.get_pending_action(
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=thread_id,
                consent_id=consent_id,
            )
            if action is None:
                raise HTTPException(status_code=404, detail="Pending private tool action not found")
            state = self._pending_action_states.get(consent_id, "pending")
            request = self._owned_request(tenant_id, user_id, thread_id, consent_id)
            if request.status in {"pending", "approved"}:
                request.status = "denied"
                request.expires_at = now + self._grant_ttl_seconds
            self._record("discarded", request, now)
            self._pending_actions.pop(consent_id, None)
            self._pending_action_states.pop(consent_id, None)
            return {
                "consent_id": consent_id,
                "thread_id": thread_id,
                "tool_name": action.tool_call.name,
                "state": state,
                "discarded": True,
            }

    def delete_pending_action(self, consent_id: str) -> None:
        with self._lock:
            self._pending_actions.pop(consent_id, None)
            self._pending_action_states.pop(consent_id, None)

    def clear_thread(self, tenant_id: str, thread_id: str) -> None:
        with self._lock:
            consent_ids = [
                consent_id
                for consent_id, request in self._requests.items()
                if request.tenant_id == tenant_id and request.thread_id == thread_id
            ]
            for consent_id in consent_ids:
                self._requests.pop(consent_id, None)
                self._pending_actions.pop(consent_id, None)
                self._pending_action_states.pop(consent_id, None)
            self._audit = [
                record
                for record in self._audit
                if not (record.tenant_id == tenant_id and record.thread_id == thread_id)
            ]

    def _owned_request(
        self, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> PrivateValueConsentRequest:
        request = self._requests.get(consent_id)
        if (
            request is None
            or request.tenant_id != tenant_id
            or request.user_id != user_id
            or request.thread_id != thread_id
        ):
            raise HTTPException(status_code=404, detail="Consent request not found")
        return request

    def _expire(self, now: float) -> None:
        for request in self._requests.values():
            if request.status in {"pending", "approved", "denied", "consumed"} and (
                request.expires_at <= now
            ):
                request.status = "expired"
                self._pending_actions.pop(request.consent_id, None)
                self._pending_action_states.pop(request.consent_id, None)
                self._record("expired", request, now)

    def _record(self, event: str, request: PrivateValueConsentRequest, occurred_at: float) -> None:
        self._audit.append(
            PrivateValueDisclosureAuditRecord(
                event=event,
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                thread_id=request.thread_id,
                consent_id=request.consent_id,
                tool_name=request.tool_name,
                argument_fingerprint=request.argument_fingerprint,
                disclosures=request.disclosures,
                occurred_at=occurred_at,
            )
        )
        self._prune_audit(occurred_at)

    def _prune_audit(self, now: float) -> None:
        cutoff = now - self._audit_ttl_seconds
        retained: list[PrivateValueDisclosureAuditRecord] = []
        scope_counts: dict[tuple[str, str, str], int] = {}
        for record in reversed(self._audit):
            if record.occurred_at <= cutoff:
                continue
            scope = (record.tenant_id, record.user_id, record.thread_id)
            count = scope_counts.get(scope, 0)
            if count >= self._max_audit_records_per_scope:
                continue
            scope_counts[scope] = count + 1
            retained.append(record)
        self._audit = list(reversed(retained))


def build_private_value_consent_store_from_env(
    env: Mapping[str, str] | None = None,
) -> PrivateValueConsentStore:
    lookup = normalize_mindweft_env(dict(os.environ if env is None else env))
    if not lookup.get(PRIVATE_CONSENT_DB_PATH_ENV, "").strip():
        return InMemoryPrivateValueConsentStore()
    from app.private_consent_sqlite import SQLiteEncryptedPrivateValueConsentStore

    return SQLiteEncryptedPrivateValueConsentStore.from_env(lookup)
