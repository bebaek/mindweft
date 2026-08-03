import asyncio
import base64
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.execution import GenericOAuthExportSettings
from app.llm import GenericOAuthResponsesAdapter, build_llm_adapter_from_env
from app.main import create_app
from app.models import Message, MessageRole, ToolSpec
from app.oauth import (
    GENERIC_OAUTH_PROVIDER,
    FileOAuthCredentialStore,
    GenericOAuthConfig,
    GenericOAuthProvider,
    OAuthCredentials,
    OAuthSettings,
    OAuthStoreSettings,
    PendingOAuthFlow,
    SQLiteEncryptedOAuthStore,
    extract_jwt_claim,
    generic_oauth_config_from_env,
    oauth_store_path_from_env,
)
from app.tools import build_local_tool_registry


def _token(account_id: str) -> str:
    return jwt.encode({"auth": {"account_id": account_id}}, "secret", algorithm="HS256")


def test_generic_oauth_provider_supports_tenant_credential_key(tmp_path: Path) -> None:
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    config = GenericOAuthConfig(
        provider_id="openai-codex",
        client_id="client",
        authorize_url="https://example.com/authorize",
        token_url="https://example.com/token",
        redirect_uri="http://localhost/callback",
        scope="openid",
        auth_params={},
    )
    global_credentials = OAuthCredentials("global-access", "global-refresh", time.time() + 3600)
    tenant_credentials = OAuthCredentials("tenant-access", "tenant-refresh", time.time() + 3600)
    store.set("openai-codex", global_credentials)
    store.set("openai-codex:tenant:tenant-1", tenant_credentials)

    provider = GenericOAuthProvider(
        config=config,
        store=store,
        credential_tenant_id="tenant-1",
        allow_global_credential_fallback=True,
    )

    assert asyncio.run(provider.get_credentials()) == tenant_credentials


def test_generic_oauth_provider_keeps_global_fallback_opt_in(tmp_path: Path) -> None:
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    config = GenericOAuthConfig(
        provider_id="openai",
        client_id="client",
        authorize_url="https://example.com/authorize",
        token_url="https://example.com/token",
        redirect_uri="http://localhost/callback",
        scope="openid",
        auth_params={},
    )
    store.set("openai", OAuthCredentials("global-access", "global-refresh", time.time() + 3600))

    provider = GenericOAuthProvider(
        config=config,
        store=store,
        credential_tenant_id="tenant-1",
    )

    assert asyncio.run(provider.get_credentials()) is None


def test_generic_oauth_provider_can_fall_back_to_global_credential(tmp_path: Path) -> None:
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    config = GenericOAuthConfig(
        provider_id="openai",
        client_id="client",
        authorize_url="https://example.com/authorize",
        token_url="https://example.com/token",
        redirect_uri="http://localhost/callback",
        scope="openid",
        auth_params={},
    )
    global_credentials = OAuthCredentials("global-access", "global-refresh", time.time() + 3600)
    store.set("openai", global_credentials)

    provider = GenericOAuthProvider(
        config=config,
        store=store,
        credential_tenant_id="demo-tenant",
        allow_global_credential_fallback=True,
    )

    assert asyncio.run(provider.get_credentials()) == global_credentials


def test_generic_oauth_export_settings_from_env_mapping(tmp_path: Path) -> None:
    settings = GenericOAuthExportSettings.from_env(
        {
            "MINIGENT_OAUTH_STORE_PATH": str(tmp_path / "oauth.json"),
            "MINIGENT_OAUTH_PROVIDER_ID": "chatgpt",
            "MINIGENT_OAUTH_CLIENT_ID": "client-id",
            "MINIGENT_OAUTH_AUTHORIZE_URL": "https://example.com/authorize",
            "MINIGENT_OAUTH_TOKEN_URL": "https://example.com/token",
            "MINIGENT_OAUTH_REDIRECT_URI": "http://127.0.0.1/callback",
            "MINIGENT_OAUTH_SCOPE": "openid profile",
            "MINIGENT_OAUTH_ACCOUNT_ID_JWT_CLAIM": "auth.account_id",
            "MINIGENT_OAUTH_AUTH_PARAMS": json.dumps({"prompt": "login"}),
        }
    )

    assert settings.public_dict({"provider": GENERIC_OAUTH_PROVIDER}) == {
        "store_path": str(tmp_path / "oauth.json"),
        "provider_id": "chatgpt",
        "client_id": "client-id",
        "authorize_url": "https://example.com/authorize",
        "token_url": "https://example.com/token",
        "redirect_uri": "http://127.0.0.1/callback",
        "scope": "openid profile",
        "account_id_jwt_claim": "auth.account_id",
        "auth_params": {"prompt": "login"},
    }
    assert settings.public_dict({"provider": "mock"}) == {}


