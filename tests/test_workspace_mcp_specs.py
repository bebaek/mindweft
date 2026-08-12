from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import coding_workspace_runner as legacy_runner
from minigent_workspace import mcp_specs


def test_runner_reexports_canonical_mcp_spec_helpers() -> None:
    names = [
        "CodingMCPServerSpec",
        "env_flag_enabled",
        "interpolate_config_string",
        "normalize_path_prefix",
        "mcp_server_specs_for_gateway",
        "mcp_gateway_config_from_specs",
        "write_mcp_gateway_config",
        "resolve_mcp_servers_file",
        "load_coding_mcp_server_specs",
        "load_coding_mcp_server_specs_from_json",
        "coding_mcp_server_spec_from_mapping",
        "expand_coding_mcp_command",
    ]

    for name in names:
        assert getattr(legacy_runner, name) is getattr(mcp_specs, name)


def test_interpolate_config_string_replaces_missing_values_with_empty_string() -> None:
    assert mcp_specs.interpolate_config_string("${SET}:${MISSING}", {"SET": "value"}) == "value:"


def test_resolve_mcp_servers_file_resolves_relative_path(tmp_path: Path) -> None:
    assert (
        mcp_specs.resolve_mcp_servers_file(
            None,
            {"MINIGENT_CODING_MCP_SERVERS_FILE": "config/servers.json"},
            base_dir=tmp_path,
        )
        == (tmp_path / "config" / "servers.json").resolve()
    )


def test_disabled_mcp_server_specs_are_filtered(tmp_path: Path) -> None:
    specs = mcp_specs.load_coding_mcp_server_specs_from_json(
        json.dumps(
            [
                {"name": "disabled", "command": ["disabled"], "enabled": False},
                {"name": "enabled", "command": ["enabled"]},
            ]
        ),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path],
    )

    assert [spec.name for spec in specs] == ["enabled"]


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ({"name": "bad", "transport": "socket"}, "unsupported transport"),
        ({"name": "bad", "command": "not-an-array"}, "command must be a string array"),
        ({"name": "bad", "command": ["x"], "profiles": "inspect"}, "profiles must be"),
        ({"name": "bad", "command": ["x"], "env": {"KEY": 1}}, "env must be"),
        ({"name": "bad", "command": ["x"], "headers": {"X": 1}}, "headers must be"),
        ({"name": "bad", "command": ["x"], "request_timeout": 0}, "request_timeout"),
        ({"name": "bad", "command": ["x"], "timeout_seconds": 0}, "timeout_seconds"),
        ({"name": "bad", "command": ["x"], "restart_on_timeout": "yes"}, "restart_on_timeout"),
    ],
)
def test_coding_mcp_server_spec_rejects_invalid_mapping(
    mapping: dict[str, object], message: str, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match=message):
        mcp_specs.coding_mcp_server_spec_from_mapping(
            mapping,
            default_host="127.0.0.1",
            default_port=8765,
            workspace_roots=[tmp_path],
            env={},
        )


def test_load_coding_mcp_server_specs_expands_workspace_placeholders(tmp_path: Path) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "custom-workspace",
                        "command": [
                            "custom-mcp",
                            "{workspace_roots}",
                            "--root-csv",
                            "{workspace_roots_csv}",
                        ],
                        "port": 9001,
                        "profiles": ["inspect", "test"],
                        "allowed_tools": ["inspect_repo"],
                        "path_policy": {"deny_globs": ["**/.env*"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    specs = mcp_specs.load_coding_mcp_server_specs_from_json(
        specs_path.read_text(encoding="utf-8"),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path, other_workspace],
    )

    assert len(specs) == 1
    assert specs[0].name == "custom-workspace"
    assert specs[0].url == "http://127.0.0.1:9001/mcp"
    assert specs[0].command == [
        "custom-mcp",
        str(tmp_path),
        str(other_workspace),
        "--root-csv",
        f"{tmp_path},{other_workspace}",
    ]
    assert specs[0].profiles == ["inspect", "test"]
    assert specs[0].allowed_tools == ["inspect_repo"]
    assert specs[0].path_policy == {"deny_globs": ["**/.env*"]}


def test_load_coding_mcp_server_specs_expands_workspace_flag_pair_for_each_root(
    tmp_path: Path,
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    specs = mcp_specs.load_coding_mcp_server_specs_from_json(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "text-workspace",
                        "command": ["text-server", "--workspace", "{workspace}"],
                    }
                ]
            }
        ),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path, other_workspace],
    )

    assert specs[0].command == [
        "text-server",
        "--workspace",
        str(tmp_path),
        "--workspace",
        str(other_workspace),
    ]


def test_load_coding_mcp_server_specs_expands_workspace_args_placeholder(
    tmp_path: Path,
) -> None:
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    specs = mcp_specs.load_coding_mcp_server_specs_from_json(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "text-workspace",
                        "command": ["text-server", "{workspace_args}"],
                    }
                ]
            }
        ),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path, other_workspace],
    )

    assert specs[0].command == [
        "text-server",
        "--workspace",
        str(tmp_path),
        "--workspace",
        str(other_workspace),
    ]


