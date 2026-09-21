from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest

from mindweft_client import application
from mindweft_client.one_shot_parser import build_parser
from mindweft_workspace import code_command as code
from mindweft_workspace import readiness
from mindweft_workspace.cli import parse_args
from mindweft_workspace.runtime_plan import prepare_workspace_runtime


def args(*argv):
    return build_parser().parse_args(["code", "--read-only", *argv])


@pytest.fixture
def clean_environment(monkeypatch, tmp_path):
    for key in list(code.os.environ):
        if key.startswith(("MINDWEFT_", "MINIGENT_")):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        code,
        "browser_url",
        lambda record, credential: record.api_url + "/console/#local_ticket=test-ticket",
    )

    @contextmanager
    def fake_ports(api, gateway):
        sockets = [Mock(), Mock()]
        sockets[0].getsockname.return_value = ("127.0.0.1", api or 8000)
        sockets[1].getsockname.return_value = ("127.0.0.1", gateway or 8765)
        yield tuple(sockets)

    monkeypatch.setattr(code, "reserve_ports", fake_ports)


def test_cli_routes_before_loading_dotenv_or_building_client(monkeypatch):
    run = Mock(return_value=7)
    monkeypatch.setattr(code, "run_code_command", run)
    monkeypatch.setattr(application, "build_client", Mock(side_effect=AssertionError))
    monkeypatch.setattr(application, "build_config", Mock(side_effect=AssertionError))
    monkeypatch.setattr(application, "_apply_cli_env_file", Mock(side_effect=AssertionError))
    assert application.main(["code", ".", "--no-open"]) == 7
    assert run.call_args.args[0].no_open


def test_only_user_config_and_provider_settings_are_inherited(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mindweft.toml").write_text("not valid TOML")
    config_home = tmp_path / "config"
    config = config_home / "mindweft" / "mindweft.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[llm]\nprovider="openrouter"\nmodel="model"\n[coding]\nshell_enabled=true\nworkspaces=["/"]\n'
    )
    source = {
        "XDG_CONFIG_HOME": str(config_home),
        "OPENROUTER_API_KEY": "test-value",
        "MINDWEFT_LLM_MODEL": "override",
        "MINDWEFT_CODING_SHELL_ENABLED": "true",
        "MINIGENT_CODING_FILESYSTEM_COMMAND": "unsafe-command",
        "MINDWEFT_TENANT_EXECUTION_CONFIGS": '{"demo-tenant":{}}',
        "MINIGENT_ADMIN_DB_PATH": "/unwanted",
        "MINDWEFT_MCP_SERVERS": "[]",
        "MINDWEFT_DOTENV_FILE": "/must-not-read",
        "MINIGENT_LLM_API_KEY_FILE": "/must-not-read",
        "PYTHONPATH": str(tmp_path),
    }
    env = code.load_code_environment(source)
    assert env["MINIGENT_LLM_MODEL"] == "override"
    assert env["MINIGENT_LLM_PROVIDER"] == "openrouter"
    assert env["OPENROUTER_API_KEY"] == "test-value"
    assert env["MINIGENT_CONFIG_DISCOVERY"] == "disabled"
    assert "PYTHONPATH" not in env
    assert not any(
        "CODING_" in key
        or "TENANT_" in key
        or "ADMIN_" in key
        or "MCP_SERVERS" in key
        or key.endswith("_FILE")
        for key in env
    )

    plan = prepare_workspace_runtime(
        parse_args(["--workspace", str(tmp_path), "--mcp-gateway", "--enable-text"]), env
    )
    assert plan.workspace_roots == [tmp_path]
    assert not plan.settings.shell_enabled
    specs = plan.mcp_servers.process_specs
    assert [s.name for s in specs] == ["fs-workspace", "text-workspace"]
    assert specs[0].command == [
        "npx",
        "-y",
        "@modelcontextprotocol/server-filesystem",
        str(tmp_path),
    ]
    assert specs[0].allowed_tools == ["list_allowed_directories", "list_directory", "read_file"]
    tenant = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])["demo-tenant"]
    assert tenant["capability_profiles"]["default_profile"] == "inspect"
    assert len(tenant["capability_profiles"]["items"]) == 1