def _config(tmp_path: Path) -> GenericOAuthConfig:
    return GenericOAuthConfig(
        provider_id="test-oauth",
        client_id="client-id",
        authorize_url="https://auth.example/authorize",
        token_url="https://auth.example/token",
        redirect_uri="http://127.0.0.1:8000/oauth/generic/callback",
        scope="openid offline_access",
        auth_params={"prompt": "login"},
        account_id_jwt_claim="auth.account_id",
    )


def _oauth_env(tmp_path: Path) -> dict[str, str]:
    return {
        "MINIGENT_OAUTH_STORE_PATH": str(tmp_path / "oauth.json"),
        "MINIGENT_OAUTH_PROVIDER_ID": "test-oauth",
        "MINIGENT_OAUTH_CLIENT_ID": "client-id",
        "MINIGENT_OAUTH_AUTHORIZE_URL": "https://auth.example/authorize",
        "MINIGENT_OAUTH_TOKEN_URL": "https://auth.example/token",
        "MINIGENT_OAUTH_REDIRECT_URI": "http://127.0.0.1:8000/oauth/generic/callback",
        "MINIGENT_OAUTH_SCOPE": "openid offline_access",
        "MINIGENT_OAUTH_AUTH_PARAMS": '{"prompt":"login"}',
        "MINIGENT_OAUTH_ACCOUNT_ID_JWT_CLAIM": "auth.account_id",
    }


def test_oauth_settings_from_env_mapping_parses_values(tmp_path: Path) -> None:
    settings = OAuthSettings.from_env(_oauth_env(tmp_path))

    assert settings == OAuthSettings(
        store=OAuthStoreSettings(path=tmp_path / "oauth.json"),
        provider=_config(tmp_path),
    )


def test_generic_oauth_config_from_env_mapping_allows_missing_store_path(tmp_path: Path) -> None:
    env = _oauth_env(tmp_path)
    env.pop("MINIGENT_OAUTH_STORE_PATH")

    assert GenericOAuthConfig.from_env(env) == _config(tmp_path)


def test_oauth_store_path_from_env_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIGENT_OAUTH_STORE_PATH", "~/minigent-oauth.json")

    assert oauth_store_path_from_env() == Path("~/minigent-oauth.json").expanduser()


def test_generic_oauth_config_from_env_rejects_missing_required_value() -> None:
    env = _oauth_env(Path("/tmp"))
    env.pop("MINIGENT_OAUTH_CLIENT_ID")

    with pytest.raises(RuntimeError) as exc_info:
        GenericOAuthConfig.from_env(env)

    assert str(exc_info.value) == "MINIGENT_OAUTH_CLIENT_ID is required"


def test_generic_oauth_config_from_env_rejects_invalid_auth_params() -> None:
    env = _oauth_env(Path("/tmp"))
    env["MINIGENT_OAUTH_AUTH_PARAMS"] = '{"prompt": true}'

    with pytest.raises(RuntimeError) as exc_info:
        GenericOAuthConfig.from_env(env)

    assert (
        str(exc_info.value) == "MINIGENT_OAUTH_AUTH_PARAMS must be a JSON object of string values"
    )


def test_generic_oauth_config_from_env_reads_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key, value in _oauth_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    assert generic_oauth_config_from_env() == _config(tmp_path)


def test_generic_oauth_extracts_jwt_claim() -> None:
    assert extract_jwt_claim(_token("acct_test"), "auth.account_id") == "acct_test"
    assert extract_jwt_claim(_token("acct_test"), None) is None


def test_file_oauth_store_round_trips_credentials(tmp_path: Path) -> None:
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    credentials = OAuthCredentials(
        access_token=_token("acct_test"),
        refresh_token="refresh-token",
        expires_at=time.time() + 3600,
        account_id="acct_test",
    )

    store.set("test-oauth", credentials)

    assert store.get("test-oauth") == credentials
    assert (
        json.loads(store.path.read_text(encoding="utf-8"))["test-oauth"]["account_id"]
        == "acct_test"
    )


