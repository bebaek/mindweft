import asyncio
import json
import logging
from typing import Any

import httpx2 as httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import HTTPException

from app.mcp import (
    LEGACY_MCP_PROTOCOL_VERSION,
    LEGACY_PRIVATE_VALUES_META_KEY,
    MINIGENT_PRIVATE_VALUES_META_KEY,
    MODERN_MCP_PROTOCOL_VERSION,
    PRIVATE_VALUES_META_KEY,
    MCPHTTPClient,
    MCPPathPolicy,
    MCPPrivateToolResult,
    MCPServerConfig,
    MCPSettings,
    load_mcp_server_configs_from_env,
    mcp_jsonrpc_error,
    mcp_jsonrpc_result,
    mcp_settings_from_env,
    parse_mcp_tool_result,
    strip_modern_mcp_result_envelope,
)
from app.mcp_identity import MCPIdentityTokenIssuer
from app.redaction import RedactingLogFilter, install_log_redaction, redact_urls_in_text
from app.tools import ToolExecutionContext


def _legacy_mcp_server_config(**kwargs: Any) -> MCPServerConfig:
    return MCPServerConfig(protocol_version=LEGACY_MCP_PROTOCOL_VERSION, **kwargs)


def test_public_network_only_mcp_client_rejects_private_request_target() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    client = MCPHTTPClient(
        MCPServerConfig(
            name="personal",
            url="https://127.0.0.1/mcp",
            headers={},
            public_network_only=True,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client.list_tools())

    assert exc_info.value.status_code == 502
    assert "cannot access local or private network hosts" in str(exc_info.value.detail)
    assert requests == []


def test_mcp_jsonrpc_helpers_use_sdk_models_and_normalize_invalid_ids() -> None:
    assert mcp_jsonrpc_result("request-1", {"ok": True}) == {
        "jsonrpc": "2.0",
        "id": "request-1",
        "result": {"ok": True},
    }
    assert mcp_jsonrpc_error(False, -32600, "Invalid Request") == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert mcp_jsonrpc_result(1.5, {"ok": True}) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }


def test_strip_modern_mcp_result_envelope_preserves_payload_and_business_result() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [{"name": "echo"}],
            "resultType": "complete",
            "ttlMs": 0,
            "cacheScope": "private",
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "server"}},
        },
    }

    assert strip_modern_mcp_result_envelope(payload) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [{"name": "echo"}]},
    }
    assert payload["result"]["resultType"] == "complete"


def test_mcp_settings_from_env_mapping_defaults_to_empty() -> None:
    assert MCPSettings.from_env({}) == MCPSettings(servers=[])


def test_mcp_settings_prefer_mindweft_and_accept_legacy_env() -> None:
    preferred = MCPSettings.from_env(
        {
            "MINDWEFT_MCP_SERVERS": json.dumps(
                [{"name": "mindweft", "url": "https://mindweft.example/mcp"}]
            ),
            "MINIGENT_MCP_SERVERS": json.dumps(
                [{"name": "legacy", "url": "https://legacy.example/mcp"}]
            ),
        }
    )
    legacy = MCPSettings.from_env(
        {
            "MINIGENT_MCP_SERVERS": json.dumps(
                [{"name": "legacy", "url": "https://legacy.example/mcp"}]
            )
        }
    )

    assert preferred.servers[0].name == "mindweft"
    assert legacy.servers[0].name == "legacy"


def test_mcp_settings_from_env_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "MINDWEFT_MCP_SERVERS",
        json.dumps([{"name": "demo", "url": "https://example.com/mcp"}]),
    )

    assert mcp_settings_from_env().servers[0].name == "demo"


