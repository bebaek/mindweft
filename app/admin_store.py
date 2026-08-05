from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Literal, TypeGuard, cast
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from app.models import (
    Tenant,
    TenantDomain,
    TenantEntitlements,
    TenantStatus,
    TenantUser,
    TenantUserRole,
    TenantUserStatus,
)

SECRET_WRAPPER_KEY = "__secret__"
_UNSET = object()


@dataclass(frozen=True)
class LocalIdentity:
    username: str
    tenant_id: str
    user_id: str
    password_hash: str
    credential_version: int
    disabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PasswordSetup:
    token_hash: str
    username: str
    tenant_id: str
    user_id: str
    expires_at: datetime
    used_at: datetime | None
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class TenantMCPServerCatalogPolicy:
    tenant_id: str
    item_ids: tuple[str, ...]
    allow_custom_mcp_servers: bool
    require_subject_assignment: bool
    version: int
    updated_by: str | None
    updated_at: datetime


@dataclass(frozen=True)
class SubjectMCPServerCatalogAssignment:
    tenant_id: str
    subject_type: str
    subject_id: str
    item_ids: tuple[str, ...]
    version: int
    updated_by: str | None
    updated_at: datetime


@dataclass(frozen=True)
class UserExecutionConfigRecord:
    tenant_id: str
    user_id: str
    config: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class UserExecutionConfigConflictError(RuntimeError):
    def __init__(self, *, expected_version: int, actual_version: int | None) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        actual = "missing" if actual_version is None else str(actual_version)
        super().__init__(
            f"User execution config version conflict: expected {expected_version}, found {actual}"
        )


@dataclass(frozen=True)
class UserExecutionCredentialRecord:
    tenant_id: str
    user_id: str
    credential_ref: str
    header_name: str
    header_value: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class UserExecutionCredentialMetadata:
    tenant_id: str
    user_id: str
    credential_ref: str
    header_name: str
    version: int
    created_at: datetime
    updated_at: datetime


class UserExecutionCredentialConflictError(RuntimeError):
    def __init__(self, *, expected_version: int, actual_version: int | None) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        actual = "missing" if actual_version is None else str(actual_version)
        super().__init__(
            f"User execution credential version conflict: expected {expected_version}, found {actual}"
        )


@dataclass(frozen=True)
class UserDeprovisioningEvent:
    id: str
    tenant_id: str
    user_record_id: str
    user_id: str
    target_status: TenantUserStatus
    actor_user_id: str
    state: Literal["pending", "processing", "completed", "dead_letter"]
    attempts: int
    next_attempt_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    last_error: str | None
    assignment_removed: bool
    grants_disabled: int
    created_at: datetime
    updated_at: datetime