def test_sqlite_oauth_store_encrypts_credentials_and_shares_flows(tmp_path: Path) -> None:
    database = tmp_path / "oauth.db"
    store_a = SQLiteEncryptedOAuthStore(database, keyring={1: b"a" * 32}, active_version=1)
    store_b = SQLiteEncryptedOAuthStore(database, keyring={1: b"a" * 32}, active_version=1)
    credentials = OAuthCredentials(
        access_token="secret-access-token",
        refresh_token="secret-refresh-token",
        expires_at=time.time() + 3600,
        account_id="secret-account",
    )

    store_a.set("test-oauth", credentials)
    flow = PendingOAuthFlow("secret-verifier", "https://example.test/callback", time.time())
    store_a.put("secret-state", flow)

    assert store_b.get("test-oauth") == credentials
    assert store_b.pop("secret-state") == flow
    assert store_a.pop("secret-state") is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM oauth_credentials").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM oauth_flows").fetchone()[0] == 0
    for path in tmp_path.glob("oauth.db*"):
        content = path.read_bytes()
        assert b"secret-access-token" not in content
        assert b"secret-refresh-token" not in content
        assert b"secret-verifier" not in content
        assert b"secret-state" not in content


def test_sqlite_oauth_store_imports_legacy_file_once(tmp_path: Path) -> None:
    legacy = tmp_path / "oauth.json"
    credentials = OAuthCredentials(
        access_token="legacy-access",
        refresh_token="legacy-refresh",
        expires_at=time.time() + 3600,
    )
    FileOAuthCredentialStore(legacy).set("test-oauth", credentials)
    database = tmp_path / "oauth.db"

    imported = SQLiteEncryptedOAuthStore(
        database,
        keyring={1: b"a" * 32},
        active_version=1,
        legacy_path=legacy,
    )
    assert imported.get("test-oauth") == credentials

    FileOAuthCredentialStore(legacy).set(
        "test-oauth",
        OAuthCredentials("stale-access", "stale-refresh", time.time() + 7200),
    )
    reopened = SQLiteEncryptedOAuthStore(
        database,
        keyring={1: b"a" * 32},
        active_version=1,
        legacy_path=legacy,
    )
    assert reopened.get("test-oauth") == credentials


def test_sqlite_oauth_store_coordinates_concurrent_refresh(tmp_path: Path) -> None:
    database = tmp_path / "oauth.db"
    initial = OAuthCredentials("expired-access", "single-use-refresh", time.time() - 1)
    first_store = SQLiteEncryptedOAuthStore(database, keyring={1: b"a" * 32}, active_version=1)
    first_store.set("test-oauth", initial)
    refresh_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "access_token": _token("acct_test"),
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )

    first_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    second_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = GenericOAuthProvider(
        config=_config(tmp_path),
        store=first_store,
        client=first_client,
    )
    second = GenericOAuthProvider(
        config=_config(tmp_path),
        store=SQLiteEncryptedOAuthStore(database, keyring={1: b"a" * 32}, active_version=1),
        client=second_client,
    )

    async def run() -> tuple[OAuthCredentials | None, OAuthCredentials | None]:
        try:
            return await asyncio.gather(first.get_credentials(), second.get_credentials())
        finally:
            await first_client.aclose()
            await second_client.aclose()

    first_result, second_result = asyncio.run(run())

    assert refresh_calls == 1
    assert first_result == second_result
    assert first_result is not None
    assert first_result.refresh_token == "rotated-refresh"


def test_sqlite_oauth_store_rotates_encryption_keys(tmp_path: Path) -> None:
    database = tmp_path / "oauth.db"
    old_store = SQLiteEncryptedOAuthStore(database, keyring={1: b"a" * 32}, active_version=1)
    credentials = OAuthCredentials("access", "refresh", time.time() + 3600)
    old_store.set("test-oauth", credentials)
    old_store.put("state", PendingOAuthFlow("verifier", "https://callback", time.time()))

    rotating = SQLiteEncryptedOAuthStore(
        database,
        keyring={1: b"a" * 32, 2: b"b" * 32},
        active_version=2,
    )
    assert rotating.reencrypt_to_active_key() == (1, 1)
    new_only = SQLiteEncryptedOAuthStore(database, keyring={2: b"b" * 32}, active_version=2)
    assert new_only.get("test-oauth") == credentials
    assert new_only.pop("state") is not None
    old_only = SQLiteEncryptedOAuthStore(database, keyring={1: b"a" * 32}, active_version=1)
    with pytest.raises(RuntimeError, match="key version is unavailable"):
        old_only.get("test-oauth")


