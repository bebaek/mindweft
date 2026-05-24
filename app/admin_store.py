from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, TypeGuard

from cryptography.fernet import Fernet, InvalidToken

SECRET_WRAPPER_KEY = "__secret__"


class SQLiteTenantConfigStore:
    def __init__(self, db_path: str, *, encryption_key: str | None = None) -> None:
        self._db_path = Path(db_path)
        self._lock = Lock()
        self._fernet = _build_fernet(encryption_key)
        self._initialize()

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
                CREATE TABLE IF NOT EXISTS tenant_execution_configs (
                    tenant_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()

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
