from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.private_keyring import load_encryption_keyring

GENERIC_OAUTH_PROVIDER = "generic-oauth"
OAUTH_STORE_PATH_ENV = "MINIGENT_OAUTH_STORE_PATH"
OAUTH_PROVIDER_ID_ENV = "MINIGENT_OAUTH_PROVIDER_ID"
OAUTH_CLIENT_ID_ENV = "MINIGENT_OAUTH_CLIENT_ID"
OAUTH_AUTHORIZE_URL_ENV = "MINIGENT_OAUTH_AUTHORIZE_URL"
OAUTH_TOKEN_URL_ENV = "MINIGENT_OAUTH_TOKEN_URL"
OAUTH_REDIRECT_URI_ENV = "MINIGENT_OAUTH_REDIRECT_URI"
OAUTH_SCOPE_ENV = "MINIGENT_OAUTH_SCOPE"
OAUTH_AUTH_PARAMS_ENV = "MINIGENT_OAUTH_AUTH_PARAMS"
OAUTH_ACCOUNT_ID_JWT_CLAIM_ENV = "MINIGENT_OAUTH_ACCOUNT_ID_JWT_CLAIM"
OAUTH_ENCRYPTION_KEY_ENV = "MINIGENT_OAUTH_ENCRYPTION_KEY"
OAUTH_ENCRYPTION_KEYS_ENV = "MINIGENT_OAUTH_ENCRYPTION_KEYS"
OAUTH_KEY_VERSION_ENV = "MINIGENT_OAUTH_KEY_VERSION"
OAUTH_LEGACY_STORE_PATH_ENV = "MINIGENT_OAUTH_LEGACY_STORE_PATH"
OAUTH_REFRESH_LEASE_SECONDS = 45.0
OAUTH_REFRESH_WAIT_SECONDS = 50.0


@dataclass(frozen=True)
class OAuthCredentials:
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }
        if self.account_id is not None:
            payload["account_id"] = self.account_id
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> OAuthCredentials:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_at = payload.get("expires_at")
        account_id = payload.get("account_id")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("OAuth credential is missing access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RuntimeError("OAuth credential is missing refresh_token")
        if not isinstance(expires_at, int | float):
            raise RuntimeError("OAuth credential is missing expires_at")
        if account_id is not None and not isinstance(account_id, str):
            raise RuntimeError("OAuth credential account_id must be a string")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=float(expires_at),
            account_id=account_id,
        )


@dataclass(frozen=True)
class OAuthLoginStart:
    authorization_url: str
    state: str


@dataclass(frozen=True)
class PendingOAuthFlow:
    verifier: str
    redirect_uri: str
    created_at: float


@dataclass(frozen=True)
class GenericOAuthConfig:
    provider_id: str
    client_id: str
    authorize_url: str
    token_url: str
    redirect_uri: str
    scope: str
    auth_params: dict[str, str]
    account_id_jwt_claim: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GenericOAuthConfig:
        lookup = os.environ if env is None else env
        return cls(
            provider_id=_required_env(lookup, OAUTH_PROVIDER_ID_ENV),
            client_id=_required_env(lookup, OAUTH_CLIENT_ID_ENV),
            authorize_url=_required_env(lookup, OAUTH_AUTHORIZE_URL_ENV),
            token_url=_required_env(lookup, OAUTH_TOKEN_URL_ENV),
            redirect_uri=_required_env(lookup, OAUTH_REDIRECT_URI_ENV),
            scope=_required_env(lookup, OAUTH_SCOPE_ENV),
            auth_params=_json_string_map_env(lookup, OAUTH_AUTH_PARAMS_ENV),
            account_id_jwt_claim=lookup.get(OAUTH_ACCOUNT_ID_JWT_CLAIM_ENV, "").strip() or None,
        )


@dataclass(frozen=True)
class OAuthStoreSettings:
    path: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OAuthStoreSettings:
        lookup = os.environ if env is None else env
        return cls(path=Path(_required_env(lookup, OAUTH_STORE_PATH_ENV)).expanduser())