def test_load_mcp_server_configs_from_env(monkeypatch) -> None:
    monkeypatch.setenv(
        "MINDWEFT_MCP_SERVERS",
        json.dumps(
            [
                {
                    "name": "demo",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer token"},
                    "allowed_tools": ["list_directory", "read_file"],
                    "trusted_input_preprocessor_tools": ["read_file"],
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
    assert configs[0].trusted_input_preprocessor_tools == frozenset({"read_file"})
    assert configs[0].timeout_seconds == 45
    assert configs[0].path_policy.deny_globs == ["**/.env*", "**/.git/**"]
    assert configs[0].path_policy.allow_globs == ["**/.env*.template"]


def test_load_mcp_server_config_parses_forwarded_identity() -> None:
    configs = load_mcp_server_configs_from_env(
        {
            "MINDWEFT_MCP_SERVERS": json.dumps(
                [
                    {
                        "name": "private-calendar",
                        "url": "http://127.0.0.1:8769/mcp",
                        "forward_identity": True,
                        "identity_audience": "private-dav",
                        "identity_scopes": ["dav:calendar:read", "dav:calendar:write"],
                    }
                ]
            )
        }
    )

    assert configs[0].forward_identity is True
    assert configs[0].identity_audience == "private-dav"
    assert configs[0].identity_scopes == ("dav:calendar:read", "dav:calendar:write")


def test_mcp_http_client_initializes_lists_tools_and_calls_tool() -> None:
    requests: list[dict[str, Any]] = []
    http_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_methods.append(request.method)
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
            assert {key: value for key, value in body["params"].items() if key != "_meta"} == {
                "name": "echo",
                "arguments": {"text": "hello"},
            }
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
        config=_legacy_mcp_server_config(
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
    assert requests[5]["body"]["method"] == "tools/call"
    assert requests[6]["body"]["method"] == "tools/list"
    assert http_methods == ["POST"] * len(http_methods)


def test_mcp_http_client_discovers_and_uses_modern_stateless_protocol() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        requests.append({"headers": dict(request.headers), "body": body})
        method = body["method"]
        if method == "server/discover":
            result = {
                "supportedVersions": [MODERN_MCP_PROTOCOL_VERSION],
                "capabilities": {"tools": {}},
                "resultType": "complete",
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "modern-server",
                        "version": "2.0.0",
                    }
                },
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo text",
                        "inputSchema": {"type": "object"},
                    }
                ],
                "resultType": "complete",
            }
        elif method == "tools/call":
            result = {
                "structuredContent": {"echo": "hello"},
                "content": [{"type": "text", "text": "hello"}],
                "resultType": "complete",
            }
        else:
            raise AssertionError(f"Unexpected method {method}")
        result.update({"ttlMs": 0, "cacheScope": "private"})
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    client = MCPHTTPClient(
        config=MCPServerConfig(name="demo", url="https://example.com/mcp", headers={}),
        transport=httpx.MockTransport(handler),
    )

    specs = asyncio.run(client.list_tools())
    result = asyncio.run(client.call_tool("echo", {"text": "hello"}))

    assert [spec.name for spec in specs] == ["demo.echo"]
    assert result == {"echo": "hello"}
    assert [request["body"]["method"] for request in requests] == [
        "server/discover",
        "tools/list",
        "tools/call",
        "tools/list",
    ]
    for request in requests:
        assert "mcp-session-id" not in request["headers"]
        assert request["headers"]["mcp-protocol-version"] == MODERN_MCP_PROTOCOL_VERSION
        metadata = request["body"]["params"]["_meta"]
        assert metadata["io.modelcontextprotocol/protocolVersion"] == MODERN_MCP_PROTOCOL_VERSION
        assert metadata["io.modelcontextprotocol/clientInfo"]["name"] == "mindweft"
        assert metadata["io.modelcontextprotocol/clientCapabilities"] == {}
    assert requests[2]["headers"]["mcp-method"] == "tools/call"
    assert requests[2]["headers"]["mcp-name"] == "echo"
    assert client.server_info().session_id is None
    assert client.server_info().server_name == "modern-server"


def test_mcp_http_client_falls_back_when_modern_discovery_is_rejected() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        method = body["method"]
        methods.append(method)
        if method == "server/discover":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
        if method == "initialize":
            assert body["params"]["protocolVersion"] == LEGACY_MCP_PROTOCOL_VERSION
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "MCP-Session-Id": "legacy-session"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": LEGACY_MCP_PROTOCOL_VERSION,
                        "serverInfo": {"name": "legacy-server", "version": "1.0.0"},
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            assert request.headers["mcp-session-id"] == "legacy-session"
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=MCPServerConfig(name="demo", url="https://example.com/mcp", headers={}),
        transport=httpx.MockTransport(handler),
    )

    assert asyncio.run(client.list_tools()) == []
    assert methods == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert client.server_info().protocol_version == LEGACY_MCP_PROTOCOL_VERSION
    assert client.server_info().session_id == "legacy-session"