def test_explicit_config_missing_fails_without_fallback(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        code.load_code_environment({"MINDWEFT_CONFIG_FILE": str(tmp_path / "missing")})


def test_config_errors_do_not_echo_contents(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("secret-value invalid")
    with pytest.raises(RuntimeError) as exc:
        code.load_code_environment({"MINDWEFT_CONFIG_FILE": str(path)})
    assert "secret-value" not in str(exc.value)


@pytest.mark.parametrize("path", [".", "project with spaces"])
def test_launch_uses_absolute_root_and_opens_only_after_readiness(
    path, clean_environment, tmp_path, monkeypatch
):
    if path != ".":
        (tmp_path / path).mkdir()
    events = []
    monkeypatch.setattr(code, "wait_for_code_ready", lambda *a, **kw: events.append("ready"))
    monkeypatch.setattr(code, "open_console", lambda url: events.append(url))

    def run(**kw):
        assert events == []
        assert kw["workspace"] == (tmp_path / path).resolve()
        assert kw["process_cwd"] != tmp_path
        assert kw["process_cwd"].is_dir()
        assert kw["shell_bridge_name"] is None
        kw["on_started"]([])
        return 0

    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args(path, "--demo")) == 0
    assert events == ["ready", "http://127.0.0.1:8000/console/#local_ticket=test-ticket"]


def test_no_open(clean_environment, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(code, "open_console", opened)
    monkeypatch.setattr(code, "wait_for_code_ready", Mock())

    def run(**kw):
        kw["on_started"]([])
        return 0

    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args("--demo", "--no-open")) == 0
    opened.assert_not_called()


@pytest.mark.parametrize(
    "case, message",
    [
        ("missing", "accessible directory"),
        ("file", "accessible directory"),
        ("provider", "Configure a real provider"),
        ("auth", "development authentication only"),
        ("ports", "must be different"),
    ],
)
def test_preflight_never_spawns(case, message, clean_environment, tmp_path, monkeypatch, capsys):
    argv = ["--demo"]
    if case == "missing":
        argv.insert(0, "missing")
    if case == "file":
        (tmp_path / "file").write_text("fixture")
        argv.insert(0, "file")
    if case == "provider":
        argv = []
    if case == "auth":
        monkeypatch.setenv("MINDWEFT_AUTH_MODE", "static-tokens")
    if case == "ports":
        argv += ["--port", "8000", "--gateway-port", "8000"]
    run = Mock()
    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args(*argv)) == 2
    assert message in capsys.readouterr().err
    run.assert_not_called()


def test_provider_prerequisites_and_demo():
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        code.check_provider({"MINIGENT_LLM_PROVIDER": "openrouter"}, demo=False)
    env = {"MINIGENT_LLM_PROVIDER": "openrouter", "MINDWEFT_LLM_PROFILES": "unsafe"}
    code.check_provider(env, demo=True)
    assert env == {"MINIGENT_LLM_PROVIDER": "mock"}


def test_browser_failure_is_nonfatal(monkeypatch, capsys):
    monkeypatch.setattr(code.webbrowser, "open", Mock(side_effect=OSError))
    code.open_console("http://127.0.0.1:8000/console/")
    assert "instances open" in capsys.readouterr().err


def test_ready_requires_api_console_profile_and_exact_tools(monkeypatch, tmp_path):
    plan = prepare_workspace_runtime(
        parse_args(["--workspace", str(tmp_path), "--mcp-gateway", "--enable-text"]), {}
    )
    specs = plan.mcp_servers.tenant_specs
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/execution-options":
            return httpx.Response(200, json={"capability_profiles": {"default": "shared:inspect"}})
        if request.url.path.startswith("/mcp/"):
            spec = next(s for s in specs if s.url.endswith(request.url.path))
            return httpx.Response(
                200, json={"result": {"tools": [{"name": n} for n in spec.allowed_tools]}}
            )
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(readiness.httpx, "Client", Mock(return_value=client))
    process = Mock()
    process.poll.return_value = None
    readiness.wait_for_code_ready([process], api_port=8000, specs=specs)
    assert requests == [
        "/health/ready",
        "/console/",
        "/execution-options",
        "/mcp/fs-workspace",
        "/mcp/text-workspace",
    ]


