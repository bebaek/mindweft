import asyncio
import json
import logging
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.mcp import MCPServerConfig, MCPServerInfo
from app.mcp_manager import MCPServerManager
from app.models import ToolSpec
from app.peer_agents import PeerAgentRegistry, parse_peer_agent_configs
from app.tools import (
    MINIGENT_MINIRAG_BACKEND_ENV,
    MINIGENT_MINIRAG_DB_PATH_ENV,
    MINIGENT_MINIRAG_EMBEDDING_PROVIDER_ENV,
    MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT_ENV,
    MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT_ENV,
    ToolExecutionContext,
    build_local_tool_registry,
    build_tool_registry,
    build_tool_registry_from_env,
)


def test_local_registry_exposes_expected_tools() -> None:
    registry = build_local_tool_registry()

    specs = {spec.name: spec for spec in registry.specs()}

    assert "current_time" in specs
    assert "fetch_url" in specs
    assert "sleep" in specs
    assert "calculator" in specs
    assert "retrieve_knowledge" not in specs
    assert "peer_agent_task" not in specs
    assert specs["current_time"].description == "Return the current UTC time in ISO 8601 format."


def test_local_registry_can_enable_peer_agent_task_tool() -> None:
    registry = build_local_tool_registry(enable_peer_agent_tool=True)

    specs = {spec.name: spec for spec in registry.specs()}

    assert "peer_agent_task" in specs


def test_peer_agent_task_tool_description_includes_peer_hints() -> None:
    peer_registry = PeerAgentRegistry(
        parse_peer_agent_configs(
            [
                {
                    "name": "codex",
                    "base_url": "http://codex-agent.test",
                    "description": "Local coding-agent wrapper",
                    "capabilities": ["repository analysis", "codebase inspection"],
                    "side_effects": ["runs local commands in the allowed workspace"],
                    "version": "0.1.0",
                }
            ]
        )
    )
    registry = build_local_tool_registry(
        peer_agent_registry=peer_registry,
        enable_peer_agent_tool=True,
    )

    spec = {spec.name: spec for spec in registry.specs()}["peer_agent_task"]

    assert "Available peers:" in spec.description
    assert "codex" in spec.description
    assert "Local coding-agent wrapper" in spec.description
    assert "repository analysis, codebase inspection" in spec.description
    assert "runs local commands in the allowed workspace" in spec.description


def test_peer_agent_task_tool_schema_includes_peer_choices() -> None:
    peer_registry = PeerAgentRegistry(
        parse_peer_agent_configs(
            [
                {
                    "name": "codex",
                    "base_url": "http://codex-agent.test",
                    "description": "Local coding-agent wrapper",
                },
                {
                    "name": "docs",
                    "base_url": "http://docs-agent.test",
                    "description": "Documentation agent",
                },
            ]
        )
    )
    registry = build_local_tool_registry(
        peer_agent_registry=peer_registry,
        enable_peer_agent_tool=True,
    )

    spec = {spec.name: spec for spec in registry.specs()}["peer_agent_task"]

    assert spec.input_schema["properties"]["peer"]["enum"] == ["codex", "docs"]
    assert spec.input_schema["properties"]["peer"]["description"] == "Configured peer agent name."
    assert "Working directory" in spec.input_schema["properties"]["cwd"]["description"]
    assert "Task prompt" in spec.input_schema["properties"]["prompt"]["description"]
    assert "canceling" in spec.input_schema["properties"]["timeout_seconds"]["description"]


def test_local_registry_can_enable_peer_agent_task_tool_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_ENABLE_PEER_AGENT_TOOL", "true")

    registry = build_local_tool_registry()

    specs = {spec.name for spec in registry.specs()}
    assert "peer_agent_task" in specs


