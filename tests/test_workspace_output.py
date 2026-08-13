from __future__ import annotations

from pathlib import Path

from minigent_workspace import output
from minigent_workspace.mcp_specs import CodingMCPServerSpec


def test_print_workspace_summary_reports_resolved_runtime(tmp_path: Path, capsys) -> None:
    env_file = tmp_path / ".env.coding"
    env_file.touch()
    source_spec = CodingMCPServerSpec(
        name="fs-workspace",
        url="http://127.0.0.1:8765/mcp",
        managed=True,
    )
    tenant_spec = CodingMCPServerSpec(
        name="fs-workspace",
        url="http://127.0.0.1:8765/mcp/fs-workspace",
        managed=True,
    )
    servers_file = tmp_path / "servers.json"

    output.print_workspace_summary(
        env_file=str(env_file),
        no_env_file=False,
        env_file_explicit=True,
        workspace_roots=[tmp_path],
        workspace_scope="default",
        tenant_id="demo-tenant",
        mcp_servers_file=servers_file,
        mcp_server_specs=[source_spec],
        tenant_mcp_server_specs=[tenant_spec],
        gateway_url_prefix="http://127.0.0.1:8765/mcp",
        api_host="127.0.0.1",
        api_port=8000,
    )

    assert capsys.readouterr().out.splitlines() == [
        f"loaded_env_file={env_file}",
        f"workspaces={tmp_path}",
        "workspace_scope=default",
        "tenant_id=demo-tenant",
        f"mcp_servers_file={servers_file} (legacy input; export emits inline specs)",
        "mcp_server=fs-workspace url=http://127.0.0.1:8765/mcp/fs-workspace "
        "transport=stdio managed=true bridge_url=http://127.0.0.1:8765/mcp",
        "mcp_gateway=http://127.0.0.1:8765/mcp",
        "api=http://127.0.0.1:8000",
    ]


def test_print_workspace_summary_reports_missing_optional_env_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    output.print_workspace_summary(
        env_file=".env.coding",
        no_env_file=False,
        env_file_explicit=False,
        workspace_roots=[tmp_path],
        workspace_scope=None,
        tenant_id="demo-tenant",
        mcp_servers_file=None,
        mcp_server_specs=[],
        tenant_mcp_server_specs=[],
        gateway_url_prefix=None,
        api_host="localhost",
        api_port=9000,
    )

    assert capsys.readouterr().out.splitlines() == [
        "optional_env_file_not_found=.env.coding",
        f"workspaces={tmp_path}",
        "tenant_id=demo-tenant",
        "api=http://localhost:9000",
    ]


def test_print_demo_commands_reports_enabled_tools(tmp_path: Path, capsys) -> None:
    output.print_demo_commands(
        "127.0.0.1",
        8000,
        "demo-tenant",
        tmp_path,
        "fs-workspace",
        "text-workspace",
        "shell-workspace",
    )

    rendered = capsys.readouterr().out
    assert "fs-workspace.list_directory" in rendered
    assert "text-workspace.read_text_file_around" in rendered
    assert "shell-workspace.run_command" in rendered
    assert rendered.endswith("Press Ctrl-C to stop.\n")
