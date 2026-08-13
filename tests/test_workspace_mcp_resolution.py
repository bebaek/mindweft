from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from minigent_workspace import cli, mcp_resolution, runtime_settings
from minigent_workspace.mcp_specs import CodingMCPServerSpec


def default_inputs(tmp_path: Path):
    args = cli.parse_args(["--env-file", str(tmp_path / ".env.coding")])
    settings = runtime_settings.resolve_workspace_runtime_settings(args, {})
    return args, settings


def test_resolve_workspace_mcp_servers_builds_builtin_specs(tmp_path: Path, monkeypatch) -> None:
    args, settings = default_inputs(tmp_path)
    spec = CodingMCPServerSpec(name="fs-workspace", url=settings.bridge_url)
    build = Mock(return_value=[spec])
    monkeypatch.setattr(mcp_resolution, "build_builtin_mcp_server_specs", build)

    resolved = mcp_resolution.resolve_workspace_mcp_servers(
        args,
        {},
        tenant_id="demo-tenant",
        workspace_roots=[tmp_path],
        settings=settings,
    )

    assert resolved == mcp_resolution.ResolvedMCPServers(None, [spec], [spec])
    build.assert_called_once_with(
        {},
        "demo-tenant",
        bridge_name=settings.bridge_name,
        bridge_host=settings.bridge_host,
        bridge_port=settings.bridge_port,
        bridge_url=settings.bridge_url,
        workspace_roots=[tmp_path],
        text_enabled=settings.text_enabled,
        text_bridge_name=settings.text_bridge_name,
        text_bridge_port=settings.text_bridge_port,
        text_bridge_url=settings.text_bridge_url,
        shell_enabled=settings.shell_enabled,
        shell_bridge_name=settings.shell_bridge_name,
        shell_bridge_port=settings.shell_bridge_port,
        shell_bridge_url=settings.shell_bridge_url,
    )


def test_resolve_workspace_mcp_servers_prefers_inline_specs(tmp_path: Path, monkeypatch) -> None:
    args, settings = default_inputs(tmp_path)
    servers_file = tmp_path / "servers.json"
    args.mcp_servers_file = str(servers_file)
    env = {"MINIGENT_CODING_MCP_SERVER_SPECS": "[]"}
    inline_spec = CodingMCPServerSpec(name="inline", url="http://127.0.0.1:9000/mcp")
    inline = Mock(return_value=[inline_spec])
    file_loader = Mock()
    monkeypatch.setattr(mcp_resolution, "load_coding_mcp_server_specs_from_json", inline)
    monkeypatch.setattr(mcp_resolution, "load_coding_mcp_server_specs", file_loader)

    resolved = mcp_resolution.resolve_workspace_mcp_servers(
        args,
        env,
        tenant_id="demo-tenant",
        workspace_roots=[tmp_path],
        settings=settings,
    )

    assert resolved.source_file == servers_file.resolve()
    assert resolved.process_specs == [inline_spec]
    inline.assert_called_once_with(
        "[]",
        bridge_host=settings.bridge_host,
        workspace_roots=[tmp_path],
        env=env,
    )
    file_loader.assert_not_called()


def test_resolve_workspace_mcp_servers_loads_legacy_file(tmp_path: Path, monkeypatch) -> None:
    args, settings = default_inputs(tmp_path)
    servers_file = tmp_path / "servers.json"
    args.mcp_servers_file = str(servers_file)
    spec = CodingMCPServerSpec(name="file", url="http://127.0.0.1:9000/mcp")
    loader = Mock(return_value=[spec])
    monkeypatch.setattr(mcp_resolution, "load_coding_mcp_server_specs", loader)

    resolved = mcp_resolution.resolve_workspace_mcp_servers(
        args,
        {},
        tenant_id="demo-tenant",
        workspace_roots=[tmp_path],
        settings=settings,
    )

    assert resolved == mcp_resolution.ResolvedMCPServers(servers_file.resolve(), [spec], [spec])
    loader.assert_called_once_with(
        servers_file.resolve(),
        bridge_host=settings.bridge_host,
        workspace_roots=[tmp_path],
        env={},
    )


def test_resolve_workspace_mcp_servers_rewrites_tenant_specs_for_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    args = cli.parse_args(["--mcp-gateway"])
    settings = runtime_settings.resolve_workspace_runtime_settings(args, {})
    process_spec = CodingMCPServerSpec(name="files", url="http://127.0.0.1:8765/mcp")
    tenant_spec = CodingMCPServerSpec(name="files", url="http://127.0.0.1:8765/mcp/files")
    monkeypatch.setattr(
        mcp_resolution, "build_builtin_mcp_server_specs", Mock(return_value=[process_spec])
    )
    rewrite = Mock(return_value=[tenant_spec])
    monkeypatch.setattr(mcp_resolution, "mcp_server_specs_for_gateway", rewrite)

    resolved = mcp_resolution.resolve_workspace_mcp_servers(
        args,
        {},
        tenant_id="demo-tenant",
        workspace_roots=[tmp_path],
        settings=settings,
    )

    assert resolved.process_specs == [process_spec]
    assert resolved.tenant_specs == [tenant_spec]
    rewrite.assert_called_once_with([process_spec], settings.gateway_url_prefix)