def test_peer_agent_task_tool_description_includes_env_peer_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_ENABLE_PEER_AGENT_TOOL", "true")
    monkeypatch.setenv(
        "MINIGENT_PEER_AGENTS",
        json.dumps(
            [
                {
                    "name": "codex",
                    "base_url": "http://codex-agent.test",
                    "description": "Local coding-agent wrapper",
                    "capabilities": ["repository analysis"],
                    "side_effects": ["runs local commands"],
                }
            ]
        ),
    )

    registry = build_local_tool_registry()
    spec = {spec.name: spec for spec in registry.specs()}["peer_agent_task"]

    assert "codex" in spec.description
    assert "repository analysis" in spec.description
    assert "runs local commands" in spec.description


def test_peer_agent_task_tool_schema_includes_env_peer_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIGENT_ENABLE_PEER_AGENT_TOOL", "true")
    monkeypatch.setenv(
        "MINIGENT_PEER_AGENTS",
        json.dumps(
            [
                {
                    "name": "codex",
                    "base_url": "http://codex-agent.test",
                    "description": "Local coding-agent wrapper",
                }
            ]
        ),
    )

    registry = build_local_tool_registry()
    spec = {spec.name: spec for spec in registry.specs()}["peer_agent_task"]

    assert spec.input_schema["properties"]["peer"]["enum"] == ["codex"]
    assert spec.input_schema["required"] == ["peer", "cwd", "prompt"]


def test_current_time_tool_returns_iso8601_timestamp() -> None:
    registry = build_local_tool_registry()

    result = asyncio.run(registry.execute("current_time", {}))

    assert set(result) == {"current_time"}
    datetime.fromisoformat(result["current_time"])


def test_sleep_tool_returns_requested_duration() -> None:
    registry = build_local_tool_registry()

    result = asyncio.run(registry.execute("sleep", {"seconds": 0}))

    assert result == {"slept_seconds": 0.0}


def test_calculator_tool_evaluates_arithmetic_expression() -> None:
    registry = build_local_tool_registry()

    result = asyncio.run(registry.execute("calculator", {"expression": "2 * (3 + 4) - 5 / 2"}))

    assert result == {"expression": "2 * (3 + 4) - 5 / 2", "result": 11.5}


def test_calculator_tool_rejects_unsupported_syntax() -> None:
    registry = build_local_tool_registry()

    with pytest.raises(HTTPException, match="unsupported syntax"):
        asyncio.run(registry.execute("calculator", {"expression": "sum([1, 2, 3])"}))