@dataclass(frozen=True)
class OAuthSettings:
    store: OAuthStoreSettings
    provider: GenericOAuthConfig

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OAuthSettings:
        lookup = os.environ if env is None else env
        return cls(
            store=OAuthStoreSettings.from_env(lookup),
            provider=GenericOAuthConfig.from_env(lookup),
        )


class OAuthFlowStore:
    def __init__(self, ttl_seconds: float = 600.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._flows: dict[str, PendingOAuthFlow] = {}

    def put(self, state: str, flow: PendingOAuthFlow) -> None:
        self.prune()
        self._flows[state] = flow

    def pop(self, state: str) -> PendingOAuthFlow | None:
        self.prune()
        return self._flows.pop(state, None)

    def prune(self) -> None:
        cutoff = time.time() - self._ttl_seconds
        expired = [state for state, flow in self._flows.items() if flow.created_at < cutoff]
        for state in expired:
            self._flows.pop(state, None)


@runtime_checkable
class OAuthCredentialStore(Protocol):
    def get(self, provider: str) -> OAuthCredentials | None: ...

    def set(self, provider: str, credentials: OAuthCredentials) -> None: ...

    def delete(self, provider: str) -> None: ...


@runtime_checkable
class CoordinatedOAuthCredentialStore(OAuthCredentialStore, Protocol):
    def get_versioned(self, provider: str) -> tuple[OAuthCredentials, int] | None: ...

    def try_claim_refresh(
        self, provider: str, *, version: int, owner: str, lease_seconds: float
    ) -> bool: ...

    def complete_refresh(
        self,
        provider: str,
        *,
        version: int,
        owner: str,
        credentials: OAuthCredentials,
    ) -> bool: ...

    def release_refresh(self, provider: str, *, version: int, owner: str) -> None: ...


class SQLiteEncryptedOAuthStore:
    def __init__(
        self,
        path: Path,
        *,
        keyring: dict[int, bytes],
        active_version: int,
        legacy_path: Path | None = None,
        flow_ttl_seconds: float = 600.0,
    ) -> None:
        if active_version not in keyring:
            raise ValueError("active OAuth encryption key is absent from the keyring")
        if any(version < 1 or len(key) != 32 for version, key in keyring.items()):
            raise ValueError("OAuth encryption keys must be versioned 32-byte keys")
        self._path = path
        self._aesgcms = {version: AESGCM(key) for version, key in keyring.items()}
        self._active_version = active_version
        self._legacy_path = legacy_path
        self._flow_ttl_seconds = flow_ttl_seconds
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self._import_legacy_once()
        try:
            self._path.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, flow_ttl_seconds: float = 600.0
    ) -> SQLiteEncryptedOAuthStore:
        lookup = os.environ if env is None else env
        active_key, keyring, active_version = load_encryption_keyring(
            lookup,
            single_key_env=OAUTH_ENCRYPTION_KEY_ENV,
            keyring_env=OAUTH_ENCRYPTION_KEYS_ENV,
            key_version_env=OAUTH_KEY_VERSION_ENV,
            database_env=OAUTH_STORE_PATH_ENV,
        )
        keyring[active_version] = active_key
        legacy_value = lookup.get(OAUTH_LEGACY_STORE_PATH_ENV, "").strip()
        return cls(
            Path(_required_env(lookup, OAUTH_STORE_PATH_ENV)).expanduser(),
            keyring=keyring,
            active_version=active_version,
            legacy_path=Path(legacy_value).expanduser() if legacy_value else None,
            flow_ttl_seconds=flow_ttl_seconds,
        )

    @property
    def path(self) -> Path:
        return self._path

    def get(self, provider: str) -> OAuthCredentials | None:
        versioned = self.get_versioned(provider)
        return versioned[0] if versioned is not None else None

    def get_versioned(self, provider: str) -> tuple[OAuthCredentials, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT credentials_cipher, key_version, version FROM oauth_credentials WHERE provider = ?",
                (provider,),
            ).fetchone()
        if row is None:
            return None
        payload = self._decrypt_json(
            bytes(row["credentials_cipher"]),
            key_version=int(row["key_version"]),
            aad=f"oauth-credentials|{provider}".encode(),
        )
        return OAuthCredentials.from_json(payload), int(row["version"])

    def set(self, provider: str, credentials: OAuthCredentials) -> None:
        ciphertext = self._encrypt_json(
            credentials.to_json(), aad=f"oauth-credentials|{provider}".encode()
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO oauth_credentials
                  (provider, credentials_cipher, key_version, version, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(provider) DO UPDATE SET
                  credentials_cipher = excluded.credentials_cipher,
                  key_version = excluded.key_version,
                  version = oauth_credentials.version + 1,
                  refresh_owner = NULL,
                  refresh_lease_expires_at = NULL,
                  updated_at = excluded.updated_at
                """,
                (provider, ciphertext, self._active_version, time.time()),
            )
            connection.commit()

    def delete(self, provider: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM oauth_credentials WHERE provider = ?", (provider,))
            connection.commit()

    def try_claim_refresh(
        self, provider: str, *, version: int, owner: str, lease_seconds: float
    ) -> bool:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE oauth_credentials
                SET refresh_owner = ?, refresh_lease_expires_at = ?
                WHERE provider = ? AND version = ?
                  AND (refresh_owner IS NULL OR refresh_lease_expires_at <= ?)
                """,
                (owner, now + lease_seconds, provider, version, now),
            )
            connection.commit()
            return cursor.rowcount == 1

    def complete_refresh(
        self,
        provider: str,
        *,
        version: int,
        owner: str,
        credentials: OAuthCredentials,
    ) -> bool:
        ciphertext = self._encrypt_json(
            credentials.to_json(), aad=f"oauth-credentials|{provider}".encode()
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE oauth_credentials
                SET credentials_cipher = ?, key_version = ?, version = version + 1,
                    refresh_owner = NULL, refresh_lease_expires_at = NULL, updated_at = ?
                WHERE provider = ? AND version = ? AND refresh_owner = ?
                """,
                (ciphertext, self._active_version, time.time(), provider, version, owner),
            )
            connection.commit()
            return cursor.rowcount == 1

    def release_refresh(self, provider: str, *, version: int, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE oauth_credentials
                SET refresh_owner = NULL, refresh_lease_expires_at = NULL
                WHERE provider = ? AND version = ? AND refresh_owner = ?
                """,
                (provider, version, owner),
            )
            connection.commit()

    def put(self, state: str, flow: PendingOAuthFlow) -> None:
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        ciphertext = self._encrypt_json(
            {
                "state": state,
                "verifier": flow.verifier,
                "redirect_uri": flow.redirect_uri,
                "created_at": flow.created_at,
            },
            aad=f"oauth-flow|{state_hash}".encode(),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM oauth_flows WHERE expires_at <= ?", (time.time(),))
            connection.execute(
                """
                INSERT OR REPLACE INTO oauth_flows
                  (state_hash, flow_cipher, key_version, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    state_hash,
                    ciphertext,
                    self._active_version,
                    flow.created_at + self._flow_ttl_seconds,
                    flow.created_at,
                ),
            )
            connection.commit()

    def pop(self, state: str) -> PendingOAuthFlow | None:
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT flow_cipher, key_version, expires_at FROM oauth_flows WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            connection.execute("DELETE FROM oauth_flows WHERE state_hash = ?", (state_hash,))
            connection.commit()
        if row is None or float(row["expires_at"]) <= now:
            return None
        payload = self._decrypt_json(
            bytes(row["flow_cipher"]),
            key_version=int(row["key_version"]),
            aad=f"oauth-flow|{state_hash}".encode(),
        )
        if payload.get("state") != state:
            raise RuntimeError("OAuth flow state authentication failed")
        verifier = payload.get("verifier")
        redirect_uri = payload.get("redirect_uri")
        created_at = payload.get("created_at")
        if (
            not isinstance(verifier, str)
            or not isinstance(redirect_uri, str)
            or not isinstance(created_at, int | float)
        ):
            raise RuntimeError("Stored OAuth flow is invalid")
        return PendingOAuthFlow(verifier, redirect_uri, float(created_at))

    def prune(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM oauth_flows WHERE expires_at <= ?", (time.time(),))
            connection.commit()

    def reencrypt_to_active_key(self) -> tuple[int, int]:
        credential_count = 0
        flow_count = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute(
                "SELECT provider, credentials_cipher, key_version FROM oauth_credentials WHERE key_version != ?",
                (self._active_version,),
            ).fetchall():
                provider = str(row["provider"])
                payload = self._decrypt_json(
                    bytes(row["credentials_cipher"]),
                    key_version=int(row["key_version"]),
                    aad=f"oauth-credentials|{provider}".encode(),
                )
                connection.execute(
                    "UPDATE oauth_credentials SET credentials_cipher = ?, key_version = ? WHERE provider = ?",
                    (
                        self._encrypt_json(payload, aad=f"oauth-credentials|{provider}".encode()),
                        self._active_version,
                        provider,
                    ),
                )
                credential_count += 1
            for row in connection.execute(
                "SELECT state_hash, flow_cipher, key_version FROM oauth_flows WHERE key_version != ?",
                (self._active_version,),
            ).fetchall():
                state_hash = str(row["state_hash"])
                payload = self._decrypt_json(
                    bytes(row["flow_cipher"]),
                    key_version=int(row["key_version"]),
                    aad=f"oauth-flow|{state_hash}".encode(),
                )
                connection.execute(
                    "UPDATE oauth_flows SET flow_cipher = ?, key_version = ? WHERE state_hash = ?",
                    (
                        self._encrypt_json(payload, aad=f"oauth-flow|{state_hash}".encode()),
                        self._active_version,
                        state_hash,
                    ),
                )
                flow_count += 1
            connection.commit()
        return credential_count, flow_count

    def _encrypt_json(self, payload: dict[str, object], *, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return nonce + self._aesgcms[self._active_version].encrypt(nonce, plaintext, aad)

    def _decrypt_json(self, ciphertext: bytes, *, key_version: int, aad: bytes) -> dict[str, Any]:
        try:
            aesgcm = self._aesgcms[key_version]
        except KeyError as exc:
            raise RuntimeError("OAuth encryption key version is unavailable") from exc
        decoded = json.loads(aesgcm.decrypt(ciphertext[:12], ciphertext[12:], aad))
        if not isinstance(decoded, dict):
            raise RuntimeError("Stored OAuth payload is invalid")
        return decoded

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS oauth_credentials (
                  provider TEXT PRIMARY KEY,
                  credentials_cipher BLOB NOT NULL,
                  key_version INTEGER NOT NULL,
                  version INTEGER NOT NULL,
                  refresh_owner TEXT,
                  refresh_lease_expires_at REAL,
                  updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_flows (
                  state_hash TEXT PRIMARY KEY,
                  flow_cipher BLOB NOT NULL,
                  key_version INTEGER NOT NULL,
                  expires_at REAL NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                """
            )

    def _import_legacy_once(self) -> None:
        with self._connect() as connection:
            imported = connection.execute(
                "SELECT 1 FROM oauth_metadata WHERE key = 'legacy_import_complete'"
            ).fetchone()
        if imported is not None:
            return
        legacy_data: dict[str, object] = {}
        if self._legacy_path is not None and self._legacy_path.exists():
            legacy_data = FileOAuthCredentialStore(self._legacy_path)._read()
        for provider, payload in legacy_data.items():
            if isinstance(payload, dict) and self.get(provider) is None:
                self.set(provider, OAuthCredentials.from_json(payload))
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO oauth_metadata (key, value) VALUES ('legacy_import_complete', ?)",
                (str(time.time()),),
            )
            connection.commit()


class FileOAuthCredentialStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or oauth_store_path_from_env()

    @property
    def path(self) -> Path:
        return self._path

    def get(self, provider: str) -> OAuthCredentials | None:
        data = self._read()
        payload = data.get(provider)
        if not isinstance(payload, dict):
            return None
        return OAuthCredentials.from_json(payload)

    def set(self, provider: str, credentials: OAuthCredentials) -> None:
        data = self._read()
        data[provider] = credentials.to_json()
        self._write(data)

    def delete(self, provider: str) -> None:
        data = self._read()
        data.pop(provider, None)
        self._write(data)

    def _read(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OAuth credential store is invalid JSON: {self._path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"OAuth credential store must contain a JSON object: {self._path}")
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass


def build_oauth_credential_store_from_env(
    env: Mapping[str, str] | None = None,
) -> OAuthCredentialStore:
    lookup = os.environ if env is None else env
    if (
        lookup.get(OAUTH_ENCRYPTION_KEY_ENV, "").strip()
        or lookup.get(OAUTH_ENCRYPTION_KEYS_ENV, "").strip()
    ):
        return SQLiteEncryptedOAuthStore.from_env(lookup)
    return FileOAuthCredentialStore(Path(_required_env(lookup, OAUTH_STORE_PATH_ENV)).expanduser())


def build_oauth_flow_store_from_env(
    env: Mapping[str, str] | None = None, *, ttl_seconds: float = 600.0
) -> OAuthFlowStore | SQLiteEncryptedOAuthStore:
    lookup = os.environ if env is None else env
    if (
        lookup.get(OAUTH_ENCRYPTION_KEY_ENV, "").strip()
        or lookup.get(OAUTH_ENCRYPTION_KEYS_ENV, "").strip()
    ):
        return SQLiteEncryptedOAuthStore.from_env(lookup, flow_ttl_seconds=ttl_seconds)
    return OAuthFlowStore(ttl_seconds=ttl_seconds)


class GenericOAuthProvider:
    def __init__(
        self,
        *,
        config: GenericOAuthConfig | None = None,
        store: OAuthCredentialStore | None = None,
        client: httpx.AsyncClient | None = None,
        credential_key: str | None = None,
        credential_tenant_id: str | None = None,
        allow_global_credential_fallback: bool = False,
    ) -> None:
        self._config = config or generic_oauth_config_from_env()
        self._store = store or build_oauth_credential_store_from_env()
        self._client = client
        if credential_key is not None and credential_tenant_id is not None:
            raise ValueError("Provide either credential_key or credential_tenant_id, not both")
        self._credential_key = credential_key or (
            tenant_oauth_credential_key(self._config.provider_id, credential_tenant_id)
            if credential_tenant_id is not None
            else self._config.provider_id
        )
        self._fallback_credential_key = (
            self._config.provider_id
            if allow_global_credential_fallback and self._credential_key != self._config.provider_id
            else None
        )

    @property
    def provider_id(self) -> str:
        return self._config.provider_id

    def start_login(self) -> tuple[OAuthLoginStart, PendingOAuthFlow]:
        verifier = _token_urlsafe(64)
        challenge = _pkce_challenge(verifier)
        state = secrets.token_hex(16)
        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "scope": self._config.scope,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            **self._config.auth_params,
        }
        return (
            OAuthLoginStart(
                authorization_url=f"{self._config.authorize_url}?{urlencode(params)}",
                state=state,
            ),
            PendingOAuthFlow(
                verifier=verifier,
                redirect_uri=self._config.redirect_uri,
                created_at=time.time(),
            ),
        )

    async def complete_login(self, *, code: str, flow: PendingOAuthFlow) -> OAuthCredentials:
        credentials = await self._exchange_token(
            {
                "grant_type": "authorization_code",
                "client_id": self._config.client_id,
                "code": code,
                "code_verifier": flow.verifier,
                "redirect_uri": flow.redirect_uri,
            }
        )
        self._store.set(self._credential_key, credentials)
        return credentials

    async def get_credentials(self) -> OAuthCredentials | None:
        credential_keys = [self._credential_key]
        if self._fallback_credential_key is not None:
            credential_keys.append(self._fallback_credential_key)
        for credential_key in credential_keys:
            if isinstance(self._store, CoordinatedOAuthCredentialStore):
                credentials = await self._get_coordinated_credentials(self._store, credential_key)
                if credentials is not None:
                    return credentials
                continue
            credentials = self._store.get(credential_key)
            if credentials is None:
                continue
            if time.time() < credentials.expires_at - 60:
                return credentials
            refreshed = await self.refresh(credentials)
            self._store.set(credential_key, refreshed)
            return refreshed
        return None

    async def _get_coordinated_credentials(
        self, store: CoordinatedOAuthCredentialStore, credential_key: str
    ) -> OAuthCredentials | None:
        owner = uuid4().hex
        deadline = time.monotonic() + OAUTH_REFRESH_WAIT_SECONDS
        while True:
            versioned = store.get_versioned(credential_key)
            if versioned is None:
                return None
            credentials, version = versioned
            if time.time() < credentials.expires_at - 60:
                return credentials
            claimed = store.try_claim_refresh(
                credential_key,
                version=version,
                owner=owner,
                lease_seconds=OAUTH_REFRESH_LEASE_SECONDS,
            )
            if claimed:
                try:
                    refreshed = await self.refresh(credentials)
                    if store.complete_refresh(
                        credential_key,
                        version=version,
                        owner=owner,
                        credentials=refreshed,
                    ):
                        return refreshed
                except BaseException:
                    store.release_refresh(credential_key, version=version, owner=owner)
                    raise
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for coordinated OAuth token refresh")
            await asyncio.sleep(0.1)

    async def refresh(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return await self._exchange_token(
            {
                "grant_type": "refresh_token",
                "client_id": self._config.client_id,
                "refresh_token": credentials.refresh_token,
            }
        )

    async def _exchange_token(self, form: dict[str, str]) -> OAuthCredentials:
        if self._client is None:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await _exchange_generic_token(client, self._config, form)
        return await _exchange_generic_token(self._client, self._config, form)


async def _exchange_generic_token(
    client: httpx.AsyncClient,
    config: GenericOAuthConfig,
    form: dict[str, str],
) -> OAuthCredentials:
    response = await client.post(
        config.token_url,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        raise RuntimeError(f"OAuth token request failed ({response.status_code}): {response.text}")
    payload = response.json()
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("OAuth token response missing access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("OAuth token response missing refresh_token")
    if not isinstance(expires_in, int | float):
        raise RuntimeError("OAuth token response missing expires_in")
    return OAuthCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + float(expires_in),
        account_id=extract_jwt_claim(access_token, config.account_id_jwt_claim),
    )


def extract_jwt_claim(access_token: str, claim_path: str | None) -> str | None:
    if not claim_path:
        return None
    payload: object = jwt.decode(access_token, options={"verify_signature": False})
    if not isinstance(payload, dict):
        raise RuntimeError("OAuth access token payload must be a JWT object")
    if isinstance(payload.get(claim_path), str):
        return payload[claim_path]
    if "#" in claim_path:
        top_level_claim, nested_path = claim_path.split("#", 1)
        current: object = payload.get(top_level_claim)
        for part in nested_path.split("."):
            if not isinstance(current, dict):
                raise RuntimeError(f"OAuth token missing JWT claim '{claim_path}'")
            current = current.get(part)
    else:
        current = payload
        for part in claim_path.split("."):
            if not isinstance(current, dict):
                raise RuntimeError(f"OAuth token missing JWT claim '{claim_path}'")
            current = current.get(part)
    if not isinstance(current, str) or not current:
        raise RuntimeError(f"OAuth token JWT claim '{claim_path}' must be a non-empty string")
    return current


def tenant_oauth_credential_key(provider_id: str, tenant_id: str) -> str:
    return f"{provider_id}:tenant:{tenant_id}"


def oauth_store_path_from_env() -> Path:
    return OAuthStoreSettings.from_env().path


def generic_oauth_config_from_env() -> GenericOAuthConfig:
    return GenericOAuthConfig.from_env()


def oauth_settings_from_env() -> OAuthSettings:
    return OAuthSettings.from_env()


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _json_string_map_env(env: Mapping[str, str], name: str) -> dict[str, str]:
    raw = env.get(name, "").strip()
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise RuntimeError(f"{name} must be a JSON object of string values")
    return dict(payload)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _token_urlsafe(length: int) -> str:
    token = secrets.token_urlsafe(length)
    return token[:128]