def test_ready_rejects_child_exit():
    process = Mock()
    process.poll.return_value = 1
    with pytest.raises(RuntimeError, match="exited before readiness"):
        readiness.wait_for_code_ready([process], api_port=8000, specs=[])


def test_ready_timeout_does_not_echo_remote_error(monkeypatch):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="secret-value"))
    )
    monkeypatch.setattr(readiness.httpx, "Client", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="did not become ready") as exc:
        readiness.wait_for_code_ready([], api_port=8000, specs=[], timeout=0.01)
    assert "secret-value" not in str(exc.value)


def test_relative_storage_is_resolved_before_temporary_cwd(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "mindweft.toml"
    config.write_text('[app]\nthread_db_path="threads.db"\n')
    env = code.load_code_environment({"MINDWEFT_CONFIG_FILE": str(config)})
    assert env["MINIGENT_THREAD_DB_PATH"] == str(config_dir / "threads.db")
    monkeypatch.chdir(tmp_path)
    env = code.load_code_environment(
        {"MINIGENT_THREAD_DB_PATH": "override.db", "MINDWEFT_CONFIG_FILE": str(config)}
    )
    assert env["MINIGENT_THREAD_DB_PATH"] == str(tmp_path / "override.db")


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_port(port):
    with pytest.raises(RuntimeError, match="between 1 and 65535"):
        code.check_port(port, "--port")


def test_port_conflict():
    with code.socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        with pytest.raises(RuntimeError, match="--gateway-port"):
            code.check_port(sock.getsockname()[1], "--gateway-port")


def test_sigterm_handler_restored():
    previous = code.signal.getsignal(code.signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), code.shutdown_on_sigterm():
        handler = code.signal.getsignal(code.signal.SIGTERM)
        handler(code.signal.SIGTERM, None)
    assert code.signal.getsignal(code.signal.SIGTERM) == previous


def test_readiness_failure_does_not_open_browser(clean_environment, monkeypatch, capsys):
    opened = Mock()
    monkeypatch.setattr(code, "open_console", opened)
    monkeypatch.setattr(code, "wait_for_code_ready", Mock(side_effect=RuntimeError("not ready")))

    def run(**kw):
        kw["on_started"]([])

    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args("--demo")) == 2
    opened.assert_not_called()
    assert "Ready:" not in capsys.readouterr().out


@pytest.mark.parametrize("tools", [[], [{"name": "write_file"}]])
def test_readiness_rejects_missing_or_unexpected_tools(monkeypatch, tmp_path, tools):
    plan = prepare_workspace_runtime(parse_args(["--workspace", str(tmp_path)]), {})

    def handle(request):
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/execution-options":
            return httpx.Response(200, json={"capability_profiles": {"default": "inspect"}})
        if request.url.path == "/console/":
            return httpx.Response(200)
        return httpx.Response(200, json={"result": {"tools": tools}})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(readiness.httpx, "Client", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="tool discovery"):
        readiness.wait_for_code_ready(
            [], api_port=8000, specs=plan.mcp_servers.tenant_specs, timeout=0.01
        )


def test_multiple_roots_deduplicate_and_ignore_inherited_roots(
    clean_environment, tmp_path, monkeypatch, capsys
):
    first = tmp_path / "first project"
    second = tmp_path / "second project"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    monkeypatch.setenv("MINDWEFT_CODING_WORKSPACES", "/")
    monkeypatch.setenv("MINIGENT_CODING_WORKSPACE", "/")
    monkeypatch.setenv("MINDWEFT_CODING_SHELL_ENABLED", "true")
    run = Mock(return_value=0)
    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert (
        code.run_code_command(args(str(first), str(second), str(alias), str(second), "--demo")) == 0
    )
    options = run.call_args.kwargs
    specs = options["mcp_server_specs"]
    assert options["workspace"] == first
    assert options["shell_bridge_name"] is None
    assert len(specs) == 2
    assert specs[0].command == [
        code.sys.executable,
        "-m",
        "mindweft_workspace.servers.filesystem",
        "--workspace",
        str(first),
        "--workspace",
        str(second),
    ]
    assert specs[1].command[-1] == "--safe-reads"
    assert specs[1].command.count(str(first)) == 1
    assert specs[1].command.count(str(second)) == 1
    assert "/" not in specs[0].command
    tenant = json.loads(options["env"]["MINIGENT_TENANT_EXECUTION_CONFIGS"])["demo-tenant"]
    prompt = tenant["skills"]["items"][0]["system_prompt"]
    assert str(first) in prompt and str(second) in prompt
    assert {item["name"] for item in tenant["capability_profiles"]["items"]} == {
        "inspect",
        "coding",
    }
    output = capsys.readouterr().out
    assert output.count(str(first)) == 1
    assert output.count(str(second)) == 1
    assert "read-only" in output


