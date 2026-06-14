from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Iterator
from io import StringIO
from typing import Any

import pytest

from minigent_client.api_client import MinigentAPIClient
from minigent_client.config import ClientConfig, PrincipalConfig

RUN_E2E_ENV = "MINIGENT_RUN_E2E_TESTS"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv(RUN_E2E_ENV, "").strip().lower() not in {"1", "true", "yes", "on"},
        reason=f"Set {RUN_E2E_ENV}=true to run Minigent e2e tests",
    ),
]

AUTH_HEADERS = {
    "X-Minigent-User-Id": "e2e-user",
    "X-Minigent-Tenant-Id": "e2e-tenant",
}


def test_live_server_thread_lifecycle(live_minigent_server: str) -> None:
    health = _request_json("GET", f"{live_minigent_server}/health")
    assert health == {"status": "ok"}

    config = _request_json("GET", f"{live_minigent_server}/config")
    assert config["llm"]["provider"] == "mock"

    thread = _request_json("POST", f"{live_minigent_server}/threads", headers=AUTH_HEADERS)
    thread_id = thread["thread_id"]
    assert isinstance(thread_id, str)
    assert thread_id

    message = _request_json(
        "POST",
        f"{live_minigent_server}/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
        payload={"content": "hello from raw http e2e"},
    )
    assert message["role"] == "user"
    assert message["content"] == "hello from raw http e2e"

    run = _request_json(
        "POST",
        f"{live_minigent_server}/threads/{thread_id}/run",
        headers=AUTH_HEADERS,
    )
    assert run == {"reply": "Mock reply: hello from raw http e2e"}

    messages = _request_json(
        "GET",
        f"{live_minigent_server}/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
    )
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "Mock reply: hello from raw http e2e"


def test_python_client_against_live_server(live_minigent_server: str) -> None:
    client = MinigentAPIClient(_client_config(live_minigent_server, stream_runs=False))

    response = client.send_user_message("hello from python client e2e")
    assert response["role"] == "user"

    reply, metadata = client.run_thread()

    assert reply == "Mock reply: hello from python client e2e"
    assert metadata is None
    assert isinstance(client.thread_id, str)


def test_python_client_streaming_against_live_server(live_minigent_server: str) -> None:
    progress_stream = StringIO()
    client = MinigentAPIClient(
        _client_config(live_minigent_server, stream_runs=True),
        progress_stream=progress_stream,
    )

    client.send_user_message("hello from streaming client e2e")
    reply, metadata = client.run_thread()

    assert reply == "Mock reply: hello from streaming client e2e"
    assert metadata is None
    client.flush_pending_token_summary()
    progress = progress_stream.getvalue()
    assert "● preparing" in progress
    assert "● sending" in progress
    assert "● done" in progress


def test_live_server_run_stream_ndjson(live_minigent_server: str) -> None:
    thread = _request_json("POST", f"{live_minigent_server}/threads", headers=AUTH_HEADERS)
    thread_id = thread["thread_id"]
    _request_json(
        "POST",
        f"{live_minigent_server}/threads/{thread_id}/messages",
        headers=AUTH_HEADERS,
        payload={"content": "hello from ndjson e2e"},
    )

    events = list(
        _request_ndjson(
            "POST",
            f"{live_minigent_server}/threads/{thread_id}/run/stream",
            headers=AUTH_HEADERS,
        )
    )

    event_types = [event["type"] for event in events]
    assert event_types == [
        "run.started",
        "llm.request",
        "assistant.message",
        "run.completed",
    ]
    assert (
        events[event_types.index("assistant.message")]["content"]
        == "Mock reply: hello from ndjson e2e"
    )


def _client_config(base_url: str, *, stream_runs: bool) -> ClientConfig:
    return ClientConfig(
        base_url=base_url,
        wake_phrase="hey minigent",
        stream_runs=stream_runs,
        principal=PrincipalConfig(user_id="e2e-user", tenant_id="e2e-tenant"),
    )


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, method=method, headers=request_headers, data=data)
    with urllib.request.urlopen(request, timeout=5) as response:
        raw_body = response.read().decode("utf-8")
    if not raw_body:
        return None
    return json.loads(raw_body)


def _request_ndjson(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    request_headers = {"Accept": "application/x-ndjson", **(headers or {})}
    request = urllib.request.Request(url, method=method, headers=request_headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if line:
                event = json.loads(line)
                assert isinstance(event, dict)
                yield event