def test_fetch_url_tool_returns_response_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStream:
        def __init__(self, response: httpx.Response) -> None:
            self.response = response

        async def __aenter__(self) -> httpx.Response:
            return self.response

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakeAsyncClient:
        def __init__(
            self,
            *,
            timeout: float,
            follow_redirects: bool,
            trust_env: bool,
        ) -> None:
            assert timeout == 2.5
            assert follow_redirects is False
            assert trust_env is False

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def stream(self, method: str, url: str, *, headers: dict[str, str]) -> FakeStream:
            assert method == "GET"
            assert headers == {"accept": "text/plain"}
            request = httpx.Request("GET", url)
            return FakeStream(
                httpx.Response(
                    200,
                    request=request,
                    text="hello from url",
                    headers={"content-type": "text/plain; charset=utf-8"},
                )
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.tools._is_blocked_fetch_host", lambda host: False)
    registry = build_local_tool_registry()

    result = asyncio.run(
        registry.execute(
            "fetch_url",
            {
                "url": "https://example.com",
                "timeout_seconds": 2.5,
                "headers": {"accept": "text/plain"},
            },
        )
    )

    assert result["url"] == "https://example.com"
    assert result["final_url"] == "https://example.com"
    assert result["status_code"] == 200
    assert result["status"] == 200
    assert result["content_type"] == "text/plain; charset=utf-8"
    assert result["headers"]["content-type"] == "text/plain; charset=utf-8"
    assert result["text"] == "hello from url"
    assert result["body"] == "hello from url"
    assert result["truncated"] is False


def test_fetch_url_tool_truncates_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStream:
        async def __aenter__(self) -> httpx.Response:
            request = httpx.Request("GET", "https://example.com")
            return httpx.Response(200, request=request, content=b"abcdef")

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def stream(self, method: str, url: str, *, headers: dict[str, str]) -> FakeStream:
            return FakeStream()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.tools._is_blocked_fetch_host", lambda host: False)
    registry = build_local_tool_registry()

    result = asyncio.run(
        registry.execute("fetch_url", {"url": "https://example.com", "max_bytes": 3})
    )

    assert result["text"] == "abc"
    assert result["truncated"] is True


def test_fetch_url_tool_blocks_private_redirect_before_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    class FakeStream:
        def __init__(self, url: str) -> None:
            self.url = url

        async def __aenter__(self) -> httpx.Response:
            requested_urls.append(self.url)
            request = httpx.Request("GET", self.url)
            return httpx.Response(
                302,
                request=request,
                headers={"location": "http://127.0.0.1/admin"},
            )

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def stream(self, method: str, url: str, *, headers: dict[str, str]) -> FakeStream:
            return FakeStream(url)

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "app.tools._is_blocked_fetch_host",
        lambda host: host == "127.0.0.1",
    )
    registry = build_local_tool_registry()

    with pytest.raises(HTTPException, match="cannot access private network hosts"):
        asyncio.run(registry.execute("fetch_url", {"url": "https://example.com"}))

    assert requested_urls == ["https://example.com"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"url": "file:///etc/passwd"}, "only supports http and https"),
        ({"url": "https://127.0.0.1"}, "cannot access private network hosts"),
        ({"url": "https://example.com", "method": "POST"}, "method must be GET or HEAD"),
        (
            {"url": "https://example.com", "headers": {"authorization": "Bearer token"}},
            "is not allowed",
        ),
    ],
)
def test_fetch_url_tool_rejects_unsafe_arguments(
    arguments: dict[str, object],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.tools._is_blocked_fetch_host",
        lambda host: host == "127.0.0.1",
    )
    registry = build_local_tool_registry()

    with pytest.raises(HTTPException, match=message):
        asyncio.run(registry.execute("fetch_url", arguments))


def test_tool_execution_logs_start_and_success(caplog: pytest.LogCaptureFixture) -> None:
    registry = build_local_tool_registry()

    with caplog.at_level(logging.INFO, logger="app.tools"):
        result = asyncio.run(registry.execute("calculator", {"expression": "1 + 2"}))

    assert result == {"expression": "1 + 2", "result": 3}
    assert "tool.start name=calculator arguments={'expression': '1 + 2'}" in caplog.text
    assert "tool.ok name=calculator duration_ms=" in caplog.text


def test_tool_execution_logs_error_with_redacted_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = build_local_tool_registry()

    with caplog.at_level(logging.INFO, logger="app.tools"):
        with pytest.raises(HTTPException, match="requires a url"):
            asyncio.run(
                registry.execute(
                    "fetch_url",
                    {"url": "", "api_key": "secret-value", "authorization": "Bearer token"},
                )
            )

    assert (
        "tool.start name=fetch_url arguments={'url': '', 'api_key': '<redacted>', 'authorization': '<redacted>'}"
        in caplog.text
    )
    assert "tool.error name=fetch_url duration_ms=" in caplog.text
    assert "detail=fetch_url requires a url" in caplog.text


def test_tool_execution_logs_redacted_url_query_params(caplog: pytest.LogCaptureFixture) -> None:
    registry = build_local_tool_registry()

    with caplog.at_level(logging.INFO, logger="app.tools"):
        result = asyncio.run(
            registry.execute(
                "calculator",
                {
                    "expression": "1 + 2",
                    "url": "https://example.com/mcp?token=secret-value&cursor=abc&api_key=other-secret",
                },
            )
        )

    assert result == {"expression": "1 + 2", "result": 3}
    assert (
        "url': 'https://example.com/mcp?token=%3Credacted%3E&cursor=abc&api_key=%3Credacted%3E'"
        in caplog.text
    )


def test_build_tool_registry_can_limit_local_tools() -> None:
    registry = build_tool_registry(allowed_local_tools=["echo", "current_time"])

    specs = {spec.name for spec in registry.specs()}

    assert specs == {"echo", "current_time"}


def test_retrieve_knowledge_tool_uses_tenant_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(
        arguments: dict[str, object], context: ToolExecutionContext | None
    ) -> dict[str, object]:
        captured["arguments"] = arguments
        captured["context"] = context
        return {"chunks": [{"chunk_id": "chk_1"}]}

    monkeypatch.setattr("app.tools._execute_retrieve_knowledge", fake_execute)
    registry = build_local_tool_registry(allowed_tools=["retrieve_knowledge"])

    result = asyncio.run(
        registry.execute(
            "retrieve_knowledge",
            {"query": "token refresh", "top_k": 3},
            context=ToolExecutionContext(tenant_id="tenant-1", thread_id="thread-1"),
        )
    )

    assert result == {"chunks": [{"chunk_id": "chk_1"}]}
    assert captured["arguments"] == {"query": "token refresh", "top_k": 3}
    assert captured["context"] == ToolExecutionContext(tenant_id="tenant-1", thread_id="thread-1")


def test_peer_agent_task_tool_can_submit_without_polling() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://codex-agent.test/tasks"
        assert json.loads(request.content) == {
            "cwd": "/workspace/project",
            "prompt": "summarize",
        }
        return httpx.Response(200, json={"task_id": "task_123", "status": "running"})

    peer_registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    registry = build_local_tool_registry(
        peer_agent_registry=peer_registry,
        enable_peer_agent_tool=True,
    )

    result = asyncio.run(
        registry.execute(
            "peer_agent_task",
            {
                "peer": "codex",
                "cwd": "/workspace/project",
                "prompt": "summarize",
                "poll": False,
            },
        )
    )

    assert result["peer"] == "codex"
    assert result["task_id"] == "task_123"
    assert result["status"] == "running"
    assert result["exit_code"] is None
    assert result["timed_out"] is False
    assert result["canceled_on_timeout"] is False
    assert result["duration_seconds"] >= 0
    assert result["events_count"] == 0


def test_peer_agent_task_tool_can_poll_until_completion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    task_status_requests = 0
    caplog.set_level(logging.INFO)

    async def fake_sleep(seconds: float) -> None:
        assert seconds == 0.1

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_status_requests
        if request.method == "POST" and str(request.url) == "http://codex-agent.test/tasks":
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if request.method == "GET" and str(request.url) == "http://codex-agent.test/tasks/task_123":
            task_status_requests += 1
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "completed",
                    "exit_code": 0,
                    "final_output": "summary",
                    "stderr_tail": "log",
                    "events_tail": [{"type": "turn.completed"}],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    monkeypatch.setattr("app.tools.asyncio.sleep", fake_sleep)
    peer_registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    registry = build_local_tool_registry(
        peer_agent_registry=peer_registry,
        enable_peer_agent_tool=True,
    )

    result = asyncio.run(
        registry.execute(
            "peer_agent_task",
            {
                "peer": "codex",
                "cwd": "/workspace/project",
                "prompt": "summarize",
                "poll_interval_seconds": 0.1,
            },
        )
    )

    assert task_status_requests == 1
    assert result["peer"] == "codex"
    assert result["task_id"] == "task_123"
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["canceled_on_timeout"] is False
    assert result["duration_seconds"] >= 0
    assert result["events_count"] == 1
    assert result["final_output"] == "summary"
    assert result["final_output_preview"] == "summary"
    assert result["stderr_tail"] == "log"
    assert result["stderr_tail_preview"] == "log"
    assert "peer_agent_task.result peer=codex task_id=task_123 status=completed" in caplog.text


