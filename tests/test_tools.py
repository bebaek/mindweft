import asyncio
import logging
from datetime import datetime

import httpx
import pytest
from fastapi import HTTPException

from app.tools import build_local_tool_registry
from app.tools import build_tool_registry_from_env


def test_local_registry_exposes_expected_tools() -> None:
    registry = build_local_tool_registry()

    specs = {spec.name: spec for spec in registry.specs()}

    assert "current_time" in specs
    assert "fetch_url" in specs
    assert "sleep" in specs
    assert "calculator" in specs
    assert specs["current_time"].description == "Return the current UTC time in ISO 8601 format."


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
    class FakeAsyncClient:
        def __init__(self, *, timeout: float, follow_redirects: bool) -> None:
            assert timeout == 2.5
            assert follow_redirects is True

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                request=request,
                text="hello from url",
                headers={"content-type": "text/plain; charset=utf-8"},
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    registry = build_local_tool_registry()

    result = asyncio.run(registry.execute("fetch_url", {"url": "https://example.com", "timeout_seconds": 2.5}))

    assert result == {
        "url": "https://example.com",
        "status_code": 200,
        "content_type": "text/plain; charset=utf-8",
        "text": "hello from url",
    }


def test_tool_execution_logs_start_and_success(caplog: pytest.LogCaptureFixture) -> None:
    registry = build_local_tool_registry()

    with caplog.at_level(logging.INFO, logger="app.tools"):
        result = asyncio.run(registry.execute("calculator", {"expression": "1 + 2"}))

    assert result == {"expression": "1 + 2", "result": 3}
    assert "tool.start name=calculator arguments={'expression': '1 + 2'}" in caplog.text
    assert "tool.ok name=calculator duration_ms=" in caplog.text


def test_tool_execution_logs_error_with_redacted_arguments(caplog: pytest.LogCaptureFixture) -> None:
    registry = build_local_tool_registry()

    with caplog.at_level(logging.INFO, logger="app.tools"):
        with pytest.raises(HTTPException, match="requires a url"):
            asyncio.run(
                registry.execute(
                    "fetch_url",
                    {"url": "", "api_key": "secret-value", "authorization": "Bearer token"},
                )
            )

    assert "tool.start name=fetch_url arguments={'url': '', 'api_key': '<redacted>', 'authorization': '<redacted>'}" in caplog.text
    assert "tool.error name=fetch_url duration_ms=" in caplog.text
    assert "detail=fetch_url requires a url" in caplog.text


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
                        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
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
        }
    ]