class SQLiteTenantConfigStore:
    def __init__(self, db_path: str, *, encryption_key: str | None = None) -> None:
        self._db_path = Path(db_path)
        self._lock = Lock()
        self._fernet = _build_fernet(encryption_key)
        self._initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def list_registry_tenants(
        self,
        *,
        status: TenantStatus | str | None = None,
        plan: str | None = None,
        slug: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Tenant], int]:
        clauses: list[str] = []
        values: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(str(status.value if isinstance(status, TenantStatus) else status))
        if plan is not None:
            clauses.append("plan = ?")
            values.append(plan)
        if slug is not None:
            clauses.append("slug = ?")
            values.append(slug)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            total_row = connection.execute(
                f"SELECT COUNT(*) FROM tenants{where}",
                tuple(values),
            ).fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            rows = connection.execute(
                f"""
                SELECT * FROM tenants{where}
                ORDER BY updated_at DESC, id ASC
                LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
        return [_tenant_from_row(row) for row in rows], total

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return _tenant_from_row(row) if row is not None else None

    def get_tenant_by_slug(self, slug: str) -> Tenant | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
        return _tenant_from_row(row) if row is not None else None

    def create_tenant(
        self,
        tenant: Tenant,
        *,
        execution_config: dict[str, Any] | None = None,
    ) -> Tenant:
        now = _utc_now_iso()
        created_at = tenant.created_at.isoformat() if tenant.created_at else now
        updated_at = tenant.updated_at.isoformat() if tenant.updated_at else now
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tenants (
                        id, slug, name, status, plan, region, metadata_json,
                        created_by, updated_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant.id,
                        tenant.slug,
                        tenant.name,
                        tenant.status.value,
                        tenant.plan,
                        tenant.region,
                        json.dumps(tenant.metadata, ensure_ascii=True, sort_keys=True),
                        tenant.created_by,
                        tenant.updated_by,
                        created_at,
                        updated_at,
                    ),
                )
                if execution_config is not None:
                    serialized_config = json.dumps(
                        _encrypt_payload(execution_config, self._fernet),
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                    connection.execute(
                        """
                        INSERT INTO tenant_execution_configs (tenant_id, config_json, version)
                        VALUES (?, ?, 1)
                        """,
                        (tenant.id, serialized_config),
                    )
                connection.commit()
        created = self.get_tenant(tenant.id)
        if created is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Tenant '{tenant.id}' was not created")
        return created

    def create_tenants_atomic(self, tenants: list[Tenant]) -> list[Tenant]:
        """Create a seed batch atomically, rolling back on any uniqueness error."""
        if not tenants:
            return []
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                try:
                    for tenant in tenants:
                        created_at = tenant.created_at.isoformat() if tenant.created_at else now
                        updated_at = tenant.updated_at.isoformat() if tenant.updated_at else now
                        connection.execute(
                            """
                            INSERT INTO tenants (
                                id, slug, name, status, plan, region, metadata_json,
                                created_by, updated_by, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                tenant.id,
                                tenant.slug,
                                tenant.name,
                                tenant.status.value,
                                tenant.plan,
                                tenant.region,
                                json.dumps(tenant.metadata, ensure_ascii=True, sort_keys=True),
                                tenant.created_by,
                                tenant.updated_by,
                                created_at,
                                updated_at,
                            ),
                        )
                    connection.commit()
                except sqlite3.IntegrityError:
                    connection.rollback()
                    raise
        return [
            tenant
            for tenant in (self.get_tenant(item.id) for item in tenants)
            if tenant is not None
        ]

    def update_tenant(
        self,
        tenant_id: str,
        *,
        slug: str | None = None,
        name: str | None = None,
        status: TenantStatus | None = None,
        plan: str | None | object = _UNSET,
        region: str | None | object = _UNSET,
        metadata: dict[str, Any] | None = None,
        updated_by: str | None = None,
    ) -> Tenant | None:
        current = self.get_tenant(tenant_id)
        if current is None:
            return None
        next_slug = current.slug if slug is None else slug
        next_name = current.name if name is None else name
        next_status = current.status if status is None else status
        next_plan = current.plan if plan is _UNSET else cast(str | None, plan)
        next_region = current.region if region is _UNSET else cast(str | None, region)
        next_metadata = current.metadata if metadata is None else metadata
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE tenants SET
                        slug = ?,
                        name = ?,
                        status = ?,
                        plan = ?,
                        region = ?,
                        metadata_json = ?,
                        updated_by = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_slug,
                        next_name,
                        next_status.value,
                        next_plan,
                        next_region,
                        json.dumps(next_metadata, ensure_ascii=True, sort_keys=True),
                        updated_by,
                        now,
                        tenant_id,
                    ),
                )
                connection.commit()
        return self.get_tenant(tenant_id)

    def delete_tenant(self, tenant_id: str, *, updated_by: str | None = None) -> bool:
        return (
            self.update_tenant(tenant_id, status=TenantStatus.DELETED, updated_by=updated_by)
            is not None
        )

    def list_tenant_users(
        self,
        tenant_id: str,
        *,
        status: TenantUserStatus | str | None = None,
        role: TenantUserRole | str | None = None,
        email: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TenantUser], int]:
        clauses = ["tenant_id = ?"]
        values: list[object] = [tenant_id]
        if status is not None:
            clauses.append("status = ?")
            values.append(str(status.value if isinstance(status, TenantUserStatus) else status))
        if role is not None:
            clauses.append("role = ?")
            values.append(str(role.value if isinstance(role, TenantUserRole) else role))
        if email is not None:
            clauses.append("email = ?")
            values.append(email)
        where = " AND ".join(clauses)
        with self._connection() as connection:
            total_row = connection.execute(
                f"SELECT COUNT(*) FROM tenant_users WHERE {where}",
                tuple(values),
            ).fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            rows = connection.execute(
                f"""
                SELECT * FROM tenant_users WHERE {where}
                ORDER BY updated_at DESC, user_id ASC
                LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
        return [_tenant_user_from_row(row) for row in rows], total

    def create_tenant_user(self, user: TenantUser) -> TenantUser:
        now = _utc_now_iso()
        created_at = user.created_at.isoformat() if user.created_at else now
        updated_at = user.updated_at.isoformat() if user.updated_at else now
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tenant_users (
                        id, tenant_id, user_id, email, display_name, role, status,
                        metadata_json, created_by, updated_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        user.tenant_id,
                        user.user_id,
                        user.email,
                        user.display_name,
                        user.role.value,
                        user.status.value,
                        json.dumps(user.metadata, ensure_ascii=True, sort_keys=True),
                        user.created_by,
                        user.updated_by,
                        created_at,
                        updated_at,
                    ),
                )
                connection.commit()
        created = self.get_tenant_user(user.tenant_id, user.id)
        if created is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Tenant user '{user.id}' was not created")
        return created

    def get_tenant_user(self, tenant_id: str, user_record_id: str) -> TenantUser | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_users WHERE tenant_id = ? AND id = ?",
                (tenant_id, user_record_id),
            ).fetchone()
        return _tenant_user_from_row(row) if row is not None else None

    def get_tenant_user_by_user_id(self, tenant_id: str, user_id: str) -> TenantUser | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_users WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            ).fetchone()
        return _tenant_user_from_row(row) if row is not None else None

    def update_tenant_user(
        self,
        tenant_id: str,
        user_record_id: str,
        *,
        email: str | None | object = _UNSET,
        display_name: str | None | object = _UNSET,
        role: TenantUserRole | None = None,
        status: TenantUserStatus | None = None,
        metadata: dict[str, Any] | None = None,
        updated_by: str | None = None,
    ) -> TenantUser | None:
        current = self.get_tenant_user(tenant_id, user_record_id)
        if current is None:
            return None
        next_email = current.email if email is _UNSET else cast(str | None, email)
        next_display_name = (
            current.display_name if display_name is _UNSET else cast(str | None, display_name)
        )
        next_role = current.role if role is None else role
        next_status = current.status if status is None else status
        next_metadata = current.metadata if metadata is None else metadata
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE tenant_users SET
                        email = ?,
                        display_name = ?,
                        role = ?,
                        status = ?,
                        metadata_json = ?,
                        updated_by = ?,
                        updated_at = ?
                    WHERE tenant_id = ? AND id = ?
                    """,
                    (
                        next_email,
                        next_display_name,
                        next_role.value,
                        next_status.value,
                        json.dumps(next_metadata, ensure_ascii=True, sort_keys=True),
                        updated_by,
                        now,
                        tenant_id,
                        user_record_id,
                    ),
                )
                if (
                    next_status
                    in {
                        TenantUserStatus.SUSPENDED,
                        TenantUserStatus.DELETED,
                    }
                    and next_status != current.status
                ):
                    connection.execute(
                        """
                        INSERT INTO user_deprovisioning_events (
                            id, tenant_id, user_record_id, user_id, target_status,
                            actor_user_id, state, attempts, next_attempt_at,
                            assignment_removed, grants_disabled, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, 0, 0, ?, ?)
                        """,
                        (
                            str(uuid4()),
                            tenant_id,
                            user_record_id,
                            current.user_id,
                            next_status.value,
                            updated_by or "system",
                            now,
                            now,
                            now,
                        ),
                    )
                connection.commit()
        return self.get_tenant_user(tenant_id, user_record_id)

    def list_user_deprovisioning_events(
        self,
        tenant_id: str,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[UserDeprovisioningEvent], int]:
        clauses = ["tenant_id = ?"]
        values: list[object] = [tenant_id]
        if state is not None:
            clauses.append("state = ?")
            values.append(state)
        where = " AND ".join(clauses)
        with self._connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM user_deprovisioning_events WHERE {where}",
                    tuple(values),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM user_deprovisioning_events WHERE {where}
                ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?
                """,
                (*values, limit, offset),
            ).fetchall()
        return [_user_deprovisioning_event_from_row(row) for row in rows], total

    def get_user_deprovisioning_event(
        self, tenant_id: str, event_id: str
    ) -> UserDeprovisioningEvent | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM user_deprovisioning_events WHERE tenant_id = ? AND id = ?",
                (tenant_id, event_id),
            ).fetchone()
        return _user_deprovisioning_event_from_row(row) if row is not None else None

    def claim_user_deprovisioning_event(
        self, *, lease_seconds: int = 60
    ) -> UserDeprovisioningEvent | None:
        now = datetime.now(timezone.utc)
        stale_before = datetime.fromtimestamp(now.timestamp() - lease_seconds, timezone.utc)
        now_iso = now.isoformat()
        with self._lock:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE user_deprovisioning_events
                    SET state = 'pending', claimed_at = NULL, next_attempt_at = ?, updated_at = ?
                    WHERE state = 'processing' AND claimed_at < ?
                    """,
                    (now_iso, now_iso, stale_before.isoformat()),
                )
                row = connection.execute(
                    """
                    SELECT id FROM user_deprovisioning_events
                    WHERE state = 'pending' AND next_attempt_at <= ?
                    ORDER BY next_attempt_at, created_at, id LIMIT 1
                    """,
                    (now_iso,),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                event_id = str(row["id"])
                connection.execute(
                    """
                    UPDATE user_deprovisioning_events
                    SET state = 'processing', attempts = attempts + 1,
                        claimed_at = ?, updated_at = ?
                    WHERE id = ? AND state = 'pending'
                    """,
                    (now_iso, now_iso, event_id),
                )
                claimed = connection.execute(
                    "SELECT * FROM user_deprovisioning_events WHERE id = ?", (event_id,)
                ).fetchone()
                connection.commit()
        return _user_deprovisioning_event_from_row(claimed) if claimed is not None else None

    def heartbeat_user_deprovisioning_event(self, event_id: str) -> bool:
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE user_deprovisioning_events
                    SET claimed_at = ?, updated_at = ?
                    WHERE id = ? AND state = 'processing'
                    """,
                    (now, now, event_id),
                )
                connection.commit()
        return cursor.rowcount == 1

    def complete_user_deprovisioning_event(
        self,
        event_id: str,
        *,
        assignment_removed: bool,
        grants_disabled: int,
    ) -> bool:
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE user_deprovisioning_events
                    SET state = 'completed', completed_at = ?, claimed_at = NULL,
                        last_error = NULL, assignment_removed = ?, grants_disabled = ?,
                        updated_at = ?
                    WHERE id = ? AND state = 'processing'
                    """,
                    (now, int(assignment_removed), grants_disabled, now, event_id),
                )
                connection.commit()
        return cursor.rowcount == 1

    def fail_user_deprovisioning_event(
        self,
        event_id: str,
        *,
        error: str,
        retry_at: datetime,
        dead_letter: bool,
        assignment_removed: bool,
        grants_disabled: int,
    ) -> bool:
        now = _utc_now_iso()
        state = "dead_letter" if dead_letter else "pending"
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE user_deprovisioning_events
                    SET state = ?, next_attempt_at = ?, claimed_at = NULL,
                        last_error = ?, assignment_removed = ?, grants_disabled = ?,
                        updated_at = ?
                    WHERE id = ? AND state = 'processing'
                    """,
                    (
                        state,
                        retry_at.isoformat(),
                        error[:2000],
                        int(assignment_removed),
                        grants_disabled,
                        now,
                        event_id,
                    ),
                )
                connection.commit()
        return cursor.rowcount == 1

    def retry_user_deprovisioning_event(self, tenant_id: str, event_id: str) -> bool:
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE user_deprovisioning_events
                    SET state = 'pending', next_attempt_at = ?, claimed_at = NULL,
                        completed_at = NULL, last_error = NULL, updated_at = ?
                    WHERE tenant_id = ? AND id = ? AND state IN ('pending', 'dead_letter')
                    """,
                    (now, now, tenant_id, event_id),
                )
                connection.commit()
        return cursor.rowcount == 1

    def delete_tenant_user(
        self,
        tenant_id: str,
        user_record_id: str,
        *,
        updated_by: str | None = None,
    ) -> bool:
        return (
            self.update_tenant_user(
                tenant_id,
                user_record_id,
                status=TenantUserStatus.DELETED,
                updated_by=updated_by,
            )
            is not None
        )

    def get_local_identity(self, username: str) -> LocalIdentity | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_identities WHERE username = ?", (username,)
            ).fetchone()
        return _local_identity_from_row(row) if row is not None else None

    def get_local_identity_for_user(self, tenant_id: str, user_id: str) -> LocalIdentity | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_identities WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            ).fetchone()
        return _local_identity_from_row(row) if row is not None else None

    def create_password_setup(
        self,
        *,
        token_hash: str,
        username: str,
        tenant_id: str,
        user_id: str,
        expires_at: datetime,
        created_by: str,
    ) -> PasswordSetup:
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                existing_for_user = connection.execute(
                    "SELECT username FROM local_identities WHERE tenant_id = ? AND user_id = ?",
                    (tenant_id, user_id),
                ).fetchone()
                if existing_for_user is not None and str(existing_for_user["username"]) != username:
                    raise sqlite3.IntegrityError("Login username cannot be changed")
                conflict = connection.execute(
                    "SELECT tenant_id, user_id FROM local_identities WHERE username = ?",
                    (username,),
                ).fetchone()
                if conflict is not None and (
                    str(conflict["tenant_id"]) != tenant_id or str(conflict["user_id"]) != user_id
                ):
                    raise sqlite3.IntegrityError("Username is already assigned")
                connection.execute(
                    """
                    UPDATE password_setups SET used_at = ?
                    WHERE tenant_id = ? AND user_id = ? AND used_at IS NULL
                    """,
                    (now, tenant_id, user_id),
                )
                connection.execute(
                    """
                    INSERT INTO password_setups (
                        token_hash, username, tenant_id, user_id, expires_at,
                        used_at, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        token_hash,
                        username,
                        tenant_id,
                        user_id,
                        expires_at.isoformat(),
                        created_by,
                        now,
                    ),
                )
                connection.commit()
        setup = self.get_password_setup(token_hash)
        if setup is None:  # pragma: no cover - defensive
            raise RuntimeError("Password setup was not created")
        return setup

    def get_password_setup(self, token_hash: str) -> PasswordSetup | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM password_setups WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return _password_setup_from_row(row) if row is not None else None

    def consume_password_setup(
        self,
        *,
        token_hash: str,
        password_hash: str,
        now: datetime,
    ) -> LocalIdentity | None:
        now_iso = now.isoformat()
        with self._lock:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM password_setups WHERE token_hash = ?", (token_hash,)
                ).fetchone()
                if row is None or row["used_at"] is not None:
                    return None
                setup = _password_setup_from_row(row)
                if setup.expires_at <= now:
                    return None
                existing = connection.execute(
                    "SELECT * FROM local_identities WHERE username = ?", (setup.username,)
                ).fetchone()
                if existing is not None and (
                    str(existing["tenant_id"]) != setup.tenant_id
                    or str(existing["user_id"]) != setup.user_id
                ):
                    return None
                connection.execute(
                    """
                    INSERT INTO local_identities (
                        username, tenant_id, user_id, password_hash,
                        credential_version, disabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 0, ?, ?)
                    ON CONFLICT(username) DO UPDATE SET
                        password_hash = excluded.password_hash,
                        credential_version = local_identities.credential_version + 1,
                        disabled = 0,
                        updated_at = excluded.updated_at
                    """,
                    (
                        setup.username,
                        setup.tenant_id,
                        setup.user_id,
                        password_hash,
                        now_iso,
                        now_iso,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tenant_users SET status = 'active', updated_at = ?
                    WHERE tenant_id = ? AND user_id = ? AND status = 'invited'
                    """,
                    (now_iso, setup.tenant_id, setup.user_id),
                )
                connection.execute(
                    "UPDATE password_setups SET used_at = ? WHERE token_hash = ?",
                    (now_iso, token_hash),
                )
                connection.commit()
        return self.get_local_identity(setup.username)

    def disable_local_identity(self, tenant_id: str, user_id: str) -> bool:
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    UPDATE local_identities SET
                        disabled = 1,
                        credential_version = credential_version + 1,
                        updated_at = ?
                    WHERE tenant_id = ? AND user_id = ?
                    """,
                    (_utc_now_iso(), tenant_id, user_id),
                )
                connection.commit()
        return cursor.rowcount > 0

    def list_tenant_domains(self, tenant_id: str) -> list[TenantDomain]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tenant_domains WHERE tenant_id = ? ORDER BY domain ASC",
                (tenant_id,),
            ).fetchall()
        return [_domain_from_row(row) for row in rows]

    def add_tenant_domain(self, domain: TenantDomain) -> TenantDomain:
        created_at = domain.created_at.isoformat()
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tenant_domains (id, tenant_id, domain, verified, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        domain.id,
                        domain.tenant_id,
                        domain.domain,
                        1 if domain.verified else 0,
                        created_at,
                    ),
                )
                connection.commit()
        created = self.get_tenant_domain(domain.tenant_id, domain.id)
        if created is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Domain '{domain.id}' was not created")
        return created

    def get_tenant_domain(self, tenant_id: str, domain_id: str) -> TenantDomain | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_domains WHERE tenant_id = ? AND id = ?",
                (tenant_id, domain_id),
            ).fetchone()
        return _domain_from_row(row) if row is not None else None

    def get_tenant_domain_by_domain(
        self,
        domain: str,
        *,
        verified_only: bool = False,
    ) -> TenantDomain | None:
        where = "domain = ? AND verified = 1" if verified_only else "domain = ?"
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM tenant_domains WHERE {where}",
                (domain,),
            ).fetchone()
        return _domain_from_row(row) if row is not None else None

    def verify_tenant_domain(self, tenant_id: str, domain_id: str) -> TenantDomain | None:
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    "UPDATE tenant_domains SET verified = 1 WHERE tenant_id = ? AND id = ?",
                    (tenant_id, domain_id),
                )
                connection.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_tenant_domain(tenant_id, domain_id)

    def delete_tenant_domain(self, tenant_id: str, domain_id: str) -> TenantDomain | None:
        current = self.get_tenant_domain(tenant_id, domain_id)
        if current is None:
            return None
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM tenant_domains WHERE tenant_id = ? AND id = ?",
                    (tenant_id, domain_id),
                )
                connection.commit()
        return current

    def get_tenant_entitlements(self, tenant_id: str) -> TenantEntitlements | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_entitlements WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return _entitlements_from_row(row) if row is not None else None

    def upsert_tenant_entitlements(
        self,
        tenant_id: str,
        *,
        features: dict[str, bool],
        limits: dict[str, int | float | str | bool | None],
    ) -> TenantEntitlements:
        current = self.get_tenant_entitlements(tenant_id)
        version = 1 if current is None else current.version + 1
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tenant_entitlements (
                        tenant_id, features_json, limits_json, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id) DO UPDATE SET
                        features_json = excluded.features_json,
                        limits_json = excluded.limits_json,
                        version = excluded.version,
                        updated_at = excluded.updated_at
                    """,
                    (
                        tenant_id,
                        json.dumps(features, ensure_ascii=True, sort_keys=True),
                        json.dumps(limits, ensure_ascii=True, sort_keys=True),
                        version,
                        now,
                    ),
                )
                connection.commit()
        entitlements = self.get_tenant_entitlements(tenant_id)
        if entitlements is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Entitlements for tenant '{tenant_id}' were not saved")
        return entitlements

    def delete_tenant_entitlements(self, tenant_id: str) -> bool:
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    "DELETE FROM tenant_entitlements WHERE tenant_id = ?",
                    (tenant_id,),
                )
                connection.commit()
        return cursor.rowcount > 0

    def get_tenant_mcp_server_catalog_policy(
        self, tenant_id: str
    ) -> TenantMCPServerCatalogPolicy | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_mcp_server_catalog_policies WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return _mcp_server_catalog_policy_from_row(row) if row is not None else None

    def upsert_tenant_mcp_server_catalog_policy(
        self,
        tenant_id: str,
        *,
        item_ids: list[str],
        allow_custom_mcp_servers: bool,
        require_subject_assignment: bool,
        updated_by: str | None,
    ) -> TenantMCPServerCatalogPolicy:
        current = self.get_tenant_mcp_server_catalog_policy(tenant_id)
        version = 1 if current is None else current.version + 1
        now = _utc_now_iso()
        normalized_item_ids = sorted(set(item_ids))
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tenant_mcp_server_catalog_policies (
                        tenant_id, item_ids_json, allow_custom_mcp_servers,
                        require_subject_assignment, version, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id) DO UPDATE SET
                        item_ids_json = excluded.item_ids_json,
                        allow_custom_mcp_servers = excluded.allow_custom_mcp_servers,
                        require_subject_assignment = excluded.require_subject_assignment,
                        version = excluded.version,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        tenant_id,
                        json.dumps(normalized_item_ids, ensure_ascii=True),
                        int(allow_custom_mcp_servers),
                        int(require_subject_assignment),
                        version,
                        updated_by,
                        now,
                    ),
                )
                connection.commit()
        policy = self.get_tenant_mcp_server_catalog_policy(tenant_id)
        if policy is None:  # pragma: no cover - defensive
            raise RuntimeError(f"MCP server catalog policy for tenant '{tenant_id}' was not saved")
        return policy

    def delete_tenant_mcp_server_catalog_policy(self, tenant_id: str) -> bool:
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    "DELETE FROM tenant_mcp_server_catalog_policies WHERE tenant_id = ?",
                    (tenant_id,),
                )
                connection.commit()
        return cursor.rowcount > 0

    def get_subject_mcp_server_catalog_assignment(
        self, tenant_id: str, subject_type: str, subject_id: str
    ) -> SubjectMCPServerCatalogAssignment | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM subject_mcp_server_catalog_assignments
                WHERE tenant_id = ? AND subject_type = ? AND subject_id = ?
                """,
                (tenant_id, subject_type, subject_id),
            ).fetchone()
        return _subject_mcp_server_catalog_assignment_from_row(row) if row is not None else None

    def list_subject_mcp_server_catalog_assignments(
        self, tenant_id: str
    ) -> list[SubjectMCPServerCatalogAssignment]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM subject_mcp_server_catalog_assignments
                WHERE tenant_id = ? ORDER BY subject_type, subject_id
                """,
                (tenant_id,),
            ).fetchall()
        return [_subject_mcp_server_catalog_assignment_from_row(row) for row in rows]

    def upsert_subject_mcp_server_catalog_assignment(
        self,
        tenant_id: str,
        subject_type: str,
        subject_id: str,
        *,
        item_ids: list[str],
        updated_by: str | None,
    ) -> SubjectMCPServerCatalogAssignment:
        current = self.get_subject_mcp_server_catalog_assignment(
            tenant_id, subject_type, subject_id
        )
        version = 1 if current is None else current.version + 1
        now = _utc_now_iso()
        normalized_item_ids = sorted(set(item_ids))
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO subject_mcp_server_catalog_assignments (
                        tenant_id, subject_type, subject_id, item_ids_json, version, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, subject_type, subject_id) DO UPDATE SET
                        item_ids_json = excluded.item_ids_json, version = excluded.version,
                        updated_by = excluded.updated_by, updated_at = excluded.updated_at
                    """,
                    (
                        tenant_id,
                        subject_type,
                        subject_id,
                        json.dumps(normalized_item_ids, ensure_ascii=True),
                        version,
                        updated_by,
                        now,
                    ),
                )
                connection.commit()
        assignment = self.get_subject_mcp_server_catalog_assignment(
            tenant_id, subject_type, subject_id
        )
        if assignment is None:  # pragma: no cover - defensive
            raise RuntimeError("MCP server catalog assignment was not saved")
        return assignment

    def delete_subject_mcp_server_catalog_assignment(
        self, tenant_id: str, subject_type: str, subject_id: str
    ) -> bool:
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    "DELETE FROM subject_mcp_server_catalog_assignments WHERE tenant_id = ? AND subject_type = ? AND subject_id = ?",
                    (tenant_id, subject_type, subject_id),
                )
                connection.commit()
        return cursor.rowcount > 0

    def effective_subject_mcp_server_catalog_item_ids(
        self, tenant_id: str, user_id: str
    ) -> tuple[str, ...] | None:
        item_ids, _source = self.effective_subject_mcp_server_catalog_access(tenant_id, user_id)
        return item_ids

    def effective_subject_mcp_server_catalog_access(
        self, tenant_id: str, user_id: str
    ) -> tuple[tuple[str, ...] | None, str]:
        tenant_policy = self.get_tenant_mcp_server_catalog_policy(tenant_id)
        if tenant_policy is None:
            return None, "legacy"
        user = self.get_tenant_user_by_user_id(tenant_id, user_id)
        if user is not None and user.status != TenantUserStatus.ACTIVE:
            return (), "inactive"
        user_assignment = self.get_subject_mcp_server_catalog_assignment(tenant_id, "user", user_id)
        if user_assignment is not None:
            item_ids = set(user_assignment.item_ids) & set(tenant_policy.item_ids)
            return tuple(sorted(item_ids)), "user"
        if user is not None:
            role_assignment = self.get_subject_mcp_server_catalog_assignment(
                tenant_id, "role", user.role.value
            )
            if role_assignment is not None:
                item_ids = set(role_assignment.item_ids) & set(tenant_policy.item_ids)
                return tuple(sorted(item_ids)), "role"
        if tenant_policy.require_subject_assignment:
            return (), "unassigned"
        return None, "tenant"

    def get_user_execution_config(
        self, tenant_id: str, user_id: str
    ) -> UserExecutionConfigRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT tenant_id, user_id, config_json, version, created_at, updated_at
                FROM user_execution_configs
                WHERE tenant_id = ? AND user_id = ?
                """,
                (tenant_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return _user_execution_config_from_row(row, self._fernet)

    def upsert_user_execution_config(
        self,
        tenant_id: str,
        user_id: str,
        payload: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> UserExecutionConfigRecord:
        serialized = json.dumps(
            _encrypt_payload(payload, self._fernet),
            ensure_ascii=True,
            sort_keys=True,
        )
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                if expected_version is None:
                    connection.execute(
                        """
                        INSERT INTO user_execution_configs (
                            tenant_id, user_id, config_json, version, created_at, updated_at
                        ) VALUES (?, ?, ?, 1, ?, ?)
                        ON CONFLICT(tenant_id, user_id) DO UPDATE SET
                            config_json = excluded.config_json,
                            version = user_execution_configs.version + 1,
                            updated_at = excluded.updated_at
                        """,
                        (tenant_id, user_id, serialized, now, now),
                    )
                elif expected_version == 0:
                    try:
                        connection.execute(
                            """
                            INSERT INTO user_execution_configs (
                                tenant_id, user_id, config_json, version, created_at, updated_at
                            ) VALUES (?, ?, ?, 1, ?, ?)
                            """,
                            (tenant_id, user_id, serialized, now, now),
                        )
                    except sqlite3.IntegrityError as exc:
                        actual_version = _user_execution_config_version(
                            connection, tenant_id, user_id
                        )
                        raise UserExecutionConfigConflictError(
                            expected_version=expected_version,
                            actual_version=actual_version,
                        ) from exc
                else:
                    cursor = connection.execute(
                        """
                        UPDATE user_execution_configs SET
                            config_json = ?,
                            version = version + 1,
                            updated_at = ?
                        WHERE tenant_id = ? AND user_id = ? AND version = ?
                        """,
                        (serialized, now, tenant_id, user_id, expected_version),
                    )
                    if cursor.rowcount == 0:
                        actual_version = _user_execution_config_version(
                            connection, tenant_id, user_id
                        )
                        raise UserExecutionConfigConflictError(
                            expected_version=expected_version,
                            actual_version=actual_version,
                        )
                connection.commit()
        record = self.get_user_execution_config(tenant_id, user_id)
        if record is None:  # pragma: no cover - defensive
            raise RuntimeError("User execution config disappeared after upsert")
        return record

    def delete_user_execution_config(
        self,
        tenant_id: str,
        user_id: str,
        *,
        expected_version: int | None = None,
    ) -> bool:
        with self._lock:
            with self._connection() as connection:
                if expected_version is None:
                    cursor = connection.execute(
                        """
                        DELETE FROM user_execution_configs
                        WHERE tenant_id = ? AND user_id = ?
                        """,
                        (tenant_id, user_id),
                    )
                else:
                    cursor = connection.execute(
                        """
                        DELETE FROM user_execution_configs
                        WHERE tenant_id = ? AND user_id = ? AND version = ?
                        """,
                        (tenant_id, user_id, expected_version),
                    )
                    if cursor.rowcount == 0:
                        actual_version = _user_execution_config_version(
                            connection, tenant_id, user_id
                        )
                        if actual_version is not None or expected_version != 0:
                            raise UserExecutionConfigConflictError(
                                expected_version=expected_version,
                                actual_version=actual_version,
                            )
                connection.commit()
        return cursor.rowcount > 0

    @property
    def user_execution_credentials_encrypted(self) -> bool:
        return self._fernet is not None

    def get_user_execution_credential(
        self,
        tenant_id: str,
        user_id: str,
        credential_ref: str,
    ) -> UserExecutionCredentialRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT tenant_id, user_id, credential_ref, header_name, value_cipher,
                       version, created_at, updated_at
                FROM user_execution_credentials
                WHERE tenant_id = ? AND user_id = ? AND credential_ref = ?
                """,
                (tenant_id, user_id, credential_ref),
            ).fetchone()
        if row is None:
            return None
        return _user_execution_credential_from_row(row, self._fernet)

    def list_user_execution_credentials(
        self,
        tenant_id: str,
        user_id: str,
    ) -> list[UserExecutionCredentialMetadata]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT tenant_id, user_id, credential_ref, header_name,
                       version, created_at, updated_at
                FROM user_execution_credentials
                WHERE tenant_id = ? AND user_id = ?
                ORDER BY credential_ref
                """,
                (tenant_id, user_id),
            ).fetchall()
        return [_user_execution_credential_metadata_from_row(row) for row in rows]

    def upsert_user_execution_credential(
        self,
        tenant_id: str,
        user_id: str,
        credential_ref: str,
        *,
        header_name: str,
        header_value: str,
        expected_version: int | None = None,
    ) -> UserExecutionCredentialRecord:
        if self._fernet is None:
            raise RuntimeError("Admin encryption is required for user execution credentials")
        value_cipher = _encrypt_user_execution_credential_value(
            tenant_id,
            user_id,
            credential_ref,
            header_name,
            header_value,
            self._fernet,
        )
        now = _utc_now_iso()
        with self._lock:
            with self._connection() as connection:
                if expected_version is None:
                    connection.execute(
                        """
                        INSERT INTO user_execution_credentials (
                            tenant_id, user_id, credential_ref, header_name, value_cipher,
                            version, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(tenant_id, user_id, credential_ref) DO UPDATE SET
                            header_name = excluded.header_name,
                            value_cipher = excluded.value_cipher,
                            version = user_execution_credentials.version + 1,
                            updated_at = excluded.updated_at
                        """,
                        (
                            tenant_id,
                            user_id,
                            credential_ref,
                            header_name,
                            value_cipher,
                            now,
                            now,
                        ),
                    )
                elif expected_version == 0:
                    try:
                        connection.execute(
                            """
                            INSERT INTO user_execution_credentials (
                                tenant_id, user_id, credential_ref, header_name, value_cipher,
                                version, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                tenant_id,
                                user_id,
                                credential_ref,
                                header_name,
                                value_cipher,
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        actual_version = _user_execution_credential_version(
                            connection, tenant_id, user_id, credential_ref
                        )
                        raise UserExecutionCredentialConflictError(
                            expected_version=expected_version,
                            actual_version=actual_version,
                        ) from exc
                else:
                    cursor = connection.execute(
                        """
                        UPDATE user_execution_credentials SET
                            header_name = ?, value_cipher = ?, version = version + 1, updated_at = ?
                        WHERE tenant_id = ? AND user_id = ? AND credential_ref = ? AND version = ?
                        """,
                        (
                            header_name,
                            value_cipher,
                            now,
                            tenant_id,
                            user_id,
                            credential_ref,
                            expected_version,
                        ),
                    )
                    if cursor.rowcount == 0:
                        actual_version = _user_execution_credential_version(
                            connection, tenant_id, user_id, credential_ref
                        )
                        raise UserExecutionCredentialConflictError(
                            expected_version=expected_version,
                            actual_version=actual_version,
                        )
                connection.commit()
        record = self.get_user_execution_credential(tenant_id, user_id, credential_ref)
        if record is None:  # pragma: no cover - defensive
            raise RuntimeError("User execution credential disappeared after upsert")
        return record

    def delete_user_execution_credential(
        self,
        tenant_id: str,
        user_id: str,
        credential_ref: str,
        *,
        expected_version: int | None = None,
    ) -> bool:
        with self._lock:
            with self._connection() as connection:
                if expected_version is None:
                    cursor = connection.execute(
                        """
                        DELETE FROM user_execution_credentials
                        WHERE tenant_id = ? AND user_id = ? AND credential_ref = ?
                        """,
                        (tenant_id, user_id, credential_ref),
                    )
                else:
                    cursor = connection.execute(
                        """
                        DELETE FROM user_execution_credentials
                        WHERE tenant_id = ? AND user_id = ? AND credential_ref = ? AND version = ?
                        """,
                        (tenant_id, user_id, credential_ref, expected_version),
                    )
                    if cursor.rowcount == 0:
                        actual_version = _user_execution_credential_version(
                            connection, tenant_id, user_id, credential_ref
                        )
                        if actual_version is not None or expected_version != 0:
                            raise UserExecutionCredentialConflictError(
                                expected_version=expected_version,
                                actual_version=actual_version,
                            )
                connection.commit()
        return cursor.rowcount > 0

    def list_tenants(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT tenant_id FROM tenant_execution_configs ORDER BY tenant_id"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def get_raw_config(self, tenant_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT config_json FROM tenant_execution_configs WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Stored config for tenant '{tenant_id}' is invalid")
        return _decrypt_payload(payload, self._fernet)

    def get_config_version(self, tenant_id: str) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT version FROM tenant_execution_configs WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def upsert_raw_config(self, tenant_id: str, payload: dict[str, Any]) -> None:
        serialized = json.dumps(
            _encrypt_payload(payload, self._fernet),
            ensure_ascii=True,
            sort_keys=True,
        )
        with self._lock:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tenant_execution_configs (tenant_id, config_json, version)
                    VALUES (?, ?, 1)
                    ON CONFLICT(tenant_id) DO UPDATE SET
                        config_json = excluded.config_json,
                        version = tenant_execution_configs.version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (tenant_id, serialized),
                )
                connection.commit()

    def delete_config(self, tenant_id: str) -> bool:
        with self._lock:
            with self._connection() as connection:
                cursor = connection.execute(
                    "DELETE FROM tenant_execution_configs WHERE tenant_id = ?",
                    (tenant_id,),
                )
                connection.commit()
        return cursor.rowcount > 0

    def _initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan TEXT,
                    region TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT,
                    updated_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tenants_plan ON tenants(plan)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenants_updated_at ON tenants(updated_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_users (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    email TEXT,
                    display_name TEXT,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT,
                    updated_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, user_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_users_tenant_status ON tenant_users(tenant_id, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_users_tenant_role ON tenant_users(tenant_id, role)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_users_email ON tenant_users(email)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_deprovisioning_events (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_record_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    target_status TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'processing', 'completed', 'dead_letter')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT,
                    last_error TEXT,
                    assignment_removed INTEGER NOT NULL DEFAULT 0,
                    grants_disabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_deprovisioning_claim
                ON user_deprovisioning_events(state, next_attempt_at, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_deprovisioning_tenant
                ON user_deprovisioning_events(tenant_id, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS local_identities (
                    username TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    credential_version INTEGER NOT NULL DEFAULT 1,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, user_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_local_identities_user ON local_identities(tenant_id, user_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS password_setups (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_password_setups_user ON password_setups(tenant_id, user_id, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_password_setups_expiry ON password_setups(expires_at, used_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_domains (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    domain TEXT NOT NULL UNIQUE,
                    verified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_domains_tenant ON tenant_domains(tenant_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenant_domains_domain_verified ON tenant_domains(domain, verified)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_entitlements (
                    tenant_id TEXT PRIMARY KEY,
                    features_json TEXT NOT NULL DEFAULT '{}',
                    limits_json TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_mcp_server_catalog_policies (
                    tenant_id TEXT PRIMARY KEY,
                    item_ids_json TEXT NOT NULL DEFAULT '[]',
                    allow_custom_mcp_servers INTEGER NOT NULL DEFAULT 0,
                    require_subject_assignment INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subject_mcp_server_catalog_assignments (
                    tenant_id TEXT NOT NULL,
                    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'role')),
                    subject_id TEXT NOT NULL,
                    item_ids_json TEXT NOT NULL DEFAULT '[]',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, subject_type, subject_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_execution_configs (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_execution_configs_updated
                ON user_execution_configs(tenant_id, updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_execution_credentials (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    credential_ref TEXT NOT NULL,
                    header_name TEXT NOT NULL,
                    value_cipher BLOB NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, credential_ref)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_execution_credentials_updated
                ON user_execution_credentials(tenant_id, user_id, updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_execution_configs (
                    tenant_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            policy_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(tenant_mcp_server_catalog_policies)"
                )
            }
            if "require_subject_assignment" not in policy_columns:
                connection.execute(
                    """
                    ALTER TABLE tenant_mcp_server_catalog_policies
                    ADD COLUMN require_subject_assignment INTEGER NOT NULL DEFAULT 0
                    """
                )
            connection.commit()
            _ensure_tenant_execution_config_columns(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection


def _user_execution_config_version(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
) -> int | None:
    row = connection.execute(
        """
        SELECT version FROM user_execution_configs
        WHERE tenant_id = ? AND user_id = ?
        """,
        (tenant_id, user_id),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _user_execution_config_from_row(
    row: sqlite3.Row,
    fernet: Fernet | None,
) -> UserExecutionConfigRecord:
    payload = json.loads(str(row["config_json"]))
    if not isinstance(payload, dict):
        raise RuntimeError("Stored user execution config is invalid")
    config = _decrypt_payload(payload, fernet)
    return UserExecutionConfigRecord(
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        config=config,
        version=int(row["version"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _subject_mcp_server_catalog_assignment_from_row(
    row: sqlite3.Row,
) -> SubjectMCPServerCatalogAssignment:
    item_ids = json.loads(str(row["item_ids_json"]))
    if not isinstance(item_ids, list) or not all(isinstance(item, str) for item in item_ids):
        raise RuntimeError("Stored MCP server catalog assignment is invalid")
    return SubjectMCPServerCatalogAssignment(
        tenant_id=str(row["tenant_id"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        item_ids=tuple(item_ids),
        version=int(row["version"]),
        updated_by=str(row["updated_by"]) if row["updated_by"] is not None else None,
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _mcp_server_catalog_policy_from_row(
    row: sqlite3.Row,
) -> TenantMCPServerCatalogPolicy:
    item_ids = json.loads(str(row["item_ids_json"]))
    if not isinstance(item_ids, list) or not all(isinstance(item, str) for item in item_ids):
        raise RuntimeError("Stored MCP server catalog policy is invalid")
    return TenantMCPServerCatalogPolicy(
        tenant_id=str(row["tenant_id"]),
        item_ids=tuple(item_ids),
        allow_custom_mcp_servers=bool(row["allow_custom_mcp_servers"]),
        require_subject_assignment=bool(row["require_subject_assignment"]),
        version=int(row["version"]),
        updated_by=str(row["updated_by"]) if row["updated_by"] is not None else None,
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_identity_from_row(row: sqlite3.Row) -> LocalIdentity:
    return LocalIdentity(
        username=str(row["username"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        password_hash=str(row["password_hash"]),
        credential_version=int(row["credential_version"]),
        disabled=bool(row["disabled"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _password_setup_from_row(row: sqlite3.Row) -> PasswordSetup:
    return PasswordSetup(
        token_hash=str(row["token_hash"]),
        username=str(row["username"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        used_at=(
            datetime.fromisoformat(str(row["used_at"])) if row["used_at"] is not None else None
        ),
        created_by=str(row["created_by"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def _tenant_from_row(row: sqlite3.Row) -> Tenant:
    metadata = json.loads(str(row["metadata_json"]))
    if not isinstance(metadata, dict):
        raise RuntimeError(f"Stored metadata for tenant '{row['id']}' is invalid")
    return Tenant(
        id=str(row["id"]),
        slug=str(row["slug"]),
        name=str(row["name"]),
        status=TenantStatus(str(row["status"])),
        plan=str(row["plan"]) if row["plan"] is not None else None,
        region=str(row["region"]) if row["region"] is not None else None,
        metadata=metadata,
        created_by=str(row["created_by"]) if row["created_by"] is not None else None,
        updated_by=str(row["updated_by"]) if row["updated_by"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _user_deprovisioning_event_from_row(row: sqlite3.Row) -> UserDeprovisioningEvent:
    return UserDeprovisioningEvent(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        user_record_id=str(row["user_record_id"]),
        user_id=str(row["user_id"]),
        target_status=TenantUserStatus(str(row["target_status"])),
        actor_user_id=str(row["actor_user_id"]),
        state=cast(Literal["pending", "processing", "completed", "dead_letter"], str(row["state"])),
        attempts=int(row["attempts"]),
        next_attempt_at=datetime.fromisoformat(str(row["next_attempt_at"])),
        claimed_at=(
            datetime.fromisoformat(str(row["claimed_at"]))
            if row["claimed_at"] is not None
            else None
        ),
        completed_at=(
            datetime.fromisoformat(str(row["completed_at"]))
            if row["completed_at"] is not None
            else None
        ),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        assignment_removed=bool(row["assignment_removed"]),
        grants_disabled=int(row["grants_disabled"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _tenant_user_from_row(row: sqlite3.Row) -> TenantUser:
    metadata = json.loads(str(row["metadata_json"]))
    if not isinstance(metadata, dict):
        raise RuntimeError(f"Stored metadata for tenant user '{row['id']}' is invalid")
    return TenantUser(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        email=str(row["email"]) if row["email"] is not None else None,
        display_name=str(row["display_name"]) if row["display_name"] is not None else None,
        role=TenantUserRole(str(row["role"])),
        status=TenantUserStatus(str(row["status"])),
        metadata=metadata,
        created_by=str(row["created_by"]) if row["created_by"] is not None else None,
        updated_by=str(row["updated_by"]) if row["updated_by"] is not None else None,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _domain_from_row(row: sqlite3.Row) -> TenantDomain:
    return TenantDomain(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        domain=str(row["domain"]),
        verified=bool(row["verified"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def _entitlements_from_row(row: sqlite3.Row) -> TenantEntitlements:
    features = json.loads(str(row["features_json"]))
    limits = json.loads(str(row["limits_json"]))
    if not isinstance(features, dict) or not all(
        isinstance(value, bool) for value in features.values()
    ):
        raise RuntimeError(f"Stored features for tenant '{row['tenant_id']}' are invalid")
    if not isinstance(limits, dict):
        raise RuntimeError(f"Stored limits for tenant '{row['tenant_id']}' are invalid")
    return TenantEntitlements(
        tenant_id=str(row["tenant_id"]),
        features={str(key): value for key, value in features.items()},
        limits={str(key): value for key, value in limits.items()},
        version=int(row["version"]),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _user_execution_credential_version(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    credential_ref: str,
) -> int | None:
    row = connection.execute(
        """
        SELECT version FROM user_execution_credentials
        WHERE tenant_id = ? AND user_id = ? AND credential_ref = ?
        """,
        (tenant_id, user_id, credential_ref),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _encrypt_user_execution_credential_value(
    tenant_id: str,
    user_id: str,
    credential_ref: str,
    header_name: str,
    header_value: str,
    fernet: Fernet,
) -> bytes:
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "credential_ref": credential_ref,
            "header_name": header_name,
            "header_value": header_value,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return fernet.encrypt(payload)


def _user_execution_credential_from_row(
    row: sqlite3.Row,
    fernet: Fernet | None,
) -> UserExecutionCredentialRecord:
    if fernet is None:
        raise RuntimeError("Admin encryption key is required to read user execution credentials")
    try:
        decrypted = fernet.decrypt(bytes(row["value_cipher"]))
        payload = json.loads(decrypted)
    except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Stored user execution credential could not be decrypted") from exc
    expected_scope = {
        "tenant_id": str(row["tenant_id"]),
        "user_id": str(row["user_id"]),
        "credential_ref": str(row["credential_ref"]),
        "header_name": str(row["header_name"]),
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected_scope.items()
    ):
        raise RuntimeError("Stored user execution credential scope authentication failed")
    header_value = payload.get("header_value")
    if not isinstance(header_value, str) or not header_value:
        raise RuntimeError("Stored user execution credential is invalid")
    return UserExecutionCredentialRecord(
        **expected_scope,
        header_value=header_value,
        version=int(row["version"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _user_execution_credential_metadata_from_row(
    row: sqlite3.Row,
) -> UserExecutionCredentialMetadata:
    return UserExecutionCredentialMetadata(
        tenant_id=str(row["tenant_id"]),
        user_id=str(row["user_id"]),
        credential_ref=str(row["credential_ref"]),
        header_name=str(row["header_name"]),
        version=int(row["version"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _build_fernet(encryption_key: str | None) -> Fernet | None:
    if encryption_key is None:
        return None
    raw_key = encryption_key.strip()
    if not raw_key:
        return None
    try:
        return Fernet(raw_key.encode("ascii"))
    except Exception:
        try:
            derived = base64.urlsafe_b64encode(raw_key.encode("utf-8").ljust(32, b"\0")[:32])
            return Fernet(derived)
        except Exception as exc:
            raise RuntimeError("Invalid admin encryption key") from exc


def _encrypt_payload(payload: dict[str, Any], fernet: Fernet | None) -> dict[str, Any]:
    cloned = json.loads(json.dumps(payload))
    if fernet is None:
        return cloned

    llm = cloned.get("llm")
    if isinstance(llm, dict):
        _encrypt_secret_field(llm, "api_key", fernet)
        _encrypt_secret_field(llm, "extra_headers", fernet)
        _encrypt_secret_field(llm, "extraHeaders", fernet)

    tools = cloned.get("tools")
    if isinstance(tools, dict):
        mcp_servers = tools.get("mcp_servers") or tools.get("mcpServers")
        if isinstance(mcp_servers, list):
            for server in mcp_servers:
                if isinstance(server, dict):
                    _encrypt_secret_field(server, "headers", fernet)
    return cloned


def _decrypt_payload(payload: dict[str, Any], fernet: Fernet | None) -> dict[str, Any]:
    cloned = json.loads(json.dumps(payload))
    if fernet is None:
        return cloned

    llm = cloned.get("llm")
    if isinstance(llm, dict):
        _decrypt_secret_field(llm, "api_key", fernet)
        _decrypt_secret_field(llm, "extra_headers", fernet)
        _decrypt_secret_field(llm, "extraHeaders", fernet)

    tools = cloned.get("tools")
    if isinstance(tools, dict):
        mcp_servers = tools.get("mcp_servers") or tools.get("mcpServers")
        if isinstance(mcp_servers, list):
            for server in mcp_servers:
                if isinstance(server, dict):
                    _decrypt_secret_field(server, "headers", fernet)
    return cloned


def _ensure_tenant_execution_config_columns(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA table_info(tenant_execution_configs)").fetchall()
    columns = {str(row[1]) for row in rows}
    if "version" not in columns:
        connection.execute(
            "ALTER TABLE tenant_execution_configs ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )


def _encrypt_secret_field(container: dict[str, Any], key: str, fernet: Fernet) -> None:
    value = container.get(key)
    if value is None or _is_wrapped_secret(value):
        return
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")
    container[key] = {SECRET_WRAPPER_KEY: fernet.encrypt(serialized).decode("ascii")}


def _decrypt_secret_field(container: dict[str, Any], key: str, fernet: Fernet) -> None:
    value = container.get(key)
    wrapped = _as_wrapped_secret(value)
    if wrapped is None:
        return
    token = wrapped[SECRET_WRAPPER_KEY]
    try:
        decrypted = fernet.decrypt(str(token).encode("ascii"))
    except InvalidToken as exc:
        raise RuntimeError("Unable to decrypt stored admin secret") from exc
    container[key] = json.loads(decrypted.decode("utf-8"))


def _is_wrapped_secret(value: object) -> TypeGuard[dict[str, str]]:
    return _as_wrapped_secret(value) is not None


def _as_wrapped_secret(value: object) -> dict[str, str] | None:
    if (
        isinstance(value, dict)
        and set(value) == {SECRET_WRAPPER_KEY}
        and isinstance(value[SECRET_WRAPPER_KEY], str)
    ):
        return {SECRET_WRAPPER_KEY: value[SECRET_WRAPPER_KEY]}
    return None
