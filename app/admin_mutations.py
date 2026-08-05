"""Confirmation-bound admin mutations for authenticated admin chat."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.models import AuditRecord, TenantDomain

CONFIRMATION_TTL = timedelta(minutes=5)


class AdminMutationService:
    """Durable, single-use confirmation flow for the first admin-chat write."""

    def __init__(self, app: Any) -> None:
        self._app = app
        store = app.state.admin_store
        if store is None:
            raise HTTPException(status_code=409, detail="Admin store is not configured")
        self._db_path = store.db_path
        with sqlite3.connect(self._db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_mutation_confirmations (
                    token_hash TEXT PRIMARY KEY,
                    admin_user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    diff_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                )
                """
            )

    def propose_tenant_update(
        self, *, admin_user_id: str, tenant_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {"slug", "name", "plan", "region", "metadata", "status"}
        if not changes or set(changes) - allowed:
            raise HTTPException(status_code=400, detail="Unsupported or empty tenant changes")
        if _contains_sensitive_key(changes):
            raise HTTPException(status_code=400, detail="Secrets are not accepted in mutations")
        store = self._app.state.admin_store
        current = store.get_tenant(tenant_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
        diff = {key: {"from": getattr(current, key), "to": value} for key, value in changes.items()}
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + CONFIRMATION_TTL
        with sqlite3.connect(self._db_path) as connection:
            connection.execute(
                "INSERT INTO admin_mutation_confirmations VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    self._hash(token),
                    admin_user_id,
                    tenant_id,
                    "tenant.update",
                    json.dumps(changes, sort_keys=True),
                    json.dumps(diff, sort_keys=True),
                    expires.isoformat(),
                ),
            )
        return {
            "confirmation_id": token,
            "operation": "tenant.update",
            "tenant_id": tenant_id,
            "diff": diff,
            "expires_at": expires.isoformat(),
            "requires_confirmation": True,
        }

    def propose_entitlements(
        self,
        *,
        admin_user_id: str,
        tenant_id: str,
        features: dict[str, bool],
        limits: dict[str, Any],
    ) -> dict[str, Any]:
        store = self._app.state.admin_store
        if store.get_tenant(tenant_id) is None:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
        if not isinstance(features, dict) or not all(
            isinstance(value, bool) for value in features.values()
        ):
            raise HTTPException(status_code=400, detail="features must map strings to booleans")
        if not isinstance(limits, dict):
            raise HTTPException(status_code=400, detail="limits must be an object")
        current = store.get_tenant_entitlements(tenant_id)
        before = current.model_dump(mode="json") if current is not None else None
        return self._create_confirmation(
            admin_user_id=admin_user_id,
            tenant_id=tenant_id,
            operation="tenant_entitlements.update",
            payload={"features": features, "limits": limits},
            diff={"from": before, "to": {"features": features, "limits": limits}},
        )

    def propose_domain_add(
        self, *, admin_user_id: str, tenant_id: str, domain: str
    ) -> dict[str, Any]:
        normalized = domain.strip().lower().rstrip(".")
        if not normalized or "/" in normalized or "://" in normalized:
            raise HTTPException(status_code=400, detail="domain must be a hostname")
        store = self._app.state.admin_store
        if store.get_tenant(tenant_id) is None:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
        if store.get_tenant_domain_by_domain(normalized) is not None:
            raise HTTPException(status_code=409, detail="Domain is already assigned")
        return self._create_confirmation(
            admin_user_id=admin_user_id,
            tenant_id=tenant_id,
            operation="tenant_domain.add",
            payload={"domain": normalized, "domain_id": str(uuid4())},
            diff={"domain": {"from": None, "to": normalized}},
        )

    def _create_confirmation(
        self,
        *,
        admin_user_id: str,
        tenant_id: str,
        operation: str,
        payload: dict[str, Any],
        diff: dict[str, Any],
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + CONFIRMATION_TTL
        with sqlite3.connect(self._db_path) as connection:
            connection.execute(
                "INSERT INTO admin_mutation_confirmations VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    self._hash(token),
                    admin_user_id,
                    tenant_id,
                    operation,
                    json.dumps(payload, sort_keys=True),
                    json.dumps(diff, sort_keys=True),
                    expires.isoformat(),
                ),
            )
        return {
            "confirmation_id": token,
            "operation": operation,
            "tenant_id": tenant_id,
            "diff": diff,
            "expires_at": expires.isoformat(),
            "requires_confirmation": True,
        }

    def confirm(self, *, admin_user_id: str, token: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with sqlite3.connect(self._db_path) as connection:
            row = connection.execute(
                "SELECT tenant_id, operation, payload_json, expires_at, consumed_at FROM admin_mutation_confirmations WHERE token_hash = ? AND admin_user_id = ?",
                (self._hash(token), admin_user_id),
            ).fetchone()
            if row is None or row[4] is not None or datetime.fromisoformat(row[3]) <= now:
                raise HTTPException(
                    status_code=409, detail="Confirmation is invalid, expired, or already used"
                )
            connection.execute(
                "UPDATE admin_mutation_confirmations SET consumed_at = ? WHERE token_hash = ?",
                (now.isoformat(), self._hash(token)),
            )
        if row[1] == "tenant_entitlements.update":
            payload = json.loads(row[2])
            try:
                entitlements = self._app.state.admin_store.upsert_tenant_entitlements(
                    row[0], features=payload["features"], limits=payload["limits"]
                )
            except Exception:
                self._release(self._hash(token))
                raise
            self._app.state.store.append_audit_record(
                AuditRecord(
                    tenant_id=row[0],
                    actor_user_id=admin_user_id,
                    action="tenant_entitlements.update",
                    affected_count=1,
                    resource_type="tenant_entitlements",
                    resource_id=row[0],
                    new_values=entitlements.model_dump(mode="json"),
                )
            )
            return {
                "confirmed": True,
                "operation": row[1],
                "tenant_id": row[0],
                "entitlements": entitlements.model_dump(mode="json"),
            }
        if row[1] == "tenant_domain.add":
            payload = json.loads(row[2])
            try:
                domain = self._app.state.admin_store.add_tenant_domain(
                    TenantDomain(
                        id=payload["domain_id"], tenant_id=row[0], domain=payload["domain"]
                    )
                )
            except Exception:
                self._release(self._hash(token))
                raise
            self._app.state.store.append_audit_record(
                AuditRecord(
                    tenant_id=row[0],
                    actor_user_id=admin_user_id,
                    action="tenant_domains.create",
                    affected_count=1,
                    resource_type="tenant_domain",
                    resource_id=domain.id,
                    new_values={"domain": domain.domain, "verified": domain.verified},
                )
            )
            return {
                "confirmed": True,
                "operation": row[1],
                "tenant_id": row[0],
                "domain": domain.model_dump(mode="json"),
            }
        if row[1] != "tenant.update":
            raise HTTPException(status_code=400, detail="Unsupported mutation")
        changes = json.loads(row[2])
        try:
            tenant = self._app.state.admin_store.update_tenant(
                row[0], **changes, updated_by=admin_user_id
            )
        except Exception:
            self._release(self._hash(token))
            raise
        if tenant is None:
            raise HTTPException(status_code=404, detail="Tenant no longer exists")
        from app.admin_api import _redact_audit_payload

        self._app.state.store.append_audit_record(
            AuditRecord(
                tenant_id=tenant.id,
                actor_user_id=admin_user_id,
                action="tenant.update",
                affected_count=1,
                resource_type="tenant",
                resource_id=tenant.id,
                new_values=_redact_audit_payload(dict(changes)),
            )
        )
        return {
            "confirmed": True,
            "operation": row[1],
            "tenant_id": tenant.id,
            "tenant": tenant.model_dump(mode="json"),
        }

    def _release(self, token_hash: str) -> None:
        with sqlite3.connect(self._db_path) as connection:
            connection.execute(
                "UPDATE admin_mutation_confirmations SET consumed_at = NULL WHERE token_hash = ?",
                (token_hash,),
            )

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if any(
                part in lowered
                for part in ("secret", "token", "password", "authorization", "api_key")
            ):
                return True
            if _contains_sensitive_key(nested):
                return True
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False
