from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any


class SQLiteTenantConfigStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        self._lock = Lock()
        self._initialize()

    def list_tenants(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT tenant_id FROM tenant_execution_configs ORDER BY tenant_id"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def get_raw_config(self, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT config_json FROM tenant_execution_configs WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Stored config for tenant '{tenant_id}' is invalid")
        return payload

    def upsert_raw_config(self, tenant_id: str, payload: dict[str, Any]) -> None:
        # TODO: Encrypt secret fields before persisting. LLM API keys and MCP headers are
        # currently stored in plaintext inside config_json and only redacted in API responses.
        serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO tenant_execution_configs (tenant_id, config_json)
                    VALUES (?, ?)
                    ON CONFLICT(tenant_id) DO UPDATE SET
                        config_json = excluded.config_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (tenant_id, serialized),
                )
                connection.commit()

    def delete_config(self, tenant_id: str) -> bool:
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM tenant_execution_configs WHERE tenant_id = ?",
                    (tenant_id,),
                )
                connection.commit()
        return cursor.rowcount > 0

    def _initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_execution_configs (
                    tenant_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection
