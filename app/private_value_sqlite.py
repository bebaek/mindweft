from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import RLock

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from app.private_keyring import load_encryption_keyring, parse_boolean
from app.private_values import (
    DEFAULT_PRIVATE_VALUE_MAX_CHARS,
    DEFAULT_PRIVATE_VALUE_MAX_REFS_PER_THREAD,
    DEFAULT_PRIVATE_VALUE_TTL_SECONDS,
    PII_PLACEHOLDER_PATTERN,
    PRIVATE_VALUE_DB_PATH_ENV,
    PRIVATE_VALUE_ENCRYPTION_KEY_ENV,
    PRIVATE_VALUE_ENCRYPTION_KEYS_ENV,
    PRIVATE_VALUE_KEY_VERSION_ENV,
    PRIVATE_VALUE_MAX_CHARS_ENV,
    PRIVATE_VALUE_MAX_REFS_ENV,
    PRIVATE_VALUE_REENCRYPT_ON_STARTUP_ENV,
    PRIVATE_VALUE_TTL_SECONDS_ENV,
)

_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_NONCE_BYTES = 12


class SQLiteEncryptedPrivateValueStore:
    """Restart-safe private-value storage encrypted with AES-256-GCM."""

    def __init__(
        self,
        db_path: str | Path,
        encryption_key: bytes,
        *,
        key_version: int = 1,
        decryption_keys: Mapping[int, bytes] | None = None,
        reencrypt_on_startup: bool = False,
        ttl_seconds: float = DEFAULT_PRIVATE_VALUE_TTL_SECONDS,
        max_refs_per_thread: int = DEFAULT_PRIVATE_VALUE_MAX_REFS_PER_THREAD,
        max_value_chars: int = DEFAULT_PRIVATE_VALUE_MAX_CHARS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(encryption_key) != 32:
            raise ValueError("private value encryption key must contain exactly 32 bytes")
        if key_version < 1:
            raise ValueError("private value key version must be positive")
        if ttl_seconds <= 0:
            raise ValueError("private value TTL must be positive")
        if max_refs_per_thread < 1:
            raise ValueError("private value reference limit must be positive")
        if max_value_chars < 1:
            raise ValueError("private value character limit must be positive")
        self._db_path = Path(db_path).expanduser()
        if str(db_path) == ":memory:":
            raise ValueError("encrypted private value DB must use a filesystem path")
        self._key_version = key_version
        keys = dict(decryption_keys or {})
        existing_active_key = keys.get(key_version)
        if existing_active_key is not None and existing_active_key != encryption_key:
            raise ValueError("active private value key conflicts with decryption keyring")
        keys[key_version] = encryption_key
        for version, key in keys.items():
            if version < 1 or len(key) != 32:
                raise ValueError("private value decryption keys must be versioned 32-byte keys")
        self._aesgcms = {version: AESGCM(key) for version, key in keys.items()}
        self._ttl_seconds = ttl_seconds
        self._max_refs_per_thread = max_refs_per_thread
        self._max_value_chars = max_value_chars
        self._clock = clock
        self._lock = RLock()
        self._initialize()
        if reencrypt_on_startup:
            self.rotate_to_active_key()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SQLiteEncryptedPrivateValueStore:
        lookup = os.environ if env is None else env
        db_path = lookup.get(PRIVATE_VALUE_DB_PATH_ENV, "").strip()
        if not db_path:
            raise RuntimeError(f"{PRIVATE_VALUE_DB_PATH_ENV} is required")
        active_key, keyring, key_version = load_encryption_keyring(
            lookup,
            single_key_env=PRIVATE_VALUE_ENCRYPTION_KEY_ENV,
            keyring_env=PRIVATE_VALUE_ENCRYPTION_KEYS_ENV,
            key_version_env=PRIVATE_VALUE_KEY_VERSION_ENV,
            database_env=PRIVATE_VALUE_DB_PATH_ENV,
        )
        return cls(
            db_path,
            active_key,
            key_version=key_version,
            decryption_keys=keyring,
            reencrypt_on_startup=parse_boolean(
                lookup.get(PRIVATE_VALUE_REENCRYPT_ON_STARTUP_ENV, ""),
                PRIVATE_VALUE_REENCRYPT_ON_STARTUP_ENV,
            ),
            ttl_seconds=_positive_float(
                lookup.get(PRIVATE_VALUE_TTL_SECONDS_ENV, ""),
                PRIVATE_VALUE_TTL_SECONDS_ENV,
                DEFAULT_PRIVATE_VALUE_TTL_SECONDS,
            ),
            max_refs_per_thread=_positive_int(
                lookup.get(PRIVATE_VALUE_MAX_REFS_ENV, ""),
                PRIVATE_VALUE_MAX_REFS_ENV,
                DEFAULT_PRIVATE_VALUE_MAX_REFS_PER_THREAD,
            ),
            max_value_chars=_positive_int(
                lookup.get(PRIVATE_VALUE_MAX_CHARS_ENV, ""),
                PRIVATE_VALUE_MAX_CHARS_ENV,
                DEFAULT_PRIVATE_VALUE_MAX_CHARS,
            ),
        )

    def add(
        self,
        tenant_id: str,
        thread_id: str,
        values: Mapping[str, str],
        *,
        kinds: Mapping[str, str] | None = None,
    ) -> None:
        if not values:
            return
        now = self._clock()
        expires_at = now + self._ttl_seconds
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_expired(connection, now)
            existing_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM private_values WHERE tenant_id = ? AND thread_id = ?",
                    (tenant_id, thread_id),
                ).fetchone()[0]
            )
            existing_refs = {
                str(row[0])
                for row in connection.execute(
                    "SELECT reference FROM private_values WHERE tenant_id = ? AND thread_id = ?",
                    (tenant_id, thread_id),
                )
            }
            if existing_count + len(set(values) - existing_refs) > self._max_refs_per_thread:
                raise HTTPException(
                    status_code=502,
                    detail="Private value reference limit exceeded for thread",
                )
            for reference, value in values.items():
                if len(value) > self._max_value_chars:
                    raise HTTPException(
                        status_code=502,
                        detail="Private value exceeded the configured character limit",
                    )
                requested_kind = (kinds or {}).get(reference, "unknown")
                kind = requested_kind if _KIND_PATTERN.fullmatch(requested_kind) else "unknown"
                existing = connection.execute(
                    """
                    SELECT kind, nonce, ciphertext, key_version
                    FROM private_values
                    WHERE tenant_id = ? AND thread_id = ? AND reference = ?
                    """,
                    (tenant_id, thread_id, reference),
                ).fetchone()
                if existing is not None:
                    existing_value = self._decrypt(
                        tenant_id,
                        thread_id,
                        reference,
                        str(existing[0]),
                        bytes(existing[1]),
                        bytes(existing[2]),
                        int(existing[3]),
                    )
                    if existing_value != value:
                        raise HTTPException(
                            status_code=502,
                            detail=f"Private value reference collision for '{reference}'",
                        )
                nonce = os.urandom(_NONCE_BYTES)
                ciphertext = self._aesgcms[self._key_version].encrypt(
                    nonce,
                    value.encode("utf-8"),
                    _associated_data(
                        tenant_id,
                        thread_id,
                        reference,
                        kind,
                        self._key_version,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO private_values (
                        tenant_id, thread_id, reference, kind, nonce, ciphertext,
                        key_version, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, thread_id, reference) DO UPDATE SET
                        kind = excluded.kind,
                        nonce = excluded.nonce,
                        ciphertext = excluded.ciphertext,
                        key_version = excluded.key_version,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        tenant_id,
                        thread_id,
                        reference,
                        kind,
                        nonce,
                        ciphertext,
                        self._key_version,
                        now,
                        expires_at,
                    ),
                )
            connection.commit()

    def render_for_user(self, tenant_id: str, thread_id: str, text: str) -> str:
        return self._replace(tenant_id, thread_id, text, strict=False)

    def resolve_for_tool(self, tenant_id: str, thread_id: str, text: str) -> str:
        return self._replace(tenant_id, thread_id, text, strict=True)

    def clear_thread(self, tenant_id: str, thread_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM private_values WHERE tenant_id = ? AND thread_id = ?",
                (tenant_id, thread_id),
            )
            connection.commit()

    def _replace(self, tenant_id: str, thread_id: str, text: str, *, strict: bool) -> str:
        now = self._clock()
        with self._lock, self._connect() as connection:
            self._delete_expired(connection, now)
            connection.commit()

            def replace(match: re.Match[str]) -> str:
                reference = match.group("reference")
                row = connection.execute(
                    """
                    SELECT kind, nonce, ciphertext, key_version
                    FROM private_values
                    WHERE tenant_id = ? AND thread_id = ? AND reference = ?
                    """,
                    (tenant_id, thread_id, reference),
                ).fetchone()
                if row is None:
                    if strict:
                        raise HTTPException(
                            status_code=409,
                            detail="Private value is missing or expired",
                        )
                    return match.group(0)
                return self._decrypt(
                    tenant_id,
                    thread_id,
                    reference,
                    str(row[0]),
                    bytes(row[1]),
                    bytes(row[2]),
                    int(row[3]),
                )

            return PII_PLACEHOLDER_PATTERN.sub(replace, text)

    def _decrypt(
        self,
        tenant_id: str,
        thread_id: str,
        reference: str,
        kind: str,
        nonce: bytes,
        ciphertext: bytes,
        key_version: int,
    ) -> str:
        aesgcm = self._aesgcms.get(key_version)
        if aesgcm is None:
            raise HTTPException(
                status_code=500,
                detail="Private value encryption key version is unavailable",
            )
        try:
            plaintext = aesgcm.decrypt(
                nonce,
                ciphertext,
                _associated_data(tenant_id, thread_id, reference, kind, key_version),
            )
        except InvalidTag as exc:
            raise HTTPException(
                status_code=500,
                detail="Private value authentication failed",
            ) from exc
        return plaintext.decode("utf-8")

    def rotate_to_active_key(self) -> int:
        """Re-encrypt all surviving records with the configured active key version."""
        now = self._clock()
        rotated = 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._delete_expired(connection, now)
            rows = connection.execute(
                """
                SELECT tenant_id, thread_id, reference, kind, nonce, ciphertext, key_version
                FROM private_values
                WHERE key_version != ?
                """,
                (self._key_version,),
            ).fetchall()
            for row in rows:
                tenant_id, thread_id, reference, kind = map(str, row[:4])
                value = self._decrypt(
                    tenant_id,
                    thread_id,
                    reference,
                    kind,
                    bytes(row[4]),
                    bytes(row[5]),
                    int(row[6]),
                )
                nonce = os.urandom(_NONCE_BYTES)
                ciphertext = self._aesgcms[self._key_version].encrypt(
                    nonce,
                    value.encode("utf-8"),
                    _associated_data(
                        tenant_id,
                        thread_id,
                        reference,
                        kind,
                        self._key_version,
                    ),
                )
                connection.execute(
                    """
                    UPDATE private_values
                    SET nonce = ?, ciphertext = ?, key_version = ?
                    WHERE tenant_id = ? AND thread_id = ? AND reference = ?
                    """,
                    (
                        nonce,
                        ciphertext,
                        self._key_version,
                        tenant_id,
                        thread_id,
                        reference,
                    ),
                )
                rotated += 1
            connection.commit()
        return rotated

    def _initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS private_values (
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
                "CREATE INDEX IF NOT EXISTS idx_private_values_expiry ON private_values(expires_at)"
            )
            self._delete_expired(connection, self._clock())
            connection.commit()
            versions = {
                int(version_row[0])
                for version_row in connection.execute(
                    "SELECT DISTINCT key_version FROM private_values"
                )
            }
            missing_versions = versions - self._aesgcms.keys()
            if missing_versions:
                missing = ", ".join(str(version) for version in sorted(missing_versions))
                raise RuntimeError(
                    f"Encrypted private value database requires unavailable key version(s): {missing}"
                )
            row = connection.execute(
                """
                SELECT tenant_id, thread_id, reference, kind, nonce, ciphertext, key_version
                FROM private_values
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if row is not None:
                try:
                    self._decrypt(
                        str(row[0]),
                        str(row[1]),
                        str(row[2]),
                        str(row[3]),
                        bytes(row[4]),
                        bytes(row[5]),
                        int(row[6]),
                    )
                except HTTPException as exc:
                    raise RuntimeError(
                        "Encrypted private value database could not be opened with the configured key"
                    ) from exc
        try:
            self._db_path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _delete_expired(connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM private_values WHERE expires_at <= ?", (now,))


def _associated_data(
    tenant_id: str,
    thread_id: str,
    reference: str,
    kind: str,
    key_version: int,
) -> bytes:
    return json.dumps(
        {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "reference": reference,
            "kind": kind,
            "key_version": key_version,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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