def test_sqlite_oauth_store_from_env_uses_versioned_keyring(tmp_path: Path) -> None:
    key = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
    env = _oauth_env(tmp_path)
    env["MINIGENT_OAUTH_STORE_PATH"] = str(tmp_path / "oauth.db")
    env["MINIGENT_OAUTH_ENCRYPTION_KEYS"] = json.dumps({"3": key})
    env["MINIGENT_OAUTH_KEY_VERSION"] = "3"

    store = SQLiteEncryptedOAuthStore.from_env(env)
    store.set("test-oauth", OAuthCredentials("access", "refresh", time.time() + 3600))

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT key_version FROM oauth_credentials").fetchone() == (3,)


def test_generic_oauth_adapter_sends_headers_and_parses_text(tmp_path: Path) -> None:
    seen_headers: dict[str, str] = {}
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_headers, seen_payload
        seen_headers = dict(request.headers)
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text='data: {"type":"response.output_text.delta","delta":"hello from oauth"}\n\ndata: [DONE]\n\n',
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        extra_headers={"x-extra": "yes"},
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_local_tool_registry().specs(),
        )
    )

    assert response.content == "hello from oauth"
    assert seen_headers["authorization"].startswith("Bearer ")
    assert seen_headers["accept"] == "text/event-stream, application/json"
    assert seen_headers["x-extra"] == "yes"
    assert seen_headers["session_id"] == "thread"
    assert seen_headers["session-id"] == "thread"
    assert seen_headers["thread-id"] == "thread"
    assert seen_headers["x-client-request-id"] == "thread"
    assert seen_payload["model"] == "test-model"
    assert seen_payload["stream"] is True
    assert seen_payload["prompt_cache_key"] == "thread"


def test_generic_oauth_adapter_sends_prompt_cache_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": _token("acct_test"), "expires_in": 3600}
            )
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text='data: {"type":"response.output_text.delta","delta":"ok"}\n\ndata: [DONE]\n\n',
        )

    monkeypatch.setenv("MINIGENT_LLM_PROMPT_CACHE_KEY", "minigent-debug-cache")
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "ok"
    assert seen_payload["prompt_cache_key"] == "minigent-debug-cache"


def test_generic_oauth_adapter_supports_thread_prompt_cache_key_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": _token("acct_test"), "expires_in": 3600}
            )
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text='data: {"type":"response.output_text.delta","delta":"ok"}\n\ndata: [DONE]\n\n',
        )

    monkeypatch.setenv("MINIGENT_LLM_PROMPT_CACHE_KEY", "thread")
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread-123", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "ok"
    assert seen_payload["prompt_cache_key"] == "thread-123"


def test_generic_oauth_adapter_matches_pi_codex_include_for_chatgpt_codex_url(
    tmp_path: Path,
) -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": _token("acct_test"), "expires_in": 3600}
            )
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text='data: {"type":"response.output_text.delta","delta":"ok"}\n\ndata: [DONE]\n\n',
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://chatgpt.com/backend-api/codex/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread-123", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "ok"
    assert seen_payload["include"] == ["reasoning.encrypted_content"]


def test_generic_oauth_adapter_round_trips_encrypted_reasoning_items(tmp_path: Path) -> None:
    seen_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        seen_payload = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "id": "rs_1",
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "thought"}],
                        "encrypted_content": "encrypted-reasoning",
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    },
                ]
            },
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="call a tool"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="echo",
                    tool_call_id="call_1",
                    tool_arguments={"text": "hello"},
                    metadata={
                        "generic_oauth_responses_output_items": [
                            {
                                "id": "rs_0",
                                "type": "reasoning",
                                "encrypted_content": "prior-encrypted-reasoning",
                            }
                        ]
                    },
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content="hello",
                    tool_call_id="call_1",
                ),
            ],
            [ToolSpec(name="echo", description="Echo text", input_schema={"type": "object"})],
        )
    )

    assert response.content == "ok"
    assert response.metadata == {
        "generic_oauth_responses_output_items": [
            {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "thought"}],
                "encrypted_content": "encrypted-reasoning",
            }
        ]
    }
    assert seen_payload["input"][1] == {
        "id": "rs_0",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "prior-encrypted-reasoning",
    }


