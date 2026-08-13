from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from app import coding_workspace_runner as legacy_runner
from minigent_workspace import application, cli
from minigent_workspace.mcp_resolution import ResolvedMCPServers
from minigent_workspace.mcp_specs import CodingMCPServerSpec
from minigent_workspace.runtime_plan import WorkspaceRuntimePlan
from minigent_workspace.runtime_settings import WorkspaceRuntimeSettings
from minigent_workspace.scopes import WorkspaceScope


def runtime_plan(tmp_path: Path, *, mismatches: list[str] | None = None) -> WorkspaceRuntimePlan:
    settings = WorkspaceRuntimeSettings(
        api_host="127.0.0.1",
        api_port=8000,
        bridge_host="127.0.0.1",
        bridge_port=8765,
        bridge_name="fs-workspace",
        bridge_url="http://127.0.0.1:8765/mcp",
        gateway_enabled=True,
        gateway_port=8765,
        gateway_path_prefix="/mcp",
        gateway_url_prefix="http://127.0.0.1:8765/mcp",
        text_enabled=True,
        text_bridge_name="text-workspace",
        text_bridge_port=8767,
        text_bridge_url="http://127.0.0.1:8767/mcp",
        shell_enabled=True,
        shell_bridge_name="shell-workspace",
        shell_bridge_port=8766,
        shell_bridge_url="http://127.0.0.1:8766/mcp",
    )
    process_spec = CodingMCPServerSpec(name="files", url="http://127.0.0.1:8765/mcp")
    tenant_spec = CodingMCPServerSpec(name="files", url="http://127.0.0.1:8765/mcp/files")
    return WorkspaceRuntimePlan(
        env={"PATH": "/bin"},
        tenant_id="tenant",
        workspace_roots=[tmp_path],
        active_workspace_scope=WorkspaceScope("repo", [tmp_path]),
        settings=settings,
        mcp_servers=ResolvedMCPServers(tmp_path / "servers.json", [process_spec], [tenant_spec]),
        gateway_mcp_server_mismatches=mismatches or [],
    )


def test_runner_reexports_canonical_application_helper() -> None:
    assert legacy_runner.main is application.main
    assert legacy_runner.run_workspace_command is application.run_workspace_command


def test_application_main_routes_config_command(monkeypatch) -> None:
    config = Mock(return_value=4)
    workspace = Mock()
    monkeypatch.setattr(application, "run_config_command", config)
    monkeypatch.setattr(application, "run_workspace_command", workspace)

    assert application.main(["config", "export", "--no-env-file"]) == 4

    config.assert_called_once_with(["config", "export", "--no-env-file"])
    workspace.assert_not_called()


def test_application_main_routes_workspace_command(monkeypatch) -> None:
    config = Mock()
    workspace = Mock(return_value=5)
    monkeypatch.setattr(application, "run_config_command", config)
    monkeypatch.setattr(application, "run_workspace_command", workspace)

    assert application.main(["--no-env-file"]) == 5

    workspace.assert_called_once_with(["--no-env-file"])
    config.assert_not_called()


def test_run_workspace_command_prepares_reports_and_runs(tmp_path: Path, monkeypatch) -> None:
    plan = runtime_plan(tmp_path, mismatches=["missing"])
    loaded_env = plan.env
    parse = Mock(return_value=cli.parse_args(["--env-file", str(tmp_path / ".env.coding")]))
    load = Mock(return_value=loaded_env)
    state_defaults = Mock()
    prepare = Mock(return_value=plan)
    summary = Mock()
    run = Mock(return_value=7)
    monkeypatch.setattr(application, "parse_args", parse)
    monkeypatch.setattr(application, "load_env_file", load)
    monkeypatch.setattr(application, "apply_coding_workspace_state_defaults", state_defaults)
    monkeypatch.setattr(application, "prepare_workspace_runtime", prepare)
    monkeypatch.setattr(application, "print_workspace_summary", summary)
    monkeypatch.setattr(application, "run_workspace_processes", run)
    argv = ["--env-file", str(tmp_path / ".env.coding")]

    assert application.run_workspace_command(argv) == 7

    parse.assert_called_once_with(argv)
    load.assert_called_once_with(str(tmp_path / ".env.coding"), warn_if_missing=True)
    state_defaults.assert_called_once_with(loaded_env)
    prepare.assert_called_once_with(parse.return_value, loaded_env)
    summary.assert_called_once_with(
        env_file=str(tmp_path / ".env.coding"),
        no_env_file=False,
        env_file_explicit=True,
        workspace_roots=[tmp_path],
        workspace_scope="repo",
        tenant_id="tenant",
        mcp_servers_file=tmp_path / "servers.json",
        mcp_server_specs=plan.mcp_servers.process_specs,
        tenant_mcp_server_specs=plan.mcp_servers.tenant_specs,
        gateway_url_prefix=plan.settings.gateway_url_prefix,
        api_host=plan.settings.api_host,
        api_port=plan.settings.api_port,
    )
    run.assert_called_once_with(
        env=loaded_env,
        mcp_server_specs=plan.mcp_servers.process_specs,
        skip_bridge=False,
        gateway_enabled=True,
        bridge_host="127.0.0.1",
        gateway_port=8765,
        skip_api=False,
        api_host="127.0.0.1",
        api_port=8000,
        tenant_id="tenant",
        workspace=tmp_path,
        bridge_name="fs-workspace",
        text_bridge_name="text-workspace",
        shell_bridge_name="shell-workspace",
    )


def test_run_workspace_command_reports_gateway_mismatch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    plan = runtime_plan(tmp_path, mismatches=["missing"])
    monkeypatch.setattr(application, "load_env_file", Mock(return_value=plan.env))
    monkeypatch.setattr(application, "apply_coding_workspace_state_defaults", Mock())
    monkeypatch.setattr(application, "prepare_workspace_runtime", Mock(return_value=plan))
    monkeypatch.setattr(application, "print_workspace_summary", Mock())
    monkeypatch.setattr(application, "run_workspace_processes", Mock(return_value=0))

    assert application.run_workspace_command([]) == 0

    assert "tenant MCP server 'missing'" in capsys.readouterr().err


def test_run_workspace_command_reports_preparation_error(monkeypatch, capsys) -> None:
    env: dict[str, str] = {}
    monkeypatch.setattr(application, "load_env_file", Mock(return_value=env))
    monkeypatch.setattr(application, "apply_coding_workspace_state_defaults", Mock())
    monkeypatch.setattr(
        application, "prepare_workspace_runtime", Mock(side_effect=RuntimeError("bad workspace"))
    )
    run = Mock()
    monkeypatch.setattr(application, "run_workspace_processes", run)

    assert application.run_workspace_command([]) == 2

    assert capsys.readouterr().err == "bad workspace\n"
    run.assert_not_called()


def test_run_workspace_command_skips_dotenv(monkeypatch) -> None:
    env: dict[str, str] = {}
    load = Mock(return_value=env)
    monkeypatch.setattr(application, "load_env_file", load)
    monkeypatch.setattr(application, "apply_coding_workspace_state_defaults", Mock())
    monkeypatch.setattr(
        application, "prepare_workspace_runtime", Mock(side_effect=RuntimeError("stop"))
    )

    assert application.run_workspace_command(["--no-env-file"]) == 2

    load.assert_called_once_with(None, warn_if_missing=False)
