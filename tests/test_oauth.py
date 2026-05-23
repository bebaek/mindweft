import asyncio
import json
import time
from pathlib import Path

import httpx
import jwt
from fastapi.testclient import TestClient

from app.llm import GenericOAuthResponsesAdapter, build_llm_adapter_from_env
from app.main import create_app
from app.models import Message, MessageRole, ToolSpec
from app.oauth import (
    GENERIC_OAUTH_PROVIDER,
    FileOAuthCredentialStore,
    GenericOAuthConfig,
    GenericOAuthProvider,
    OAuthCredentials,
    extract_jwt_claim,
)
from app.tools import build_local_tool_registry


def _token(account_id: str) -> str:
    return jwt.encode({"auth": {"account_id": account_id}}, "secret", algorithm="HS256")


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
    assert json.loads(store.path.read_text(encoding="utf-8"))["test-oauth"]["account_id"] == "acct_test"


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
    assert seen_payload["model"] == "test-model"
    assert seen_payload["stream"] is True


def test_generic_oauth_adapter_sends_system_messages_as_instructions(tmp_path: Path) -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_payload
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": _token("acct_test"), "expires_in": 3600})
        seen_payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]},
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
                'event: response.created\n'
                'data: {"type":"response.created","response":{"id":"resp_1","status":"in_progress"}}\n\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":" from event-prefixed sse"}\n\n'
                'event: done\n'
                'data: [DONE]\n\n'
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


def test_generic_oauth_adapter_accumulates_streamed_function_arguments(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"fc_1","type":"function_call","call_id":"call_1","name":"echo","arguments":""}}\n\n'
                'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"{\\\"text\\\":"}\n\n'
                'data: {"type":"response.function_call_arguments.delta","output_index":0,"delta":"\\\"hello\\\"}"}\n\n'
                'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"fc_1","type":"function_call","call_id":"call_1","name":"echo","arguments":"{\\\"text\\\":\\\"hello\\\"}"}}\n\n'
                'data: [DONE]\n\n'
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


def test_generic_oauth_login_endpoint_returns_authorization_url(monkeypatch, tmp_path: Path) -> None:
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
