from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path
from threading import RLock
from typing import Any, cast
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from app.models import ToolCall
from app.private_consents import (
    DEFAULT_CONSENT_AUDIT_TTL_SECONDS,
    DEFAULT_CONSENT_GRANT_TTL_SECONDS,
    DEFAULT_CONSENT_MAX_AUDIT_RECORDS_PER_SCOPE,
    DEFAULT_CONSENT_REQUEST_TTL_SECONDS,
    PRIVATE_CONSENT_AUDIT_TTL_ENV,
    PRIVATE_CONSENT_DB_PATH_ENV,
    PRIVATE_CONSENT_ENCRYPTION_KEY_ENV,
    PRIVATE_CONSENT_ENCRYPTION_KEYS_ENV,
    PRIVATE_CONSENT_GRANT_TTL_ENV,
    PRIVATE_CONSENT_KEY_VERSION_ENV,
    PRIVATE_CONSENT_MAX_AUDIT_RECORDS_ENV,
    PRIVATE_CONSENT_REENCRYPT_ON_STARTUP_ENV,
    PRIVATE_CONSENT_REQUEST_TTL_ENV,
    PendingPrivateToolAction,
    PrivateValueConsentRequest,
    PrivateValueDisclosure,
    PrivateValueDisclosureAuditRecord,
)
from app.private_keyring import load_encryption_keyring, parse_boolean
from minigent_config.unified_config import normalize_mindweft_env

_NONCE_BYTES = 12