def test_load_coding_mcp_server_specs_defaults_stdio_port_when_omitted(tmp_path: Path) -> None:
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "web-fetch",
                        "command": ["uvx", "mcp-server-fetch"],
                        "profiles": ["inspect"],
                        "allowed_tools": ["fetch"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    specs = mcp_specs.load_coding_mcp_server_specs_from_json(
        specs_path.read_text(encoding="utf-8"),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path],
    )

    assert len(specs) == 1
    assert specs[0].name == "web-fetch"
    assert specs[0].port == mcp_specs.DEFAULT_BRIDGE_PORT
    assert specs[0].url == "http://127.0.0.1:8765/mcp"
    assert specs[0].command == ["uvx", "mcp-server-fetch"]


def test_load_coding_mcp_server_specs_loads_managed_http_server(tmp_path: Path) -> None:
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "web-search",
                        "transport": "http",
                        "managed": True,
                        "command": [
                            "npx",
                            "-y",
                            "@example/search-mcp",
                            "--api-key",
                            "${SEARCH_API_KEY}",
                        ],
                        "url": "http://127.0.0.1:8766/mcp",
                        "health_url": "http://127.0.0.1:8766/ping",
                        "startup_timeout_seconds": 2,
                        "request_timeout": 45,
                        "timeout_seconds": 50,
                        "env": {"SEARCH_API_KEY": "${SEARCH_API_KEY}"},
                        "headers": {"Authorization": "Bearer ${SEARCH_API_KEY}"},
                        "profiles": ["inspect"],
                        "allowed_tools": ["web_search"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    specs = mcp_specs.load_coding_mcp_server_specs_from_json(
        specs_path.read_text(encoding="utf-8"),
        bridge_host="127.0.0.1",
        workspace_roots=[tmp_path],
        env={"SEARCH_API_KEY": "secret-token"},
    )

    assert len(specs) == 1
    assert specs[0].name == "web-search"
    assert specs[0].transport == "http"
    assert specs[0].managed is True
    assert specs[0].command == [
        "npx",
        "-y",
        "@example/search-mcp",
        "--api-key",
        "secret-token",
    ]
    assert specs[0].url == "http://127.0.0.1:8766/mcp"
    assert specs[0].health_url == "http://127.0.0.1:8766/ping"
    assert specs[0].startup_timeout_seconds == 2
    assert specs[0].request_timeout == 45
    assert specs[0].timeout_seconds == 50
    assert specs[0].env == {"SEARCH_API_KEY": "secret-token"}
    assert specs[0].headers == {"Authorization": "Bearer secret-token"}


def test_load_coding_mcp_server_specs_requires_command_for_managed_http(tmp_path: Path) -> None:
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps({"servers": [{"name": "managed-http", "transport": "http", "managed": True}]}),
        encoding="utf-8",
    )

    try:
        mcp_specs.load_coding_mcp_server_specs_from_json(
            specs_path.read_text(encoding="utf-8"),
            bridge_host="127.0.0.1",
            workspace_roots=[tmp_path],
        )
    except RuntimeError as error:
        assert "requires command" in str(error)
    else:
        raise AssertionError("expected RuntimeError")


def test_mcp_server_specs_for_gateway_rewrites_stdio_urls_only() -> None:
    specs = [
        mcp_specs.CodingMCPServerSpec(
            name="stdio-workspace",
            url="http://127.0.0.1:9001/mcp",
            command=["stdio-server"],
        ),
        mcp_specs.CodingMCPServerSpec(
            name="http-workspace",
            url="http://127.0.0.1:9002/mcp",
            transport="http",
        ),
    ]

    transformed = mcp_specs.mcp_server_specs_for_gateway(specs, "http://127.0.0.1:8765/mcp")

    assert transformed[0].url == "http://127.0.0.1:8765/mcp/stdio-workspace"
    assert transformed[1].url == "http://127.0.0.1:9002/mcp"


def test_mcp_gateway_config_from_specs_includes_stdio_servers_only() -> None:
    specs = [
        mcp_specs.CodingMCPServerSpec(
            name="stdio-workspace",
            url="http://127.0.0.1:9001/mcp",
            command=["stdio-server"],
            allowed_tools=["read_file"],
            path_policy={"deny_globs": ["**/.env*"]},
            env={"EXAMPLE": "1"},
            request_timeout=45,
        ),
        mcp_specs.CodingMCPServerSpec(
            name="http-workspace",
            url="http://127.0.0.1:9002/mcp",
            transport="http",
        ),
    ]

    assert mcp_specs.mcp_gateway_config_from_specs(specs) == {
        "servers": [
            {
                "name": "stdio-workspace",
                "command": ["stdio-server"],
                "request_timeout": 45,
                "allowed_tools": ["read_file"],
                "path_policy": {"deny_globs": ["**/.env*"]},
                "env": {"EXAMPLE": "1"},
            }
        ]
    }