def test_mcp_http_client_forwards_short_lived_user_identity() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    claims_by_method: dict[str, list[dict[str, object]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        method = body["method"]
        token = request.headers["authorization"].removeprefix("Bearer ")
        claims_by_method.setdefault(method, []).append(
            jwt.decode(
                token,
                public_pem,
                algorithms=["RS256"],
                audience="private-dav",
                issuer="https://minigent.example",
            )
        )
        if method == "notifications/initialized":
            return httpx.Response(202)
        result: dict[str, object]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "private-dav-gateway", "version": "1"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "calendar_accounts_list",
                        "description": "List accounts",
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        elif method == "tools/call":
            result = {"structuredContent": {"accounts": []}, "content": []}
        else:
            raise AssertionError(method)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    config = _legacy_mcp_server_config(
        name="private-calendar",
        url="https://calendar.example/mcp",
        headers={},
        forward_identity=True,
        identity_audience="private-dav",
        identity_scopes=("dav:calendar:read",),
    )
    issuer = MCPIdentityTokenIssuer(
        issuer="https://minigent.example",
        audience="private-dav",
        private_key=private_pem,
        key_id="test-key",
    )
    client = MCPHTTPClient(
        config,
        transport=httpx.MockTransport(handler),
        identity_issuer=issuer,
    )

    asyncio.run(client.list_tools())
    with pytest.raises(HTTPException, match="requires user identity context"):
        asyncio.run(client.call_tool("calendar_accounts_list", {}))
    asyncio.run(
        client.call_tool(
            "calendar_accounts_list",
            {},
            context=ToolExecutionContext(tenant_id="tenant-a", user_id="user-a"),
        )
    )

    assert claims_by_method["initialize"][0]["tenant_id"] == "__mcp_discovery__"
    assert claims_by_method["initialize"][-1]["tenant_id"] == "tenant-a"
    assert claims_by_method["tools/list"][0]["sub"] == "__mcp_discovery__"
    assert claims_by_method["tools/call"][0]["tenant_id"] == "tenant-a"
    assert claims_by_method["tools/call"][0]["sub"] == "user-a"
    assert claims_by_method["tools/call"][0]["scope"] == "dav:calendar:read"
    assert "jti" in claims_by_method["tools/call"][0]


def test_parse_mcp_tool_result_separates_private_metadata() -> None:
    result = parse_mcp_tool_result(
        {
            "structuredContent": {
                "name": "{{pii:name:name-ref}}",
                "email": "{{pii:email:email-ref}}",
            },
            "_meta": {
                PRIVATE_VALUES_META_KEY: {
                    "name-ref": "Alice Smith",
                    "email-ref": "alice@example.com",
                }
            },
        },
        tool_name="contacts.list",
    )

    assert result == MCPPrivateToolResult(
        model_content={
            "name": "{{pii:name:name-ref}}",
            "email": "{{pii:email:email-ref}}",
        },
        private_values={
            "name-ref": "Alice Smith",
            "email-ref": "alice@example.com",
        },
    )


def test_parse_mcp_tool_result_rejects_invalid_private_metadata() -> None:
    with pytest.raises(HTTPException, match="invalid private-value metadata"):
        parse_mcp_tool_result(
            {
                "structuredContent": {"name": "{{pii:name:name-ref}}"},
                "_meta": {PRIVATE_VALUES_META_KEY: {"name-ref": 123}},
            },
            tool_name="contacts.list",
        )


def test_parse_mcp_tool_result_accepts_minigent_private_value_metadata() -> None:
    result = parse_mcp_tool_result(
        {
            "structuredContent": {"name": "protected-name"},
            "_meta": {MINIGENT_PRIVATE_VALUES_META_KEY: {"name-ref": "Example Name"}},
        },
        tool_name="contacts.list",
    )

    assert result == MCPPrivateToolResult(
        model_content={"name": "protected-name"},
        private_values={"name-ref": "Example Name"},
    )


def test_parse_mcp_tool_result_accepts_legacy_private_value_metadata() -> None:
    result = parse_mcp_tool_result(
        {
            "structuredContent": {"name": "{{pii:name:name-ref}}"},
            "_meta": {LEGACY_PRIVATE_VALUES_META_KEY: {"name-ref": "Alice Smith"}},
        },
        tool_name="contacts.list",
    )

    assert result == MCPPrivateToolResult(
        model_content={"name": "{{pii:name:name-ref}}"},
        private_values={"name-ref": "Alice Smith"},
    )


def test_mcp_http_client_rejects_mismatched_jsonrpc_response_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": body["id"] + 100, "result": {}},
        )

    client = MCPHTTPClient(
        config=_legacy_mcp_server_config(
            name="demo",
            url="https://example.com/mcp",
            headers={},
            timeout_seconds=0.05,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client.list_tools())

    assert exc_info.value.status_code == 502
    assert "request failed" in str(exc_info.value.detail)


def test_mcp_http_client_filters_disallowed_tools() -> None:
    requests: list[dict[str, Any]] = []

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
                            {
                                "name": "read_file",
                                "description": "Read",
                                "inputSchema": {"type": "object"},
                            },
                            {
                                "name": "write_file",
                                "description": "Write",
                                "inputSchema": {"type": "object"},
                            },
                        ]
                    },
                },
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=_legacy_mcp_server_config(
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
        config=_legacy_mcp_server_config(
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
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "serverInfo": {"name": "demo-server", "version": "1.0.0"},
                        "capabilities": {},
                    },
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
                                "name": "read_file",
                                "description": "Read a file",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=_legacy_mcp_server_config(
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

    assert requests == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/list",
    ]

    def listing_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read().decode())
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
                        "serverInfo": {"name": "demo-server", "version": "1.0.0"},
                        "capabilities": {},
                    },
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
                                "name": "list_directory",
                                "description": "List a directory",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"Unexpected method {method}")

    client = MCPHTTPClient(
        config=_legacy_mcp_server_config(
            name="fs",
            url="https://example.com/mcp",
            headers={},
            allowed_tools=["list_directory"],
            path_policy=MCPPathPolicy(
                deny_globs=["**/.env*", "**/.git/**"],
                allow_globs=["**/.env*.template"],
            ),
        ),
        transport=httpx.MockTransport(listing_handler),
    )

    result = asyncio.run(client.call_tool("list_directory", {"path": "/workspace"}))

    text = result["content"][0]["text"]
    assert "README.md" in text
    assert ".env.template" in text
    assert "[FILE] .env\n" not in text
    assert ".git" not in text
    assert "hidden 2 entries" in text


def test_mcp_http_client_supports_sse_jsonrpc_responses() -> None:
    requests: list[dict[str, Any]] = []

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
        config=_legacy_mcp_server_config(
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
        config=_legacy_mcp_server_config(
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
    requests: list[dict[str, Any]] = []
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
        config=_legacy_mcp_server_config(name="demo", url="https://example.com/mcp", headers={}),
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
    requests: list[dict[str, Any]] = []
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
        config=_legacy_mcp_server_config(name="demo", url="https://example.com/mcp", headers={}),
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
        config=_legacy_mcp_server_config(
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


def test_mcp_http_client_maps_notification_transport_failure_to_502() -> None:
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
        config=_legacy_mcp_server_config(
            name="demo",
            url="https://example.com/mcp",
            headers={},
            timeout_seconds=12,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client.list_tools())

    assert exc_info.value.status_code == 502
    assert "request failed" in exc_info.value.detail


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