def test_peer_agent_task_tool_reports_timeout_with_observability_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = 0
    cancel_requests = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        assert seconds == 0.1
        sleeps += 1

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancel_requests
        if request.method == "POST" and str(request.url) == "http://codex-agent.test/tasks":
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "running",
                    "stderr_tail": "still working",
                },
            )
        if request.method == "GET" and str(request.url) == "http://codex-agent.test/tasks/task_123":
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "running",
                    "stderr_tail": "still working",
                    "events_tail": [{"type": "turn.started"}, {"type": "item.started"}],
                },
            )
        if (
            request.method == "POST"
            and str(request.url) == "http://codex-agent.test/tasks/task_123/cancel"
        ):
            cancel_requests += 1
            return httpx.Response(
                200,
                json={
                    "task_id": "task_123",
                    "status": "canceled",
                    "stderr_tail": "canceled",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    monkeypatch.setattr("app.tools.asyncio.sleep", fake_sleep)
    peer_registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    registry = build_local_tool_registry(
        peer_agent_registry=peer_registry,
        enable_peer_agent_tool=True,
    )

    result = asyncio.run(
        registry.execute(
            "peer_agent_task",
            {
                "peer": "codex",
                "cwd": "/workspace/project",
                "prompt": "summarize",
                "timeout_seconds": 0.000001,
                "poll_interval_seconds": 0.1,
            },
        )
    )

    assert sleeps >= 1
    assert cancel_requests == 1
    assert result["peer"] == "codex"
    assert result["task_id"] == "task_123"
    assert result["status"] == "canceled"
    assert result["timed_out"] is True
    assert result["canceled_on_timeout"] is True
    assert result["duration_seconds"] >= 0
    assert result["events_count"] == 0
    assert result["stderr_tail"] == "canceled"
    assert result["stderr_tail_preview"] == "canceled"


def test_peer_agent_task_tool_reports_timeout_cancel_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(seconds: float) -> None:
        assert seconds == 0.1

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url) == "http://codex-agent.test/tasks":
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if request.method == "GET" and str(request.url) == "http://codex-agent.test/tasks/task_123":
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if (
            request.method == "POST"
            and str(request.url) == "http://codex-agent.test/tasks/task_123/cancel"
        ):
            return httpx.Response(500, json={"detail": "nope"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    monkeypatch.setattr("app.tools.asyncio.sleep", fake_sleep)
    peer_registry = PeerAgentRegistry(
        parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
        transport=httpx.MockTransport(handler),
    )
    registry = build_local_tool_registry(
        peer_agent_registry=peer_registry,
        enable_peer_agent_tool=True,
    )

    result = asyncio.run(
        registry.execute(
            "peer_agent_task",
            {
                "peer": "codex",
                "cwd": "/workspace/project",
                "prompt": "summarize",
                "timeout_seconds": 0.000001,
                "poll_interval_seconds": 0.1,
            },
        )
    )

    assert result["status"] == "running"
    assert result["timed_out"] is True
    assert result["canceled_on_timeout"] is False
    assert "Peer agent 'codex' /tasks/task_123/cancel request failed" in result["cancel_error"]


def test_peer_agent_task_tool_cancels_peer_when_coroutine_is_canceled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeping = asyncio.Event()
    release_sleep = asyncio.Event()
    cancel_requests = 0

    async def fake_sleep(seconds: float) -> None:
        assert seconds == 10.0
        sleeping.set()
        await release_sleep.wait()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancel_requests
        if request.method == "POST" and str(request.url) == "http://codex-agent.test/tasks":
            return httpx.Response(200, json={"task_id": "task_123", "status": "running"})
        if (
            request.method == "POST"
            and str(request.url) == "http://codex-agent.test/tasks/task_123/cancel"
        ):
            cancel_requests += 1
            return httpx.Response(200, json={"task_id": "task_123", "status": "canceled"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def run_scenario() -> None:
        peer_registry = PeerAgentRegistry(
            parse_peer_agent_configs([{"name": "codex", "base_url": "http://codex-agent.test"}]),
            transport=httpx.MockTransport(handler),
        )
        registry = build_local_tool_registry(
            peer_agent_registry=peer_registry,
            enable_peer_agent_tool=True,
        )
        task = asyncio.create_task(
            registry.execute(
                "peer_agent_task",
                {
                    "peer": "codex",
                    "cwd": "/workspace/project",
                    "prompt": "summarize",
                    "poll_interval_seconds": 10.0,
                },
            )
        )
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr("app.tools.asyncio.sleep", fake_sleep)

    asyncio.run(run_scenario())

    assert cancel_requests == 1


def test_retrieve_knowledge_requires_minirag_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MINIGENT_MINIRAG_DB_PATH_ENV, raising=False)
    registry = build_local_tool_registry(allowed_tools=["retrieve_knowledge"])

    with pytest.raises(HTTPException, match=MINIGENT_MINIRAG_DB_PATH_ENV):
        asyncio.run(
            registry.execute(
                "retrieve_knowledge",
                {"query": "token refresh"},
                context=ToolExecutionContext(tenant_id="tenant-1"),
            )
        )


@pytest.mark.parametrize(
    ("backend_name", "embedding_provider_name", "lexical_weight", "dense_weight"),
    [
        ("dense", "hash", None, None),
        ("hybrid", "openrouter", "0.05", "0.95"),
    ],
)
def test_retrieve_knowledge_uses_backend_configuration(
    monkeypatch: pytest.MonkeyPatch,
    backend_name: str,
    embedding_provider_name: str,
    lexical_weight: str | None,
    dense_weight: str | None,
) -> None:
    class FakeMiniRAG:
        def __init__(self, *, db_path: str, backend: object) -> None:
            self.db_path = db_path
            self.backend = backend

    captured: dict[str, object] = {}

    def fake_build_backend(
        name: object,
        *,
        embedding_provider_name: str | None = None,
        hybrid_lexical_weight: float | None = None,
        hybrid_dense_weight: float | None = None,
    ) -> object:
        captured["backend_name"] = name
        captured["embedding_provider_name"] = embedding_provider_name
        captured["hybrid_lexical_weight"] = hybrid_lexical_weight
        captured["hybrid_dense_weight"] = hybrid_dense_weight
        return {
            "backend_name": name,
            "embedding_provider_name": embedding_provider_name,
            "hybrid_lexical_weight": hybrid_lexical_weight,
            "hybrid_dense_weight": hybrid_dense_weight,
        }

    def fake_retrieve_knowledge(
        rag: object,
        *,
        query: str,
        tenant_id: str,
        top_k: int,
    ) -> dict[str, object]:
        captured["rag"] = rag
        captured["query"] = query
        captured["tenant_id"] = tenant_id
        captured["top_k"] = top_k
        return {"chunks": []}

    fake_retrieve_module = SimpleNamespace(
        MiniRAG=FakeMiniRAG,
        build_backend=fake_build_backend,
    )
    fake_tool_module = SimpleNamespace(retrieve_knowledge=fake_retrieve_knowledge)

    def fake_import_module(name: str) -> object:
        if name == "minirag.retrieve":
            return fake_retrieve_module
        if name == "minirag.tool":
            return fake_tool_module
        raise ImportError(name)

    monkeypatch.setenv(MINIGENT_MINIRAG_DB_PATH_ENV, "/tmp/minirag.db")
    monkeypatch.setenv(MINIGENT_MINIRAG_BACKEND_ENV, backend_name)
    monkeypatch.setenv(MINIGENT_MINIRAG_EMBEDDING_PROVIDER_ENV, embedding_provider_name)
    if lexical_weight is not None:
        monkeypatch.setenv(MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT_ENV, lexical_weight)
    else:
        monkeypatch.delenv(MINIGENT_MINIRAG_HYBRID_LEXICAL_WEIGHT_ENV, raising=False)
    if dense_weight is not None:
        monkeypatch.setenv(MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT_ENV, dense_weight)
    else:
        monkeypatch.delenv(MINIGENT_MINIRAG_HYBRID_DENSE_WEIGHT_ENV, raising=False)
    monkeypatch.setattr("app.tools.importlib.import_module", fake_import_module)

    registry = build_local_tool_registry(allowed_tools=["retrieve_knowledge"])
    result = asyncio.run(
        registry.execute(
            "retrieve_knowledge",
            {"query": "token refresh", "top_k": 3},
            context=ToolExecutionContext(tenant_id="tenant-1"),
        )
    )

    assert result == {"chunks": []}
    assert captured["backend_name"] == backend_name
    assert captured["embedding_provider_name"] == embedding_provider_name
    assert captured["hybrid_lexical_weight"] == (
        float(lexical_weight) if lexical_weight is not None else None
    )
    assert captured["hybrid_dense_weight"] == (
        float(dense_weight) if dense_weight is not None else None
    )
    assert captured["query"] == "token refresh"
    assert captured["tenant_id"] == "tenant-1"
    assert captured["top_k"] == 3


def test_build_tool_registry_from_env_discovers_mcp_tools_inside_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMCPClient:
        def __init__(self, config: object) -> None:
            self._config = config

        async def list_tools(self) -> list[object]:
            return [
                type(
                    "Spec",
                    (),
                    {
                        "name": "demo.search",
                        "description": "Search docs",
                        "input_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                )()
            ]

        def server_info(self) -> object:
            return type(
                "ServerInfo",
                (),
                {
                    "name": "demo",
                    "url": "https://example.com/mcp",
                    "protocol_version": "2025-11-25",
                    "session_id": "session-123",
                    "server_name": "demo-server",
                    "server_version": "1.0.0",
                },
            )()

    monkeypatch.setenv(
        "MINIGENT_MCP_SERVERS",
        '[{"name":"demo","url":"https://example.com/mcp","headers":{}}]',
    )
    monkeypatch.setattr("app.tools.MCPHTTPClient", FakeMCPClient)

    async def build_registry_inside_running_loop() -> tuple[list[str], list[dict[str, object]]]:
        registry = build_tool_registry_from_env()
        return [spec.name for spec in registry.specs()], registry.mcp_servers()

    tool_names, servers = asyncio.run(build_registry_inside_running_loop())

    assert "demo.search" in tool_names
    assert servers == [
        {
            "name": "demo",
            "url": "https://example.com/mcp",
            "protocol_version": "2025-11-25",
            "session": True,
            "server_name": "demo-server",
            "server_version": "1.0.0",
            "tool_count": 1,
            "status": "connected",
            "last_error": None,
            "last_checked_at": None,
            "next_retry_at": None,
        }
    ]


def test_mcp_manager_retains_unavailable_server_and_recovers() -> None:
    config = MCPServerConfig(name="demo", url="https://example.com/mcp", headers={})
    attempts = 0

    class FakeMCPClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self._config = config

        async def list_tools(self) -> list[ToolSpec]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise HTTPException(status_code=502, detail="temporary outage")
            return [
                ToolSpec(
                    name="demo.search",
                    description="Search docs",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, tool_name: str, arguments: dict[str, object]) -> object:
            return {"tool_name": tool_name, "arguments": arguments}

        def server_info(self) -> MCPServerInfo:
            return MCPServerInfo(
                name="demo",
                url="https://example.com/mcp",
                protocol_version="2025-11-25",
                session_id="session-123",
                server_name="demo-server",
                server_version="1.0.0",
            )

    manager = MCPServerManager(client_factory=FakeMCPClient)

    first_snapshot = manager.snapshot([config])
    first_registry = build_tool_registry(mcp_snapshot=first_snapshot)

    assert [server["status"] for server in first_registry.mcp_servers()] == ["unavailable"]
    assert first_registry.mcp_servers()[0]["last_error"] == "temporary outage"
    assert "demo.search" not in {spec.name for spec in first_registry.specs()}

    asyncio.run(manager.refresh([config], force=True))
    second_snapshot = manager.snapshot([config])
    second_registry = build_tool_registry(mcp_snapshot=second_snapshot)

    assert [server["status"] for server in second_registry.mcp_servers()] == ["connected"]
    assert "demo.search" in {spec.name for spec in second_registry.specs()}
