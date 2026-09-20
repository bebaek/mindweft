from __future__ import annotations

import json
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
    return build_parser().parse_args(["code", *argv])


@pytest.fixture
def clean_environment(monkeypatch, tmp_path):
    for key in list(code.os.environ):
        if key.startswith(("MINDWEFT_", "MINIGENT_")):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(code.shutil, "which", Mock(return_value="/usr/bin/npx"))
    monkeypatch.setattr(code, "check_port", Mock())


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
    assert events == ["ready", "http://127.0.0.1:8000/console/"]


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
        ("npx", "Node.js"),
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
    if case == "npx":
        monkeypatch.setattr(code.shutil, "which", Mock(return_value=None))
    if case == "provider":
        argv = []
    if case == "auth":
        monkeypatch.setenv("MINDWEFT_AUTH_MODE", "static-tokens")
    if case == "ports":
        argv += ["--gateway-port", "8000"]
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
    assert "manually" in capsys.readouterr().err


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
