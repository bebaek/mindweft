import asyncio
import json
import logging

import httpx
import pytest

from app.mcp import MCPHTTPClient, MCPServerConfig, load_mcp_server_configs_from_env
from app.redaction import RedactingLogFilter, install_log_redaction


def test_load_mcp_server_configs_from_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_MCP_SERVERS",
        json.dumps(
            [
                {
                    "name": "demo",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer token"},
                }
            ]
        ),
    )

    configs = load_mcp_server_configs_from_env()

    assert len(configs) == 1
    assert configs[0].name == "demo"
    assert configs[0].url == "https://example.com/mcp"
    assert configs[0].headers == {"Authorization": "Bearer token"}


def test_mcp_http_client_initializes_lists_tools_and_calls_tool() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "headers": dict(request.headers),
                "body": json.loads(request.read().decode()),
            }
        )
        body = requests[-1]["body"]
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "MCP-Session-Id": "session-123"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "demo-server", "version": "1.2.3"},
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo text",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                },
                            }
                        ]
                    },
                },
            )
        if method == "tools/call":
            assert body["params"] == {"name": "echo", "arguments": {"text": "hello"}}
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "structuredContent": {"echo": "hello"},
                        "content": [{"type": "text", "text": "hello"}],
                    },
                },
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="demo",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer token"},
        ),
        transport=httpx.MockTransport(handler),
    )

    specs = asyncio.run(client.list_tools())
    result = asyncio.run(client.call_tool("echo", {"text": "hello"}))

    assert [spec.name for spec in specs] == ["demo.echo"]
    assert result == {"echo": "hello"}
    assert requests[1]["body"]["method"] == "notifications/initialized"
    assert requests[2]["body"]["method"] == "tools/list"
    assert requests[2]["headers"]["mcp-session-id"] == "session-123"
    assert requests[2]["headers"]["mcp-protocol-version"] == "2025-11-25"
    assert requests[3]["body"]["method"] == "tools/call"


def test_mcp_http_client_supports_sse_jsonrpc_responses() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        requests.append({"headers": dict(request.headers), "body": body})
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "MCP-Session-Id": "session-sse"},
                text=(
                    'event: message\n'
                    'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25","serverInfo":{"name":"demo-server","version":"1.0.0"},"capabilities":{"tools":{}}}}\n\n'
                ),
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    ': keepalive\n\n'
                    'event: message\n'
                    'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"search","description":"Search docs","inputSchema":{"type":"object","properties":{"query":{"type":"string"}}}}]}}\n\n'
                ),
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="demo",
            url="https://example.com/mcp",
            headers={},
        ),
        transport=httpx.MockTransport(handler),
    )

    specs = asyncio.run(client.list_tools())

    assert [spec.name for spec in specs] == ["demo.search"]
    assert requests[1]["body"]["method"] == "notifications/initialized"
    assert requests[2]["headers"]["mcp-session-id"] == "session-sse"


def test_mcp_http_client_logs_redacted_url(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "MCP-Session-Id": "session-123"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "demo-server", "version": "1.2.3"},
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="demo",
            url="https://example.com/mcp?token=secret-value&cursor=abc",
            headers={},
        ),
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level(logging.INFO, logger="app.mcp"):
        asyncio.run(client.list_tools())

    assert "url=https://example.com/mcp?token=%3Credacted%3E&cursor=abc" in caplog.text
    assert "secret-value" not in caplog.text


def test_redacting_log_filter_redacts_httpx_style_log_messages(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("httpx.test")
    logger.addFilter(RedactingLogFilter())

    with caplog.at_level(logging.INFO, logger="httpx.test"):
        logger.info("HTTP Request: %s %s", "POST", "https://mcp.tavily.com/mcp?tavilyApiKey=tvly-dev-secret&x=1")

    assert "tavilyApiKey=%3Credacted%3E" in caplog.text
    assert "tvly-dev-secret" not in caplog.text


def test_install_log_redaction_redacts_new_log_records() -> None:
    original_factory = logging.getLogRecordFactory()
    try:
        install_log_redaction()
        record = logging.getLogRecordFactory()(
            "httpx",
            logging.INFO,
            __file__,
            1,
            "HTTP Request: %s %s",
            ("POST", "https://mcp.tavily.com/mcp?tavilyApiKey=tvly-dev-secret&x=1"),
            None,
        )

        assert record.getMessage() == "HTTP Request: POST https://mcp.tavily.com/mcp?tavilyApiKey=%3Credacted%3E&x=1"
    finally:
        logging.setLogRecordFactory(original_factory)


def test_install_log_redaction_redacts_httpx_url_objects() -> None:
    original_factory = logging.getLogRecordFactory()
    try:
        install_log_redaction()
        record = logging.getLogRecordFactory()(
            "httpx",
            logging.INFO,
            __file__,
            1,
            'HTTP Request: %s %s "%s %d %s"',
            ("POST", httpx.URL("https://mcp.tavily.com/mcp?tavilyApiKey=tvly-dev-secret&x=1"), "HTTP/1.1", 200, "OK"),
            None,
        )

        assert (
            record.getMessage()
            == 'HTTP Request: POST https://mcp.tavily.com/mcp?tavilyApiKey=%3Credacted%3E&x=1 "HTTP/1.1 200 OK"'
        )
    finally:
        logging.setLogRecordFactory(original_factory)
