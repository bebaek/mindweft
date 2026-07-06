import asyncio
import json
import logging

import httpx
import pytest
from fastapi import HTTPException

from app.mcp import (
    MCPHTTPClient,
    MCPPathPolicy,
    MCPServerConfig,
    MCPSettings,
    load_mcp_server_configs_from_env,
    mcp_settings_from_env,
)
from app.redaction import RedactingLogFilter, install_log_redaction, redact_urls_in_text


def test_mcp_settings_from_env_mapping_defaults_to_empty() -> None:
    assert MCPSettings.from_env({}) == MCPSettings(servers=[])


def test_mcp_settings_from_env_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_MCP_SERVERS",
        json.dumps([{"name": "demo", "url": "https://example.com/mcp"}]),
    )

    assert mcp_settings_from_env().servers[0].name == "demo"


def test_load_mcp_server_configs_from_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "MINIGENT_MCP_SERVERS",
        json.dumps(
            [
                {
                    "name": "demo",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer token"},
                    "allowed_tools": ["list_directory", "read_file"],
                    "timeout_seconds": 45,
                    "path_policy": {
                        "deny_globs": ["**/.env*", "**/.git/**"],
                        "allow_globs": ["**/.env*.template"],
                    },
                }
            ]
        ),
    )

    configs = load_mcp_server_configs_from_env()

    assert len(configs) == 1
    assert configs[0].name == "demo"
    assert configs[0].url == "https://example.com/mcp"
    assert configs[0].headers == {"Authorization": "Bearer token"}
    assert configs[0].allowed_tools == ["list_directory", "read_file"]
    assert configs[0].timeout_seconds == 45
    assert configs[0].path_policy.deny_globs == ["**/.env*", "**/.git/**"]
    assert configs[0].path_policy.allow_globs == ["**/.env*.template"]


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


def test_mcp_http_client_rejects_mismatched_jsonrpc_response_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": body["id"] + 100, "result": {}},
        )

    client = MCPHTTPClient(
        config=MCPServerConfig(name="demo", url="https://example.com/mcp", headers={}),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client.list_tools())

    assert exc_info.value.status_code == 502
    assert "mismatched JSON-RPC response id" in str(exc_info.value.detail)


def test_mcp_http_client_filters_disallowed_tools() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        requests.append({"headers": dict(request.headers), "body": body})
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
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
                            {"name": "read_file", "description": "Read", "inputSchema": {}},
                            {"name": "write_file", "description": "Write", "inputSchema": {}},
                        ]
                    },
                },
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="fs",
            url="https://example.com/mcp",
            headers={},
            allowed_tools=["read_file"],
        ),
        transport=httpx.MockTransport(handler),
    )

    specs = asyncio.run(client.list_tools())

    assert [spec.name for spec in specs] == ["fs.read_file"]
    with pytest.raises(Exception, match="not allowed"):
        asyncio.run(client.call_tool("write_file", {"path": "/tmp/x"}))
    assert [request["body"]["method"] for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


def test_mcp_http_client_blocks_denied_path_arguments() -> None:
    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="fs",
            url="https://example.com/mcp",
            headers={},
            allowed_tools=["read_file"],
            path_policy=MCPPathPolicy(deny_globs=["**/.env*"]),
        ),
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    with pytest.raises(Exception, match="denied"):
        asyncio.run(client.call_tool("read_file", {"path": "/workspace/.env"}))


def test_mcp_http_client_allows_explicitly_allowed_path_over_denied_glob() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        method = body["method"]
        requests.append(method)
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-11-25", "serverInfo": {}},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": []}},
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="fs",
            url="https://example.com/mcp",
            headers={},
            allowed_tools=["read_file"],
            path_policy=MCPPathPolicy(
                deny_globs=["**/.env*"],
                allow_globs=["**/.env*.template"],
            ),
        ),
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(client.call_tool("read_file", {"path": "/workspace/.env.coding.template"}))

    assert requests == ["initialize", "notifications/initialized", "tools/call"]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": "2025-11-25", "serverInfo": {}},
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "[FILE] README.md\n[FILE] .env\n[FILE] .env.template\n[DIR] .git\n[DIR] app",
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="fs",
            url="https://example.com/mcp",
            headers={},
            allowed_tools=["list_directory"],
            path_policy=MCPPathPolicy(
                deny_globs=["**/.env*", "**/.git/**"],
                allow_globs=["**/.env*.template"],
            ),
        ),
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(client.call_tool("list_directory", {"path": "/workspace"}))

    text = result["content"][0]["text"]
    assert "README.md" in text
    assert ".env.template" in text
    assert "[FILE] .env\n" not in text
    assert ".git" not in text
    assert "hidden 2 entries" in text


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
                    "event: message\n"
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
                    ": keepalive\n\n"
                    "event: message\n"
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