def test_invalid_second_root_aborts_before_spawning(
    clean_environment, tmp_path, monkeypatch, capsys
):
    run = Mock()
    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args(str(tmp_path), str(tmp_path / "missing"), "--demo")) == 2
    run.assert_not_called()
    assert "accessible directory" in capsys.readouterr().err


def test_no_paths_defaults_to_cwd(clean_environment, tmp_path, monkeypatch):
    run = Mock(return_value=0)
    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args("--demo", "--no-open")) == 0
    assert run.call_args.kwargs["workspace"] == tmp_path


def test_named_instance_isolates_conversations_but_reuses_configured_oauth(
    clean_environment, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("MINDWEFT_THREAD_DB_PATH", str(tmp_path / "daily.db"))
    monkeypatch.setenv("MINIGENT_ATTACHMENT_DB_PATH", str(tmp_path / "attachments.db"))
    monkeypatch.setenv("MINDWEFT_OAUTH_STORE_PATH", str(tmp_path / "oauth.json"))
    run = Mock(return_value=0)
    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args("--demo", "--instance", "preview")) == 0
    env = run.call_args.kwargs["env"]
    root = tmp_path / "mindweft" / "instances" / "preview"
    assert env["MINIGENT_THREAD_DB_PATH"] == str(root / "threads.db")
    assert env["MINIGENT_ATTACHMENT_DB_PATH"] == str(root / "attachments.db")
    assert env["MINIGENT_OAUTH_STORE_PATH"] == str(tmp_path / "oauth.json")
    assert env["MINDWEFT_OAUTH_STORE_PATH"] == str(tmp_path / "oauth.json")
    assert not (root / "oauth.json").exists()
    assert env["MINDWEFT_LOCAL_INSTANCE_NAME"] == "preview"
    assert run.call_args.kwargs["inherited_fds"]
    assert not (tmp_path / "daily.db").exists()
    captured = capsys.readouterr()
    assert "overrides configured THREAD_DB_PATH" in captured.err
    assert "overrides configured OAUTH_STORE_PATH" not in captured.err
    assert "reusing configured store" in captured.out
    assert "without cross-process refresh coordination" in captured.err


def test_default_refuses_automatic_fallback_to_shared_legacy_state(
    clean_environment, monkeypatch, capsys
):
    @contextmanager
    def ports(*args):
        first, second = Mock(), Mock()
        first.getsockname.return_value = ("127.0.0.1", 8999)
        second.getsockname.return_value = ("127.0.0.1", 8998)
        yield first, second

    monkeypatch.setattr(code, "reserve_ports", ports)
    run = Mock()
    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args("--demo")) == 2
    run.assert_not_called()
    assert "--instance preview" in capsys.readouterr().err


def test_config_source_reports_user_symlink_without_values(tmp_path, capsys):
    config = tmp_path / "config" / "mindweft" / "mindweft.toml"
    config.parent.mkdir(parents=True)
    target = tmp_path / "dotfiles.toml"
    target.write_text('[llm]\nprovider="openrouter"\napi_key="test-secret-not-for-output"\n')
    config.symlink_to(target)
    code.load_code_environment({"XDG_CONFIG_HOME": str(tmp_path / "config")}, report_source=True)
    output = capsys.readouterr().err
    assert f"Config: loaded {str(config)!r}" in output
    assert f"Config target: {str(target)!r}" in output
    assert "provider/auth/storage settings only" in output
    assert "test-secret-not-for-output" not in output


