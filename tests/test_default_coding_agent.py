from __future__ import annotations

import argparse
import io
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.admin_store import SQLiteTenantConfigStore
from app.main import create_app
from mindweft_client.cli import _write_current_agent
from mindweft_client.config import ClientConfig
from mindweft_workspace.cli import parse_args
from mindweft_workspace.runtime_plan import prepare_workspace_runtime
from mindweft_workspace.servers.writable import WorkspaceWritePolicy
from mindweft_workspace.workspace_trust import choose_coding_access


@pytest.mark.parametrize("trusted", [False, True])
def test_default_agent_is_real_and_personal_copy_is_persistent(tmp_path, monkeypatch, trusted):
    env = {"MINDWEFT_LOCAL_CODING_TRUSTED": "1" if trusted else "0"}
    plan = prepare_workspace_runtime(
        parse_args(
            ["--no-env-file", "--workspace", str(tmp_path), "--enable-text", "--mcp-gateway"]
        ),
        env,
        bundled_readonly=True,
    )
    config = json.loads(env["MINIGENT_TENANT_EXECUTION_CONFIGS"])["demo-tenant"]
    profile = "coding" if trusted else "inspect"
    assert config["agents"]["default_agent"] == "coding"
    assert config["capability_profiles"]["default_profile"] == profile
    specs = {spec.name: spec for spec in plan.mcp_servers.process_specs}
    assert ("shell-workspace" in specs) is trusted
    assert ("write_file" in specs["fs-workspace"].allowed_tools) is trusted
    assert ("--writable" in specs["fs-workspace"].command) is trusted
    if trusted:
        assert "--no-login-shell" in specs["shell-workspace"].command
    # Do not start real gateway processes in the API/catalog test.
    config["tools"]["mcp_servers"] = []
    for capability in config["capability_profiles"]["items"]:
        capability["mcp_server_names"] = []
    monkeypatch.setenv("MINDWEFT_TENANT_EXECUTION_CONFIGS", json.dumps({"demo-tenant": config}))
    monkeypatch.setenv("MINDWEFT_AUTH_MODE", "dev-headers")
    headers = {"X-Mindweft-Tenant-Id": "demo-tenant", "X-Mindweft-User-Id": "demo-user"}
    store = SQLiteTenantConfigStore(str(tmp_path / "personal.db"))
    with TestClient(create_app(admin_store=store)) as client:
        options = client.get("/execution-options", headers=headers).json()
        assert options["agents"]["default"] in {"coding", "shared:coding"}
        assert any(item["name"] == "coding" for item in options["agents"]["items"])
        created = client.post("/threads", headers=headers, json={})
        assert created.status_code == 200
        # A personal copy can be customized and selected without changing the built-in.
        saved = client.put(
            "/me/execution-config",
            headers=headers,
            json={
                "expected_version": 0,
                "config": {
                    "defaults": {"agent_ref": "user:my-coding"},
                    "agents": {
                        "items": [
                            {
                                "id": "user:my-coding",
                                "name": "my-coding",
                                "skill_refs": ["shared:coding-workspace"],
                                "capability_profile_ref": "shared:coding",
                            },
                        ],
                    },
                },
            },
        )
        assert saved.status_code == 200, saved.text
        options = client.get("/execution-options", headers=headers).json()
        assert options["agents"]["default"] == "user:my-coding"
        selected = client.post("/threads", headers=headers, json={"agent_name": "coding"})
        assert selected.status_code == 200, selected.text
    with TestClient(
        create_app(admin_store=SQLiteTenantConfigStore(str(tmp_path / "personal.db")))
    ) as client:
        assert (
            client.get("/execution-options", headers=headers).json()["agents"]["default"]
            == "user:my-coding"
        )


def test_workspace_trust_is_explicit_and_scoped(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    env = {"XDG_STATE_HOME": str(tmp_path)}
    args = argparse.Namespace(read_only=False, trust_workspace=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(RuntimeError, match="trust required"):
        choose_coding_access(args, [root], env)
    args.trust_workspace = True
    assert choose_coding_access(args, [root], env)
    args.trust_workspace = False
    assert choose_coding_access(args, [root], env)
    another = tmp_path / "another"
    another.mkdir()
    with pytest.raises(RuntimeError, match="trust required"):
        choose_coding_access(args, [root, another], env)
    args.read_only = True
    assert not choose_coding_access(args, [root], env)


def test_bounded_edits_and_safe_write_paths(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    policy = WorkspaceWritePolicy([root])
    target = root / "main.py"
    policy.write_text(str(target), "value = 1\n")
    assert target.read_text() == "value = 1\n"
    preview = policy.edit_text(str(target), "1", "2", dry_run=True)
    assert "+value = 2" in preview["diff"]
    assert target.read_text() == "value = 1\n"
    policy.edit_text(str(target), "1", "2")
    assert target.read_text() == "value = 2\n"
    with pytest.raises(ValueError, match="exactly once"):
        policy.edit_text(str(target), "missing", "2")
    with pytest.raises(ValueError):
        policy.write_text(str(tmp_path / "outside"), "no")
    (root / ".credentials").mkdir()
    with pytest.raises(ValueError):
        policy.write_text(str(root / ".credentials" / "token"), "no")
    (root / "link").symlink_to(target)
    with pytest.raises(ValueError):
        policy.write_text(str(root / "link"), "no")
    with pytest.raises(ValueError):
        policy.write_text(str(target), "x" * 1_048_577)


def test_cli_current_agent_reports_server_default_before_first_message():
    client = Mock(active_agent_preset=None, thread_id=None)
    client.execution_options.return_value = {"agents": {"default": "shared:coding"}}
    output = io.StringIO()
    _write_current_agent(
        client, ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey"), output
    )
    assert "shared:coding (server default)" in output.getvalue()


def test_cli_current_agent_reports_persisted_thread_agent():
    client = Mock(active_agent_preset=None, thread_id="thread-one")
    client.get_thread_lineage.return_value = {"thread": {"agent_ref": "shared:coding"}}
    output = io.StringIO()
    _write_current_agent(
        client, ClientConfig(base_url="http://127.0.0.1:8000", wake_phrase="hey"), output
    )
    assert "shared:coding (thread)" in output.getvalue()
    client.execution_options.assert_not_called()