def test_mcp_http_client_reinitializes_and_retries_once_on_invalid_session() -> None:
    requests: list[dict[str, object]] = []
    session_ids = iter(["session-1", "session-2"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        requests.append({"headers": dict(request.headers), "body": body})
        method = body["method"]
        session_id = request.headers.get("mcp-session-id")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "MCP-Session-Id": next(session_ids),
                },
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
            if session_id == "session-1":
                return httpx.Response(400, text="Bad Request: No valid session ID provided")
            if session_id == "session-2":
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
                )
        raise AssertionError(f"Unexpected method {method} with session {session_id}")

    client = MCPHTTPClient(
        config=MCPServerConfig(name="demo", url="https://example.com/mcp", headers={}),
        transport=httpx.MockTransport(handler),
    )

    specs = asyncio.run(client.list_tools())

    assert specs == []
    assert [request["body"]["method"] for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert requests[2]["headers"]["mcp-session-id"] == "session-1"
    assert requests[5]["headers"]["mcp-session-id"] == "session-2"


def test_mcp_http_client_returns_error_if_reinitialized_session_is_still_rejected() -> None:
    requests: list[dict[str, object]] = []
    session_ids = iter(["session-1", "session-2"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        requests.append({"headers": dict(request.headers), "body": body})
        method = body["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "MCP-Session-Id": next(session_ids),
                },
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
            return httpx.Response(400, text="Bad Request: No valid session ID provided")
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=MCPServerConfig(name="demo", url="https://example.com/mcp", headers={}),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception, match="No valid session ID provided"):
        asyncio.run(client.list_tools())

    assert [request["body"]["method"] for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


def test_mcp_http_client_maps_request_timeout_to_504() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream", request=request)

    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="demo",
            url="https://example.com/mcp",
            headers={},
            timeout_seconds=12,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client.list_tools())

    assert exc_info.value.status_code == 504
    assert exc_info.value.detail == "MCP server 'demo' request timed out after 12s"


def test_mcp_http_client_maps_notification_timeout_to_504() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
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
        raise httpx.ReadTimeout("slow notification", request=request)

    client = MCPHTTPClient(
        config=MCPServerConfig(
            name="demo",
            url="https://example.com/mcp",
            headers={},
            timeout_seconds=12,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client.list_tools())

    assert exc_info.value.status_code == 504
    assert exc_info.value.detail == "MCP notification timed out for server 'demo' after 12s"


def test_redacting_log_filter_redacts_httpx_style_log_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("httpx.test")
    logger.addFilter(RedactingLogFilter())

    with caplog.at_level(logging.INFO, logger="httpx.test"):
        logger.info(
            "HTTP Request: %s %s",
            "POST",
            "https://mcp.tavily.com/mcp?tavilyApiKey=tvly-dev-secret&x=1",
        )

    assert "tavilyApiKey=%3Credacted%3E" in caplog.text
    assert "tvly-dev-secret" not in caplog.text


def test_redact_urls_in_text_ignores_malformed_bracketed_hosts() -> None:
    malformed = "see " + "http" + "://[not-ip]/path?token=secret"

    assert redact_urls_in_text(malformed) == malformed


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

        assert (
            record.getMessage()
            == "HTTP Request: POST https://mcp.tavily.com/mcp?tavilyApiKey=%3Credacted%3E&x=1"
        )
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
            (
                "POST",
                httpx.URL("https://mcp.tavily.com/mcp?tavilyApiKey=tvly-dev-secret&x=1"),
                "HTTP/1.1",
                200,
                "OK",
            ),
            None,
        )

        assert (
            record.getMessage()
            == 'HTTP Request: POST https://mcp.tavily.com/mcp?tavilyApiKey=%3Credacted%3E&x=1 "HTTP/1.1 200 OK"'
        )
    finally:
        logging.setLogRecordFactory(original_factory)
