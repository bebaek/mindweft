from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

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
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        try:
            self._path.chmod(0o600)
        except OSError:
            pass


class GenericOAuthProvider:
    def __init__(
        self,
        *,
        config: GenericOAuthConfig | None = None,
        store: FileOAuthCredentialStore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or generic_oauth_config_from_env()
        self._store = store or FileOAuthCredentialStore()
        self._client = client

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
        self._store.set(self._config.provider_id, credentials)
        return credentials

    async def get_credentials(self) -> OAuthCredentials | None:
        credentials = self._store.get(self._config.provider_id)
        if credentials is None:
            return None
        if time.time() < credentials.expires_at - 60:
            return credentials
        refreshed = await self.refresh(credentials)
        self._store.set(self._config.provider_id, refreshed)
        return refreshed

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


def oauth_store_path_from_env() -> Path:
    raw = _required_env(OAUTH_STORE_PATH_ENV)
    return Path(raw).expanduser()


def generic_oauth_config_from_env() -> GenericOAuthConfig:
    return GenericOAuthConfig(
        provider_id=_required_env(OAUTH_PROVIDER_ID_ENV),
        client_id=_required_env(OAUTH_CLIENT_ID_ENV),
        authorize_url=_required_env(OAUTH_AUTHORIZE_URL_ENV),
        token_url=_required_env(OAUTH_TOKEN_URL_ENV),
        redirect_uri=_required_env(OAUTH_REDIRECT_URI_ENV),
        scope=_required_env(OAUTH_SCOPE_ENV),
        auth_params=_json_string_map_env(OAUTH_AUTH_PARAMS_ENV),
        account_id_jwt_claim=os.getenv(OAUTH_ACCOUNT_ID_JWT_CLAIM_ENV, "").strip() or None,
    )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _json_string_map_env(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
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