def test_generic_oauth_adapter_dedupes_repeated_reasoning_items(
    tmp_path: Path,
) -> None:
    seen_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.read().decode()))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            },
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )
    metadata = {
        "generic_oauth_responses_output_items": [
            {"id": "rs_duplicate", "type": "reasoning", "encrypted_content": "encrypted"}
        ]
    }

    response = asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="call tools"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="current_time",
                    tool_call_id="call_time",
                    metadata=metadata,
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"current_time":"now"}',
                    tool_call_id="call_time",
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="calculator",
                    tool_call_id="call_calc",
                    tool_arguments={"expression": "2 + 2"},
                    metadata=metadata,
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"result":4}',
                    tool_call_id="call_calc",
                ),
            ],
            build_local_tool_registry(allowed_tools=["current_time", "calculator"]).specs(),
        )
    )

    assert response.content == "ok"
    reasoning_items = [item for item in seen_payload["input"] if item.get("type") == "reasoning"]
    assert [item["id"] for item in reasoning_items] == ["rs_duplicate"]


def test_generic_oauth_adapter_prunes_orphaned_tool_outputs_for_chatgpt_codex(
    tmp_path: Path,
) -> None:
    seen_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.read().decode()))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            },
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://chatgpt.com/backend-api/codex/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.USER, content="old request"),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"echo":"old result"}',
                    tool_name="echo",
                    tool_call_id="call_orphaned",
                ),
                Message(thread_id="thread", role=MessageRole.ASSISTANT, content="old answer"),
                Message(
                    thread_id="thread",
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_name="echo",
                    tool_call_id="call_complete",
                    tool_arguments={"text": "hello"},
                ),
                Message(
                    thread_id="thread",
                    role=MessageRole.TOOL,
                    content='{"echo":"hello"}',
                    tool_name="echo",
                    tool_call_id="call_complete",
                ),
                Message(thread_id="thread", role=MessageRole.USER, content="new request"),
            ],
            [ToolSpec(name="echo", description="Echo text", input_schema={"type": "object"})],
        )
    )

    assert response.content == "ok"
    assert seen_payload["input"] == [
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {
            "type": "function_call",
            "call_id": "call_complete",
            "name": "echo",
            "arguments": '{"text": "hello"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_complete",
            "output": '{"echo":"hello"}',
        },
        {"role": "user", "content": "new request"},
    ]


def test_generic_oauth_adapter_retries_once_after_reasoning_only_output(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.read().decode()))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "id": "rs_retry",
                            "type": "reasoning",
                            "summary": [],
                            "encrypted_content": "retry-reasoning",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "continued ok"}],
                    }
                ]
            },
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "continued ok"
    assert len(requests) == 2
    assert requests[1]["input"][-1] == {
        "id": "rs_retry",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "retry-reasoning",
    }


def test_generic_oauth_adapter_auto_continues_repeated_reasoning_only_output(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.read().decode()))
        if len(requests) <= 3:
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "id": f"rs_retry_{len(requests)}",
                            "type": "reasoning",
                            "summary": [],
                            "encrypted_content": f"retry-reasoning-{len(requests)}",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "continued ok"}],
                    }
                ]
            },
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "continued ok"
    assert len(requests) == 4
    assert [item["id"] for item in requests[-1]["input"][-3:]] == [
        "rs_retry_1",
        "rs_retry_2",
        "rs_retry_3",
    ]


def test_generic_oauth_adapter_reports_reasoning_only_stall_after_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.read().decode()))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "id": f"rs_retry_{len(requests)}",
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": f"retry-reasoning-{len(requests)}",
                    }
                ]
            },
        )

    monkeypatch.setenv("MINIGENT_RESPONSES_REASONING_ONLY_RETRIES", "1")
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    assert len(requests) == 2
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == {
        "type": "provider_reasoning_only",
        "message": "Generic OAuth LLM returned reasoning state but no assistant message or tool call.",
        "provider": GENERIC_OAUTH_PROVIDER,
        "retryable": True,
        "reasoning_item_count": 1,
        "continuation_attempts": 1,
    }