def test_config_source_reports_explicit_symlink_even_with_discovery_disabled(tmp_path, capsys):
    target = tmp_path / "target.toml"
    target.write_text('[llm]\nprovider="mock"\n')
    link = tmp_path / "selected.toml"
    link.symlink_to(target)
    code.load_code_environment(
        {"MINIGENT_CONFIG_FILE": str(link), "MINDWEFT_CONFIG_DISCOVERY": "disabled"},
        report_source=True,
    )
    output = capsys.readouterr().err
    assert f"Config: loaded {str(link)!r}" in output
    assert f"Config target: {str(target)!r}" in output
    assert "environment only" not in output


@pytest.mark.parametrize("disabled", [True, False])
def test_config_source_reports_no_file_reason(tmp_path, capsys, disabled):
    source = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    if disabled:
        source["MINDWEFT_CONFIG_DISCOVERY"] = "disabled"
    code.load_code_environment(source, report_source=True)
    reason = "file discovery disabled" if disabled else "no user-level TOML found"
    assert f"Config: environment only ({reason})" in capsys.readouterr().err


def test_config_source_reports_legacy_user_file(tmp_path, capsys):
    config = tmp_path / "config" / "minigent" / "minigent.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[llm]\nprovider="mock"\n')
    code.load_code_environment({"XDG_CONFIG_HOME": str(tmp_path / "config")}, report_source=True)
    output = capsys.readouterr().err
    assert f"Config: loaded {str(config)!r}" in output
    assert "Config target:" not in output


def test_bad_config_is_not_reported_as_loaded(tmp_path, capsys):
    config = tmp_path / "invalid.toml"
    config.write_text("invalid TOML test-secret")
    with pytest.raises(RuntimeError):
        code.load_code_environment({"MINDWEFT_CONFIG_FILE": str(config)}, report_source=True)
    output = capsys.readouterr().err
    assert "Config: loaded" not in output
    assert "test-secret" not in output


def test_config_source_visible_before_provider_failure(
    clean_environment, tmp_path, capsys, monkeypatch
):
    config = tmp_path / "selected.toml"
    config.write_text('[llm]\nprovider="mock"\n')
    monkeypatch.setenv("MINDWEFT_CONFIG_FILE", str(config))
    assert code.run_code_command(args()) == 2
    output = capsys.readouterr().err
    assert output.index("Config: loaded") < output.index("Configure a real provider")


def test_config_loader_remains_quiet_by_default(tmp_path, capsys):
    code.load_code_environment({"HOME": str(tmp_path)})
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("prefix", ["MINDWEFT", "MINIGENT"])
def test_named_instance_preserves_oauth_alias_and_encryption_settings(
    clean_environment, tmp_path, monkeypatch, capsys, prefix
):
    path = tmp_path / "shared-oauth.db"
    monkeypatch.setenv(f"{prefix}_OAUTH_STORE_PATH", str(path))
    monkeypatch.setenv(f"{prefix}_OAUTH_ENCRYPTION_KEY", "synthetic-test-key")
    run = Mock(return_value=0)
    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args("--demo", "--instance", "preview")) == 0
    env = run.call_args.kwargs["env"]
    assert env["MINIGENT_OAUTH_STORE_PATH"] == str(path)
    assert env["MINIGENT_OAUTH_ENCRYPTION_KEY"] == "synthetic-test-key"
    assert not path.exists()  # No migration, credential read, or creation by the launcher.
    captured = capsys.readouterr()
    assert "without cross-process refresh coordination" not in captured.err
    assert "synthetic-test-key" not in captured.out + captured.err


def test_unconfigured_oauth_remains_instance_local(clean_environment, tmp_path, monkeypatch):
    run = Mock(return_value=0)
    monkeypatch.setattr(code, "run_workspace_processes", run)
    assert code.run_code_command(args("--demo", "--instance", "preview")) == 0
    env = run.call_args.kwargs["env"]
    assert env["MINIGENT_OAUTH_STORE_PATH"] == str(
        tmp_path / "mindweft" / "instances" / "preview" / "oauth.json"
    )
