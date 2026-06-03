from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, TypeGuard

from cryptography.fernet import Fernet, InvalidToken

from app.models import Tenant, TenantDomain, TenantEntitlements, TenantStatus

SECRET_WRAPPER_KEY = "__secret__"


class SQLiteTenantConfigStore:
    def __init__(self, db_path: str, *, encryption_key: str | None = None) -> None:
        self._db_path = Path(db_path)
        self._lock = Lock()
        self._fernet = _build_fernet(encryption_key)
        self._initialize()

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

    def create_tenant(self, tenant: Tenant) -> Tenant:
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
                connection.commit()
        created = self.get_tenant(tenant.id)
        if created is None:  # pragma: no cover - defensive
            raise RuntimeError(f"Tenant '{tenant.id}' was not created")
        return created

    def update_tenant(
        self,
        tenant_id: str,
        *,
        slug: str | None = None,
        name: str | None = None,
        status: TenantStatus | None = None,
        plan: str | None = None,
        region: str | None = None,
        metadata: dict[str, Any] | None = None,
        updated_by: str | None = None,
    ) -> Tenant | None:
        current = self.get_tenant(tenant_id)
        if current is None:
            return None
        next_slug = current.slug if slug is None else slug
        next_name = current.name if name is None else name
        next_status = current.status if status is None else status
        next_plan = current.plan if plan is None else plan
        next_region = current.region if region is None else region
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_tenants_plan ON tenants(plan)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tenants_updated_at ON tenants(updated_at)"
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
                CREATE TABLE IF NOT EXISTS tenant_execution_configs (
                    tenant_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    if not isinstance(features, dict) or not all(isinstance(value, bool) for value in features.values()):
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