def test_generic_oauth_adapter_can_log_raw_response_for_debugging(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": _token("acct_test"), "expires_in": 3600}
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "debug ok"}]}
                ],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            },
        )

    monkeypatch.setenv("MINIGENT_LLM_DEBUG_LOG_RESPONSES", "true")
    monkeypatch.setenv("MINIGENT_LLM_DEBUG_LOG_RESPONSE_MAX_CHARS", "60")
    caplog.set_level(logging.INFO, logger="app.llm")
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "debug ok"
    assert "LLM raw response adapter=generic-oauth" in caplog.text
    assert "truncated=True" in caplog.text


def test_generic_oauth_adapter_can_write_raw_response_debug_jsonl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": _token("acct_test"), "expires_in": 3600}
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "debug file"}]}
                ],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            },
        )

    debug_path = tmp_path / "raw.jsonl"
    monkeypatch.setenv("MINIGENT_LLM_DEBUG_LOG_RESPONSES", "true")
    monkeypatch.setenv("MINIGENT_LLM_DEBUG_RESPONSE_LOG_PATH", str(debug_path))
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "debug file"
    records = [json.loads(line) for line in debug_path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["adapter"] == "generic-oauth"
    assert "debug file" in records[0]["body"]


def test_generic_oauth_adapter_sends_system_messages_as_instructions(tmp_path: Path) -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": _token("acct_test"), "expires_in": 3600}
            )
        seen_payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]
            },
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [
                Message(thread_id="thread", role=MessageRole.SYSTEM, content="system rules"),
                Message(thread_id="thread", role=MessageRole.SYSTEM, content="workspace rules"),
                Message(thread_id="thread", role=MessageRole.USER, content="hello"),
            ],
            [],
        )
    )

    assert response.content == "ok"
    assert seen_payload["instructions"] == "system rules\n\nworkspace rules"
    assert seen_payload["input"] == [{"role": "user", "content": "hello"}]


def test_generic_oauth_adapter_parses_sse_with_event_prefix(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text=(
                "event: response.created\n"
                'data: {"type":"response.created","response":{"id":"resp_1","status":"in_progress"}}\n\n'
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":" from event-prefixed sse"}\n\n'
                "event: done\n"
                "data: [DONE]\n\n"
            ),
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_local_tool_registry().specs(),
        )
    )

    assert response.content == "hello from event-prefixed sse"


def test_generic_oauth_adapter_parses_sse_usage(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
                'data: {"type":"response.completed","response":{"usage":{"input_tokens":1200,"output_tokens":25,"total_tokens":1225,"input_tokens_details":{"cached_tokens":900}}}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            build_local_tool_registry().specs(),
        )
    )

    assert response.content == "hello"
    assert response.usage == {
        "prompt_tokens": 1200,
        "input_tokens": 1200,
        "completion_tokens": 25,
        "output_tokens": 25,
        "total_tokens": 1225,
        "cache_read_tokens": 900,
    }


def test_generic_oauth_adapter_surfaces_json_response_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_05b5f92658817c20016a64d98c8fa88197bf9718aa5d401b7d",
                "object": "response",
                "created_at": 1784994188,
                "status": "failed",
                "background": False,
                "completed_at": None,
                "error": {
                    "code": "server_is_overloaded",
                    "message": "Our servers are currently overloaded. Please try again later.",
                },
            },
        )

    caplog.set_level(logging.ERROR, logger="app.llm")
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == (
        "Generic OAuth LLM response failed: server_is_overloaded: "
        "Our servers are currently overloaded. Please try again later."
    )
    assert "detail=Generic OAuth LLM response failed: server_is_overloaded" in caplog.text
    assert "Our servers are currently overloaded. Please try again later." in caplog.text


def test_generic_oauth_adapter_surfaces_sse_response_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"type":"response.failed","response":{"id":"resp_failed","status":"failed",'
                '"error":{"code":"server_error","message":"upstream failed"}}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    assert exc_info.value.status_code == 502
    assert (
        exc_info.value.detail == "Generic OAuth LLM response failed: server_error: upstream failed"
    )


class _BrokenAfterChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"type":"response.output_text.delta","delta":"partial ok"}\n\n'
        raise httpx.ReadError("")

    async def aclose(self) -> None:
        return None


def test_generic_oauth_adapter_uses_partial_sse_body_after_empty_read_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_BrokenAfterChunkStream(),
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
            [],
        )
    )

    assert response.content == "partial ok"