class SQLiteEncryptedPrivateValueConsentStore:
    """Restart-safe consent, audit, and pending-action storage encrypted with AES-256-GCM."""

    def __init__(
        self,
        db_path: str | Path,
        encryption_key: bytes,
        *,
        key_version: int = 1,
        decryption_keys: Mapping[int, bytes] | None = None,
        reencrypt_on_startup: bool = False,
        request_ttl_seconds: float = DEFAULT_CONSENT_REQUEST_TTL_SECONDS,
        grant_ttl_seconds: float = DEFAULT_CONSENT_GRANT_TTL_SECONDS,
        audit_ttl_seconds: float = DEFAULT_CONSENT_AUDIT_TTL_SECONDS,
        max_audit_records_per_scope: int = DEFAULT_CONSENT_MAX_AUDIT_RECORDS_PER_SCOPE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(encryption_key) != 32:
            raise ValueError("private consent encryption key must contain exactly 32 bytes")
        if key_version < 1:
            raise ValueError("private consent key version must be positive")
        if request_ttl_seconds <= 0 or grant_ttl_seconds <= 0:
            raise ValueError("private consent TTLs must be positive")
        if audit_ttl_seconds <= 0:
            raise ValueError("private consent audit TTL must be positive")
        if max_audit_records_per_scope < 1:
            raise ValueError("private consent audit record limit must be positive")
        if str(db_path) == ":memory:":
            raise ValueError("encrypted private consent DB must use a filesystem path")
        self._db_path = Path(db_path).expanduser()
        self._key_version = key_version
        keys = dict(decryption_keys or {})
        existing_active_key = keys.get(key_version)
        if existing_active_key is not None and existing_active_key != encryption_key:
            raise ValueError("active private consent key conflicts with decryption keyring")
        keys[key_version] = encryption_key
        for version, key in keys.items():
            if version < 1 or len(key) != 32:
                raise ValueError("private consent decryption keys must be versioned 32-byte keys")
        self._aesgcms = {version: AESGCM(key) for version, key in keys.items()}
        self._request_ttl_seconds = request_ttl_seconds
        self._grant_ttl_seconds = grant_ttl_seconds
        self._audit_ttl_seconds = audit_ttl_seconds
        self._max_audit_records_per_scope = max_audit_records_per_scope
        self._clock = clock
        self._lock = RLock()
        self._initialize()
        if reencrypt_on_startup:
            self.rotate_to_active_key()

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> SQLiteEncryptedPrivateValueConsentStore:
        lookup = normalize_mindweft_env(dict(os.environ if env is None else env))
        db_path = lookup.get(PRIVATE_CONSENT_DB_PATH_ENV, "").strip()
        if not db_path:
            raise RuntimeError(f"{PRIVATE_CONSENT_DB_PATH_ENV} is required")
        active_key, keyring, key_version = load_encryption_keyring(
            lookup,
            single_key_env=PRIVATE_CONSENT_ENCRYPTION_KEY_ENV,
            keyring_env=PRIVATE_CONSENT_ENCRYPTION_KEYS_ENV,
            key_version_env=PRIVATE_CONSENT_KEY_VERSION_ENV,
            database_env=PRIVATE_CONSENT_DB_PATH_ENV,
        )
        return cls(
            db_path,
            active_key,
            key_version=key_version,
            decryption_keys=keyring,
            reencrypt_on_startup=parse_boolean(
                lookup.get(PRIVATE_CONSENT_REENCRYPT_ON_STARTUP_ENV, ""),
                PRIVATE_CONSENT_REENCRYPT_ON_STARTUP_ENV,
            ),
            request_ttl_seconds=_positive_float(
                lookup.get(PRIVATE_CONSENT_REQUEST_TTL_ENV, ""),
                PRIVATE_CONSENT_REQUEST_TTL_ENV,
                DEFAULT_CONSENT_REQUEST_TTL_SECONDS,
            ),
            grant_ttl_seconds=_positive_float(
                lookup.get(PRIVATE_CONSENT_GRANT_TTL_ENV, ""),
                PRIVATE_CONSENT_GRANT_TTL_ENV,
                DEFAULT_CONSENT_GRANT_TTL_SECONDS,
            ),
            audit_ttl_seconds=_positive_float(
                lookup.get(PRIVATE_CONSENT_AUDIT_TTL_ENV, ""),
                PRIVATE_CONSENT_AUDIT_TTL_ENV,
                DEFAULT_CONSENT_AUDIT_TTL_SECONDS,
            ),
            max_audit_records_per_scope=_positive_int(
                lookup.get(PRIVATE_CONSENT_MAX_AUDIT_RECORDS_ENV, ""),
                PRIVATE_CONSENT_MAX_AUDIT_RECORDS_ENV,
                DEFAULT_CONSENT_MAX_AUDIT_RECORDS_PER_SCOPE,
            ),
        )

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
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, now)
            matching = self._matching_requests(
                connection,
                tenant_id,
                user_id,
                thread_id,
                tool_name,
                argument_fingerprint,
                normalized,
            )
            denied = next((item for item in matching if item.status == "denied"), None)
            if denied is not None:
                connection.commit()
                raise HTTPException(
                    status_code=403,
                    detail="Tool action was denied by the user",
                )
            approved = next((item for item in matching if item.status == "approved"), None)
            if approved is not None:
                if approved.one_shot:
                    approved.status = "consumed"
                    self._update_request(connection, approved)
                self._record(
                    connection,
                    "disclosed" if approved.disclosures else "authorized",
                    approved,
                    now,
                )
                connection.commit()
                return
            pending = next((item for item in matching if item.status == "pending"), None)
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
                self._insert_request(connection, pending)
                self._record(connection, "requested", pending, now)
            connection.commit()
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
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, now)
            request = self._owned_request(connection, tenant_id, user_id, thread_id, consent_id)
            if request.status != "pending":
                connection.commit()
                raise HTTPException(status_code=409, detail="Consent request is not pending")
            request.status = "approved" if approve else "denied"
            request.one_shot = one_shot
            request.expires_at = now + self._grant_ttl_seconds
            self._update_request(connection, request)
            self._record(connection, request.status, request, now)
            if not approve:
                connection.execute(
                    "DELETE FROM pending_private_tool_actions WHERE consent_id = ?", (consent_id,)
                )
            connection.commit()
            return request.public_dict()

    def pending(self, *, tenant_id: str, user_id: str, thread_id: str) -> list[dict[str, object]]:
        now = self._clock()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, now)
            rows = connection.execute(
                """
                SELECT consent_id, tenant_id, user_id, thread_id, status, created_at,
                       expires_at, one_shot, nonce, ciphertext, key_version
                FROM private_consent_requests
                WHERE tenant_id = ? AND user_id = ? AND thread_id = ? AND status = 'pending'
                ORDER BY created_at
                """,
                (tenant_id, user_id, thread_id),
            ).fetchall()
            connection.commit()
        return [self._request_from_row(row).public_dict() for row in rows]

    def audit_records(
        self, *, tenant_id: str, user_id: str, thread_id: str
    ) -> list[dict[str, object]]:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune_audit(connection, self._clock())
            rows = connection.execute(
                """
                SELECT event, tenant_id, user_id, thread_id, consent_id, occurred_at,
                       nonce, ciphertext, key_version
                FROM private_consent_audit
                WHERE tenant_id = ? AND user_id = ? AND thread_id = ?
                ORDER BY audit_id
                """,
                (tenant_id, user_id, thread_id),
            ).fetchall()
            connection.commit()
        return [self._audit_from_row(row).public_dict() for row in rows]

    def save_pending_action(self, consent_id: str, action: PendingPrivateToolAction) -> None:
        payload = {
            "state": "pending",
            "tool_call": action.tool_call.model_dump(mode="json"),
        }
        nonce, ciphertext = self._encrypt(
            "action", action.tenant_id, action.user_id, action.thread_id, consent_id, payload
        )
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO pending_private_tool_actions (
                    consent_id, tenant_id, user_id, thread_id, state,
                    nonce, ciphertext, key_version
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(consent_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    user_id = excluded.user_id,
                    thread_id = excluded.thread_id,
                    state = excluded.state,
                    nonce = excluded.nonce,
                    ciphertext = excluded.ciphertext,
                    key_version = excluded.key_version
                """,
                (
                    consent_id,
                    action.tenant_id,
                    action.user_id,
                    action.thread_id,
                    nonce,
                    ciphertext,
                    self._key_version,
                ),
            )
            connection.commit()

    def get_pending_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> PendingPrivateToolAction | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT state, nonce, ciphertext, key_version
                FROM pending_private_tool_actions
                WHERE consent_id = ? AND tenant_id = ? AND user_id = ? AND thread_id = ?
                """,
                (consent_id, tenant_id, user_id, thread_id),
            ).fetchone()
        if row is None:
            return None
        tool_call, _state = self._decrypt_action(
            tenant_id,
            user_id,
            thread_id,
            consent_id,
            str(row[0]),
            bytes(row[1]),
            bytes(row[2]),
            int(row[3]),
        )
        return PendingPrivateToolAction(
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            tool_call=tool_call,
        )

    def claim_pending_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> PendingPrivateToolAction | None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state, nonce, ciphertext, key_version
                FROM pending_private_tool_actions
                WHERE consent_id = ? AND tenant_id = ? AND user_id = ? AND thread_id = ?
                """,
                (consent_id, tenant_id, user_id, thread_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            tool_call, state = self._decrypt_action(
                tenant_id,
                user_id,
                thread_id,
                consent_id,
                str(row[0]),
                bytes(row[1]),
                bytes(row[2]),
                int(row[3]),
            )
            if state != "pending":
                connection.commit()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Pending private tool action was already claimed; its outcome may be unknown"
                    ),
                )
            nonce, ciphertext = self._encrypt(
                "action",
                tenant_id,
                user_id,
                thread_id,
                consent_id,
                {"state": "executing", "tool_call": tool_call.model_dump(mode="json")},
            )
            connection.execute(
                """
                UPDATE pending_private_tool_actions
                SET state = 'executing', nonce = ?, ciphertext = ?, key_version = ?
                WHERE consent_id = ?
                """,
                (nonce, ciphertext, self._key_version, consent_id),
            )
            connection.commit()
        return PendingPrivateToolAction(
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            tool_call=tool_call,
        )

    def _decrypt_action(
        self,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        consent_id: str,
        stored_state: str,
        nonce: bytes,
        ciphertext: bytes,
        key_version: int,
    ) -> tuple[ToolCall, str]:
        payload = self._decrypt(
            "action",
            tenant_id,
            user_id,
            thread_id,
            consent_id,
            nonce,
            ciphertext,
            key_version,
        )
        wrapped_tool_call = payload.get("tool_call")
        wrapped_state = payload.get("state")
        if wrapped_tool_call is None and wrapped_state is None:
            # Rows written before action claiming stored the ToolCall as the whole payload.
            if stored_state != "pending":
                raise HTTPException(
                    status_code=500,
                    detail="Pending private tool action authentication failed",
                )
            return ToolCall.model_validate(payload), "pending"
        if wrapped_state not in {"pending", "executing"} or wrapped_state != stored_state:
            raise HTTPException(
                status_code=500,
                detail="Pending private tool action authentication failed",
            )
        return ToolCall.model_validate(wrapped_tool_call), stored_state

    def action_statuses(
        self, *, tenant_id: str, user_id: str, thread_id: str
    ) -> list[dict[str, object]]:
        now = self._clock()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, now)
            rows = connection.execute(
                """
                SELECT action.consent_id, action.state, action.nonce, action.ciphertext,
                       action.key_version, request.expires_at
                FROM pending_private_tool_actions AS action
                LEFT JOIN private_consent_requests AS request
                    ON request.consent_id = action.consent_id
                WHERE action.tenant_id = ? AND action.user_id = ? AND action.thread_id = ?
                ORDER BY action.consent_id
                """,
                (tenant_id, user_id, thread_id),
            ).fetchall()
            connection.commit()
        statuses: list[dict[str, object]] = []
        for row in rows:
            consent_id = str(row[0])
            tool_call, state = self._decrypt_action(
                tenant_id,
                user_id,
                thread_id,
                consent_id,
                str(row[1]),
                bytes(row[2]),
                bytes(row[3]),
                int(row[4]),
            )
            statuses.append(
                {
                    "consent_id": consent_id,
                    "thread_id": thread_id,
                    "tool_name": tool_call.name,
                    "state": state,
                    "expires_at": float(row[5]) if row[5] is not None else None,
                }
            )
        return statuses

    def discard_action(
        self, *, tenant_id: str, user_id: str, thread_id: str, consent_id: str
    ) -> dict[str, object]:
        now = self._clock()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, now)
            row = connection.execute(
                """
                SELECT state, nonce, ciphertext, key_version
                FROM pending_private_tool_actions
                WHERE consent_id = ? AND tenant_id = ? AND user_id = ? AND thread_id = ?
                """,
                (consent_id, tenant_id, user_id, thread_id),
            ).fetchone()
            if row is None:
                connection.commit()
                raise HTTPException(status_code=404, detail="Pending private tool action not found")
            tool_call, state = self._decrypt_action(
                tenant_id,
                user_id,
                thread_id,
                consent_id,
                str(row[0]),
                bytes(row[1]),
                bytes(row[2]),
                int(row[3]),
            )
            request = self._owned_request(connection, tenant_id, user_id, thread_id, consent_id)
            if request.status in {"pending", "approved"}:
                request.status = "denied"
                request.expires_at = now + self._grant_ttl_seconds
                self._update_request(connection, request)
            self._record(connection, "discarded", request, now)
            connection.execute(
                "DELETE FROM pending_private_tool_actions WHERE consent_id = ?",
                (consent_id,),
            )
            connection.commit()
        return {
            "consent_id": consent_id,
            "thread_id": thread_id,
            "tool_name": tool_call.name,
            "state": state,
            "discarded": True,
        }

    def delete_pending_action(self, consent_id: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM pending_private_tool_actions WHERE consent_id = ?", (consent_id,)
            )
            connection.commit()

    def clear_thread(self, tenant_id: str, thread_id: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM pending_private_tool_actions WHERE tenant_id = ? AND thread_id = ?",
                (tenant_id, thread_id),
            )
            connection.execute(
                "DELETE FROM private_consent_audit WHERE tenant_id = ? AND thread_id = ?",
                (tenant_id, thread_id),
            )
            connection.execute(
                "DELETE FROM private_consent_requests WHERE tenant_id = ? AND thread_id = ?",
                (tenant_id, thread_id),
            )
            connection.commit()

    def _matching_requests(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        tool_name: str,
        argument_fingerprint: str,
        disclosures: tuple[PrivateValueDisclosure, ...],
    ) -> list[PrivateValueConsentRequest]:
        rows = connection.execute(
            """
            SELECT consent_id, tenant_id, user_id, thread_id, status, created_at,
                   expires_at, one_shot, nonce, ciphertext, key_version
            FROM private_consent_requests
            WHERE tenant_id = ? AND user_id = ? AND thread_id = ?
              AND status IN ('pending', 'approved', 'denied')
            """,
            (tenant_id, user_id, thread_id),
        ).fetchall()
        requests = [self._request_from_row(row) for row in rows]
        return [
            request
            for request in requests
            if request.tool_name == tool_name
            and request.argument_fingerprint == argument_fingerprint
            and request.disclosures == disclosures
        ]

    def _owned_request(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        consent_id: str,
    ) -> PrivateValueConsentRequest:
        row = connection.execute(
            """
            SELECT consent_id, tenant_id, user_id, thread_id, status, created_at,
                   expires_at, one_shot, nonce, ciphertext, key_version
            FROM private_consent_requests
            WHERE consent_id = ? AND tenant_id = ? AND user_id = ? AND thread_id = ?
            """,
            (consent_id, tenant_id, user_id, thread_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Consent request not found")
        return self._request_from_row(row)

    def _insert_request(
        self, connection: sqlite3.Connection, request: PrivateValueConsentRequest
    ) -> None:
        payload = self._request_payload(request)
        nonce, ciphertext = self._encrypt(
            "request",
            request.tenant_id,
            request.user_id,
            request.thread_id,
            request.consent_id,
            payload,
        )
        connection.execute(
            """
            INSERT INTO private_consent_requests (
                consent_id, tenant_id, user_id, thread_id, status, created_at,
                expires_at, one_shot, nonce, ciphertext, key_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.consent_id,
                request.tenant_id,
                request.user_id,
                request.thread_id,
                request.status,
                request.created_at,
                request.expires_at,
                int(request.one_shot),
                nonce,
                ciphertext,
                self._key_version,
            ),
        )

    def _update_request(
        self, connection: sqlite3.Connection, request: PrivateValueConsentRequest
    ) -> None:
        nonce, ciphertext = self._encrypt(
            "request",
            request.tenant_id,
            request.user_id,
            request.thread_id,
            request.consent_id,
            self._request_payload(request),
        )
        connection.execute(
            """
            UPDATE private_consent_requests
            SET status = ?, created_at = ?, expires_at = ?, one_shot = ?,
                nonce = ?, ciphertext = ?, key_version = ?
            WHERE consent_id = ?
            """,
            (
                request.status,
                request.created_at,
                request.expires_at,
                int(request.one_shot),
                nonce,
                ciphertext,
                self._key_version,
                request.consent_id,
            ),
        )

    @staticmethod
    def _request_payload(request: PrivateValueConsentRequest) -> dict[str, object]:
        return {
            "tool_name": request.tool_name,
            "argument_fingerprint": request.argument_fingerprint,
            "disclosures": [item.__dict__ for item in request.disclosures],
            "status": request.status,
            "created_at": request.created_at,
            "expires_at": request.expires_at,
            "one_shot": request.one_shot,
        }

    def _request_from_row(self, row: tuple[Any, ...]) -> PrivateValueConsentRequest:
        consent_id, tenant_id, user_id, thread_id = map(str, row[:4])
        payload = self._decrypt(
            "request",
            tenant_id,
            user_id,
            thread_id,
            consent_id,
            bytes(row[8]),
            bytes(row[9]),
            int(row[10]),
        )
        status = str(row[4])
        created_at = float(row[5])
        expires_at = float(row[6])
        one_shot = bool(row[7])
        if (
            payload.get("status") != status
            or payload.get("created_at") != created_at
            or payload.get("expires_at") != expires_at
            or payload.get("one_shot") is not one_shot
        ):
            raise HTTPException(
                status_code=500,
                detail="Private consent authentication failed",
            )
        return PrivateValueConsentRequest(
            consent_id=consent_id,
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            tool_name=str(payload["tool_name"]),
            argument_fingerprint=str(payload["argument_fingerprint"]),
            disclosures=tuple(
                PrivateValueDisclosure(**item)
                for item in cast(list[dict[str, str]], payload["disclosures"])
            ),
            status=status,
            created_at=created_at,
            expires_at=expires_at,
            one_shot=one_shot,
        )

    def _record(
        self,
        connection: sqlite3.Connection,
        event: str,
        request: PrivateValueConsentRequest,
        occurred_at: float,
    ) -> None:
        payload = {
            "tool_name": request.tool_name,
            "argument_fingerprint": request.argument_fingerprint,
            "disclosures": [item.__dict__ for item in request.disclosures],
        }
        nonce, ciphertext = self._encrypt(
            "audit",
            request.tenant_id,
            request.user_id,
            request.thread_id,
            request.consent_id,
            payload,
            event=event,
            occurred_at=occurred_at,
        )
        connection.execute(
            """
            INSERT INTO private_consent_audit (
                event, tenant_id, user_id, thread_id, consent_id, occurred_at,
                nonce, ciphertext, key_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event,
                request.tenant_id,
                request.user_id,
                request.thread_id,
                request.consent_id,
                occurred_at,
                nonce,
                ciphertext,
                self._key_version,
            ),
        )
        self._prune_audit(connection, occurred_at)

    def _audit_from_row(self, row: tuple[Any, ...]) -> PrivateValueDisclosureAuditRecord:
        event, tenant_id, user_id, thread_id, consent_id = map(str, row[:5])
        occurred_at = float(row[5])
        payload = self._decrypt(
            "audit",
            tenant_id,
            user_id,
            thread_id,
            consent_id,
            bytes(row[6]),
            bytes(row[7]),
            int(row[8]),
            event=event,
            occurred_at=occurred_at,
        )
        return PrivateValueDisclosureAuditRecord(
            event=event,
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            consent_id=consent_id,
            tool_name=str(payload["tool_name"]),
            argument_fingerprint=str(payload["argument_fingerprint"]),
            disclosures=tuple(
                PrivateValueDisclosure(**item)
                for item in cast(list[dict[str, str]], payload["disclosures"])
            ),
            occurred_at=occurred_at,
        )

    def _prune_audit(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            "DELETE FROM private_consent_audit WHERE occurred_at <= ?",
            (now - self._audit_ttl_seconds,),
        )
        connection.execute(
            """
            DELETE FROM private_consent_audit
            WHERE audit_id IN (
                SELECT audit_id
                FROM (
                    SELECT audit_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY tenant_id, user_id, thread_id
                               ORDER BY audit_id DESC
                           ) AS scope_rank
                    FROM private_consent_audit
                )
                WHERE scope_rank > ?
            )
            """,
            (self._max_audit_records_per_scope,),
        )

    def _expire(self, connection: sqlite3.Connection, now: float) -> None:
        rows = connection.execute(
            """
            SELECT consent_id, tenant_id, user_id, thread_id, status, created_at,
                   expires_at, one_shot, nonce, ciphertext, key_version
            FROM private_consent_requests
            WHERE status IN ('pending', 'approved', 'denied', 'consumed') AND expires_at <= ?
            """,
            (now,),
        ).fetchall()
        for row in rows:
            request = self._request_from_row(row)
            request.status = "expired"
            self._update_request(connection, request)
            connection.execute(
                "DELETE FROM pending_private_tool_actions WHERE consent_id = ?",
                (request.consent_id,),
            )
            self._record(connection, "expired", request, now)

    def _encrypt(
        self,
        record_type: str,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        consent_id: str,
        payload: object,
        *,
        event: str | None = None,
        occurred_at: float | None = None,
    ) -> tuple[bytes, bytes]:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aesgcms[self._key_version].encrypt(
            nonce,
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(),
            _associated_data(
                record_type,
                tenant_id,
                user_id,
                thread_id,
                consent_id,
                self._key_version,
                event=event,
                occurred_at=occurred_at,
            ),
        )
        return nonce, ciphertext

    def _decrypt(
        self,
        record_type: str,
        tenant_id: str,
        user_id: str,
        thread_id: str,
        consent_id: str,
        nonce: bytes,
        ciphertext: bytes,
        key_version: int,
        *,
        event: str | None = None,
        occurred_at: float | None = None,
    ) -> dict[str, object]:
        aesgcm = self._aesgcms.get(key_version)
        if aesgcm is None:
            raise HTTPException(
                status_code=500,
                detail="Private consent encryption key version is unavailable",
            )
        try:
            plaintext = aesgcm.decrypt(
                nonce,
                ciphertext,
                _associated_data(
                    record_type,
                    tenant_id,
                    user_id,
                    thread_id,
                    consent_id,
                    key_version,
                    event=event,
                    occurred_at=occurred_at,
                ),
            )
        except InvalidTag as exc:
            raise HTTPException(
                status_code=500,
                detail="Private consent authentication failed",
            ) from exc
        payload = json.loads(plaintext)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=500, detail="Private consent payload is invalid")
        return payload

    def rotate_to_active_key(self) -> int:
        """Re-encrypt consent requests, actions, and audit records with the active key."""
        rotated = 0
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, self._clock())
            request_rows = connection.execute(
                """
                SELECT consent_id, tenant_id, user_id, thread_id, status, created_at,
                       expires_at, one_shot, nonce, ciphertext, key_version
                FROM private_consent_requests WHERE key_version != ?
                """,
                (self._key_version,),
            ).fetchall()
            for row in request_rows:
                self._update_request(connection, self._request_from_row(row))
                rotated += 1

            action_rows = connection.execute(
                """
                SELECT consent_id, tenant_id, user_id, thread_id, state,
                       nonce, ciphertext, key_version
                FROM pending_private_tool_actions WHERE key_version != ?
                """,
                (self._key_version,),
            ).fetchall()
            for row in action_rows:
                consent_id, tenant_id, user_id, thread_id = map(str, row[:4])
                tool_call, state = self._decrypt_action(
                    tenant_id,
                    user_id,
                    thread_id,
                    consent_id,
                    str(row[4]),
                    bytes(row[5]),
                    bytes(row[6]),
                    int(row[7]),
                )
                payload = {"state": state, "tool_call": tool_call.model_dump(mode="json")}
                nonce, ciphertext = self._encrypt(
                    "action", tenant_id, user_id, thread_id, consent_id, payload
                )
                connection.execute(
                    """
                    UPDATE pending_private_tool_actions
                    SET nonce = ?, ciphertext = ?, key_version = ? WHERE consent_id = ?
                    """,
                    (nonce, ciphertext, self._key_version, consent_id),
                )
                rotated += 1

            audit_rows = connection.execute(
                """
                SELECT audit_id, event, tenant_id, user_id, thread_id, consent_id, occurred_at,
                       nonce, ciphertext, key_version
                FROM private_consent_audit WHERE key_version != ?
                """,
                (self._key_version,),
            ).fetchall()
            for row in audit_rows:
                audit_id = int(row[0])
                record = self._audit_from_row(row[1:])
                payload = {
                    "tool_name": record.tool_name,
                    "argument_fingerprint": record.argument_fingerprint,
                    "disclosures": [item.__dict__ for item in record.disclosures],
                }
                nonce, ciphertext = self._encrypt(
                    "audit",
                    record.tenant_id,
                    record.user_id,
                    record.thread_id,
                    record.consent_id,
                    payload,
                    event=record.event,
                    occurred_at=record.occurred_at,
                )
                connection.execute(
                    """
                    UPDATE private_consent_audit
                    SET nonce = ?, ciphertext = ?, key_version = ? WHERE audit_id = ?
                    """,
                    (nonce, ciphertext, self._key_version, audit_id),
                )
                rotated += 1
            connection.commit()
        return rotated

    def _initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS private_consent_requests (
                    consent_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    one_shot INTEGER NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    key_version INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_private_consent_scope
                    ON private_consent_requests(tenant_id, user_id, thread_id, status);
                CREATE INDEX IF NOT EXISTS idx_private_consent_expiry
                    ON private_consent_requests(expires_at);
                CREATE TABLE IF NOT EXISTS private_consent_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    consent_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    key_version INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_private_consent_audit_scope
                    ON private_consent_audit(tenant_id, user_id, thread_id, audit_id);
                CREATE TABLE IF NOT EXISTS pending_private_tool_actions (
                    consent_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    key_version INTEGER NOT NULL
                );
                """
            )
            action_columns = {
                str(column_row[1])
                for column_row in connection.execute(
                    "PRAGMA table_info(pending_private_tool_actions)"
                )
            }
            if "state" not in action_columns:
                connection.execute(
                    """
                    ALTER TABLE pending_private_tool_actions
                    ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'
                    """
                )
            connection.commit()
            versions = {
                int(version_row[0])
                for version_row in connection.execute(
                    """
                    SELECT key_version FROM private_consent_requests
                    UNION SELECT key_version FROM private_consent_audit
                    UNION SELECT key_version FROM pending_private_tool_actions
                    """
                )
            }
            missing_versions = versions - self._aesgcms.keys()
            if missing_versions:
                missing = ", ".join(str(version) for version in sorted(missing_versions))
                raise RuntimeError(
                    f"Encrypted private consent database requires unavailable key version(s): {missing}"
                )
            row = connection.execute(
                """
                SELECT consent_id, tenant_id, user_id, thread_id, status, created_at,
                       expires_at, one_shot, nonce, ciphertext, key_version
                FROM private_consent_requests LIMIT 1
                """
            ).fetchone()
            if row is not None:
                try:
                    self._request_from_row(row)
                except HTTPException as exc:
                    raise RuntimeError(
                        "Encrypted private consent database could not be opened with the configured key"
                    ) from exc
            self._expire(connection, self._clock())
            self._prune_audit(connection, self._clock())
            connection.commit()
        try:
            self._db_path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection


def _associated_data(
    record_type: str,
    tenant_id: str,
    user_id: str,
    thread_id: str,
    consent_id: str,
    key_version: int,
    *,
    event: str | None = None,
    occurred_at: float | None = None,
) -> bytes:
    return json.dumps(
        {
            "record_type": record_type,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "thread_id": thread_id,
            "consent_id": consent_id,
            "key_version": key_version,
            "event": event,
            "occurred_at": occurred_at,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _positive_float(raw: str, name: str, default: float) -> float:
    if not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


def _positive_int(raw: str, name: str, default: int) -> int:
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value
