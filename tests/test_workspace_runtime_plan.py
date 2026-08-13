from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from minigent_workspace import cli, runtime_plan
from minigent_workspace.mcp_resolution import ResolvedMCPServers
from minigent_workspace.mcp_specs import CodingMCPServerSpec
from minigent_workspace.runtime_settings import WorkspaceRuntimeSettings
from minigent_workspace.scopes import WorkspaceScope


def settings(*, gateway_enabled: bool = False) -> WorkspaceRuntimeSettings:
    return WorkspaceRuntimeSettings(
        api_host="127.0.0.1",
        api_port=8000,
        bridge_host="127.0.0.1",
        bridge_port=8765,
        bridge_name="fs-workspace",
        bridge_url="http://127.0.0.1:8765/mcp",
        gateway_enabled=gateway_enabled,
        gateway_port=8765,
        gateway_path_prefix="/mcp",
        gateway_url_prefix="http://127.0.0.1:8765/mcp",
        text_enabled=False,
        text_bridge_name="text-workspace",
        text_bridge_port=8767,
        text_bridge_url="http://127.0.0.1:8767/mcp",
        shell_enabled=False,
        shell_bridge_name="shell-workspace",
        shell_bridge_port=8766,
        shell_bridge_url="http://127.0.0.1:8766/mcp",
    )


def test_prepare_workspace_runtime_coordinates_resolvers(tmp_path: Path, monkeypatch) -> None:
    args = cli.parse_args(["--tenant-id", "tenant", "--workspace-scope", "repo"])
    env: dict[str, str] = {}
    scope = WorkspaceScope("repo", [tmp_path])
    runtime_settings = settings()
    spec = CodingMCPServerSpec(name="fs-workspace", url=runtime_settings.bridge_url)
    resolved_mcp = ResolvedMCPServers(None, [spec], [spec])
    select = Mock(return_value=([tmp_path], scope))
    resolve_settings = Mock(return_value=runtime_settings)
    resolve_mcp = Mock(return_value=resolved_mcp)
    apply_env = Mock()
    mismatches = Mock()
    monkeypatch.setattr(runtime_plan, "resolve_workspace_selection", select)
    monkeypatch.setattr(runtime_plan, "resolve_workspace_runtime_settings", resolve_settings)
    monkeypatch.setattr(runtime_plan, "resolve_workspace_mcp_servers", resolve_mcp)
    monkeypatch.setattr(runtime_plan, "apply_tenant_runtime_environment", apply_env)
    monkeypatch.setattr(runtime_plan, "tenant_gateway_mcp_server_mismatches", mismatches)

    plan = runtime_plan.prepare_workspace_runtime(args, env)

    assert plan == runtime_plan.WorkspaceRuntimePlan(
        env=env,
        tenant_id="tenant",
        workspace_roots=[tmp_path],
        active_workspace_scope=scope,
        settings=runtime_settings,
        mcp_servers=resolved_mcp,
        gateway_mcp_server_mismatches=[],
    )
    select.assert_called_once_with(args.workspace, "repo", env, tenant_id="tenant")
    resolve_settings.assert_called_once_with(args, env)
    resolve_mcp.assert_called_once_with(
        args,
        env,
        tenant_id="tenant",
        workspace_roots=[tmp_path],
        settings=runtime_settings,
    )
    apply_env.assert_called_once_with(
        env,
        "tenant",
        [spec],
        workspace_roots=[tmp_path],
        workspace_scope="repo",
    )
    mismatches.assert_not_called()


def test_prepare_workspace_runtime_uses_environment_tenant_and_gateway_validation(
    tmp_path: Path, monkeypatch
) -> None:
    args = cli.parse_args([])
    env = {"MINIGENT_CODING_TENANT_ID": "env-tenant"}
    runtime_settings = settings(gateway_enabled=True)
    process_spec = CodingMCPServerSpec(name="files", url="http://127.0.0.1:8765/mcp")
    tenant_spec = CodingMCPServerSpec(name="files", url="http://127.0.0.1:8765/mcp/files")
    resolved_mcp = ResolvedMCPServers(None, [process_spec], [tenant_spec])
    monkeypatch.setattr(
        runtime_plan, "resolve_workspace_selection", Mock(return_value=([tmp_path], None))
    )
    monkeypatch.setattr(
        runtime_plan, "resolve_workspace_runtime_settings", Mock(return_value=runtime_settings)
    )
    monkeypatch.setattr(
        runtime_plan, "resolve_workspace_mcp_servers", Mock(return_value=resolved_mcp)
    )
    monkeypatch.setattr(runtime_plan, "apply_tenant_runtime_environment", Mock())
    mismatches = Mock(return_value=["missing"])
    monkeypatch.setattr(runtime_plan, "tenant_gateway_mcp_server_mismatches", mismatches)

    plan = runtime_plan.prepare_workspace_runtime(args, env)

    assert plan.tenant_id == "env-tenant"
    assert plan.gateway_mcp_server_mismatches == ["missing"]
    mismatches.assert_called_once_with(
        env,
        "env-tenant",
        gateway_url_prefix=runtime_settings.gateway_url_prefix,
        specs=[process_spec],
    )


def test_prepare_workspace_runtime_defaults_tenant(tmp_path: Path, monkeypatch) -> None:
    args = cli.parse_args([])
    monkeypatch.setattr(
        runtime_plan, "resolve_workspace_selection", Mock(return_value=([tmp_path], None))
    )
    monkeypatch.setattr(
        runtime_plan, "resolve_workspace_runtime_settings", Mock(return_value=settings())
    )
    monkeypatch.setattr(
        runtime_plan,
        "resolve_workspace_mcp_servers",
        Mock(return_value=ResolvedMCPServers(None, [], [])),
    )
    monkeypatch.setattr(runtime_plan, "apply_tenant_runtime_environment", Mock())

    plan = runtime_plan.prepare_workspace_runtime(args, {})

    assert plan.tenant_id == "demo-tenant"
