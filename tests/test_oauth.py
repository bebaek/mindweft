import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi import HTTPException
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
    assert (
        json.loads(store.path.read_text(encoding="utf-8"))["test-oauth"]["account_id"]
        == "acct_test"
    )


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


def test_generic_oauth_adapter_surfaces_json_response_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_failed",
                "status": "failed",
                "error": {"code": "context_length_exceeded", "message": "too many tokens"},
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

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            adapter.generate(
                [Message(thread_id="thread", role=MessageRole.USER, content="hello")],
                [],
            )
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == (
        "Generic OAuth LLM response failed: context_length_exceeded: too many tokens"
    )


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
    assert exc_info.value.detail == "Generic OAuth LLM response failed: server_error: upstream failed"


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