def test_generic_oauth_adapter_accumulates_streamed_function_arguments(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"fc_1","type":"function_call","call_id":"call_1","name":"echo","arguments":""}}\n\n'
                'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\"text\\":"}\n\n'
                'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"\\"hello\\"}"}\n\n'
                'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"fc_1","type":"function_call","call_id":"call_1","name":"echo","arguments":"{\\"text\\":\\"hello\\"}"}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("acct_test"),
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
            account_id="acct_test",
        ),
    )
    provider = GenericOAuthProvider(
        config=_config(tmp_path),
        store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    adapter = GenericOAuthResponsesAdapter(
        url="https://example.test/responses",
        model="test-model",
        oauth_provider=provider,
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        adapter.generate(
            [Message(thread_id="thread", role=MessageRole.USER, content="echo hello")],
            [ToolSpec(name="echo", description="Echo text", input_schema={"type": "object"})],
        )
    )

    assert response.tool_call is not None
    assert response.tool_call.name == "echo"
    assert response.tool_call.arguments == {"text": "hello"}


def test_build_llm_adapter_from_env_supports_generic_oauth(monkeypatch) -> None:
    monkeypatch.setenv("MINIGENT_LLM_PROVIDER", GENERIC_OAUTH_PROVIDER)
    monkeypatch.setenv("MINIGENT_LLM_MODEL", "test-model")
    monkeypatch.setenv("MINIGENT_LLM_URL", "https://example.test/responses")
    monkeypatch.setenv("MINIGENT_OAUTH_STORE_PATH", "/tmp/minigent-oauth-test.json")
    monkeypatch.setenv("MINIGENT_OAUTH_PROVIDER_ID", "test-oauth")
    monkeypatch.setenv("MINIGENT_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("MINIGENT_OAUTH_AUTHORIZE_URL", "https://auth.example/authorize")
    monkeypatch.setenv("MINIGENT_OAUTH_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv(
        "MINIGENT_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/oauth/generic/callback"
    )
    monkeypatch.setenv("MINIGENT_OAUTH_SCOPE", "openid offline_access")

    adapter = build_llm_adapter_from_env()

    assert isinstance(adapter, GenericOAuthResponsesAdapter)
    assert adapter.describe()["provider"] == GENERIC_OAUTH_PROVIDER


def test_generic_oauth_login_endpoint_returns_authorization_url(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MINIGENT_OAUTH_STORE_PATH", str(tmp_path / "oauth.json"))
    monkeypatch.setenv("MINIGENT_OAUTH_PROVIDER_ID", "test-oauth")
    monkeypatch.setenv("MINIGENT_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("MINIGENT_OAUTH_AUTHORIZE_URL", "https://auth.example/authorize")
    monkeypatch.setenv("MINIGENT_OAUTH_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv(
        "MINIGENT_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/oauth/generic/callback"
    )
    monkeypatch.setenv("MINIGENT_OAUTH_SCOPE", "openid offline_access")
    monkeypatch.setenv("MINIGENT_OAUTH_AUTH_PARAMS", '{"prompt":"login"}')
    client = TestClient(create_app(tool_registry=build_local_tool_registry()))

    response = client.get("/oauth/generic/login")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "test-oauth"
    assert payload["authorization_url"].startswith("https://auth.example/authorize?")
    assert "code_challenge=" in payload["authorization_url"]
    assert "prompt=login" in payload["authorization_url"]
    assert payload["state"]
    alias_response = client.get("/auth/callback")
    assert alias_response.status_code == 400
    assert "Missing code or state" in alias_response.text


def test_generic_oauth_provider_refreshes_expired_credentials(tmp_path: Path) -> None:
    store = FileOAuthCredentialStore(tmp_path / "oauth.json")
    store.set(
        "test-oauth",
        OAuthCredentials(
            access_token=_token("old_account"),
            refresh_token="old-refresh",
            expires_at=time.time() - 60,
            account_id="old_account",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(item.split("=") for item in request.read().decode().split("&"))
        assert form["grant_type"] == "refresh_token"
        return httpx.Response(
            200,
            json={
                "access_token": _token("new_account"),
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    async def run() -> OAuthCredentials | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = GenericOAuthProvider(config=_config(tmp_path), store=store, client=client)
            return await provider.get_credentials()

    credentials = asyncio.run(run())

    assert credentials is not None
    assert credentials.account_id == "new_account"
    assert credentials.refresh_token == "new-refresh"
    assert store.get("test-oauth") == credentials
