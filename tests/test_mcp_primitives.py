from __future__ import annotations

from app import mcp as legacy_mcp
from minigent_mcp.path_policy import (
    MCPPathPolicy,
    filter_directory_listing_text,
    iter_path_arguments,
    path_denied,
)
from minigent_mcp.protocol import (
    MODERN_MCP_PROTOCOL_VERSION,
    mcp_jsonrpc_error,
    mcp_jsonrpc_result,
    mcp_request_protocol_version,
    strip_modern_mcp_result_envelope,
)


def test_app_mcp_reexports_canonical_primitives() -> None:
    assert legacy_mcp.MCPPathPolicy is MCPPathPolicy
    assert legacy_mcp.MODERN_MCP_PROTOCOL_VERSION == MODERN_MCP_PROTOCOL_VERSION
    assert legacy_mcp.mcp_jsonrpc_error is mcp_jsonrpc_error
    assert legacy_mcp.mcp_jsonrpc_result is mcp_jsonrpc_result
    assert legacy_mcp.mcp_request_protocol_version is mcp_request_protocol_version
    assert legacy_mcp.strip_modern_mcp_result_envelope is strip_modern_mcp_result_envelope


def test_mcp_request_protocol_version_reads_valid_metadata() -> None:
    payload = {
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN_MCP_PROTOCOL_VERSION,
            }
        }
    }

    assert mcp_request_protocol_version(payload) == MODERN_MCP_PROTOCOL_VERSION


def test_mcp_request_protocol_version_rejects_missing_or_invalid_metadata() -> None:
    assert mcp_request_protocol_version({}) is None
    assert mcp_request_protocol_version({"params": []}) is None
    assert mcp_request_protocol_version({"params": {"_meta": []}}) is None
    assert (
        mcp_request_protocol_version(
            {"params": {"_meta": {"io.modelcontextprotocol/protocolVersion": 2}}}
        )
        is None
    )


def test_mcp_jsonrpc_helpers_normalize_request_ids() -> None:
    assert mcp_jsonrpc_result("request-1", {"ok": True}) == {
        "jsonrpc": "2.0",
        "id": "request-1",
        "result": {"ok": True},
    }
    assert mcp_jsonrpc_error(3, -32600, "Invalid Request") == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert mcp_jsonrpc_error(False, -32600, "Invalid Request")["id"] is None
    assert mcp_jsonrpc_result(1.5, {"ok": True})["id"] is None
    assert mcp_jsonrpc_result(None, {"ok": True})["id"] is None


def test_strip_modern_mcp_result_envelope_preserves_business_result() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "result": {"status": "ok"},
            "resultType": "complete",
            "ttlMs": 0,
            "cacheScope": "private",
            "_meta": {"server": "test"},
        },
    }

    assert strip_modern_mcp_result_envelope(payload) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"result": {"status": "ok"}},
    }


def test_iter_path_arguments_finds_nested_path_fields() -> None:
    arguments = {
        "path": "/workspace/one",
        "nested": {
            "paths": ["/workspace/two", 3],
            "source": "/workspace/source",
        },
        "items": [
            {"destination": "/workspace/destination"},
            {"target": "/workspace/target"},
            {"ignored": "/workspace/not-a-path"},
        ],
    }

    assert iter_path_arguments(arguments) == [
        "/workspace/one",
        "/workspace/two",
        "/workspace/source",
        "/workspace/destination",
        "/workspace/target",
    ]


def test_path_denied_normalizes_separators_and_honors_allow_override() -> None:
    policy = MCPPathPolicy(
        deny_globs=["**/.env*", "**/.git/**"],
        allow_globs=["**/.env*.template"],
    )

    assert path_denied(r"C:\workspace\.env", policy) is True
    assert path_denied("/workspace/.git/config", policy) is True
    assert path_denied("/workspace/.git", policy) is True
    assert path_denied("/workspace/.env.local.template", policy) is False
    assert path_denied("/workspace/src/main.py", policy) is False


def test_filter_directory_listing_text_hides_denied_entries() -> None:
    policy = MCPPathPolicy(deny_globs=["**/.env*", "**/.git/**"])
    listing = "[FILE] README.md\n[FILE] .env\n[DIR] .git\n[DIR] src"

    assert filter_directory_listing_text(listing, policy) == (
        "[FILE] README.md\n[DIR] src\n[hidden 2 entries by path policy]"
    )
