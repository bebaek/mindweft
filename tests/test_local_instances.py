from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from mindweft_client import application
from mindweft_client.chat_commands import state_scope_key
from mindweft_client.one_shot_parser import build_parser
from mindweft_workspace import instances


def test_instance_lease_prevents_duplicates_and_cleans_metadata(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    with instances.InstanceLease("preview", env) as lease:
        path = lease.root / "preview.json"
        assert not path.exists()  # not discoverable until ready/published
        with pytest.raises(RuntimeError, match="already running"):
            with instances.InstanceLease("preview", env):
                pass
        record = lease.publish(8100, 8865)
        assert instances.InstanceRecord.read(path) == record
        assert path.stat().st_mode & 0o777 == 0o600
        assert lease.root.stat().st_mode & 0o777 == 0o700
        assert lease.state_dir == tmp_path / "mindweft" / "instances" / "preview"
    assert not path.exists()
    with instances.InstanceLease("preview", env):
        pass


def test_lease_survives_launcher_exit_while_child_holds_fd(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    with instances.InstanceLease("preview", env) as lease:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(lease.fd,)
        )
    try:
        with pytest.raises(RuntimeError, match="already running"):
            with instances.InstanceLease("preview", env):
                pass
    finally:
        child.terminate()
        child.wait(timeout=5)
    with instances.InstanceLease("preview", env):
        pass


@pytest.mark.parametrize("name", ["", "..", "../other", "a/b", "a.json", "a" * 65, "bad name"])
def test_instance_names_are_safe(name):
    with pytest.raises(ValueError):
        instances.validate_name(name)


def test_default_state_is_compatible_and_named_state_rejects_symlink(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    with instances.InstanceLease("default", env) as lease:
        assert lease.state_dir == tmp_path / "mindweft"
        (lease.root / "preview").symlink_to(lease.state_dir, target_is_directory=True)
        with pytest.raises(OSError, match="symlink"):
            with instances.InstanceLease("preview", env):
                pass


def test_stale_record_replaced_only_after_new_publish(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    with instances.InstanceLease("preview", env) as lease:
        old = lease.publish(8100, 8865)
        # Simulate another launch's record: cleanup must never delete it.
        path = lease.root / "preview.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "launch_id": "b" * 32}))
    assert path.exists()
    with instances.InstanceLease("preview", env) as lease:
        new = lease.publish(8101, 8866)
        assert new.launch_id != old.launch_id
    assert not path.exists()


def test_port_reservation_and_fallback():
    with instances.reserve_port(None, 0, "--port") as first:
        port = first.getsockname()[1]
        with instances.reserve_port(None, port, "--port") as second:
            assert second.getsockname()[1] != port
        with pytest.raises(RuntimeError, match="unavailable"):
            with instances.reserve_port(port, port, "--port"):
                pass
    with instances.reserve_port(port, port, "--port"):
        pass


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_explicit_port_validation(port):
    with pytest.raises(ValueError, match="between"):
        with instances.reserve_port(port, 8000, "--port"):
            pass


def test_api_and_gateway_reservations_are_distinct():
    with instances.reserve_ports(None, None) as (api, gateway):
        assert api.getsockname()[1] != gateway.getsockname()[1]
    with pytest.raises(ValueError, match="different"):
        with instances.reserve_ports(8100, 8100):
            pass


def test_shared_storage_lock_rejects_alias_paths(tmp_path):
    folder = tmp_path / "state"
    folder.mkdir()
    (tmp_path / "alias").symlink_to(folder, target_is_directory=True)
    with instances.lock_storage({"MINDWEFT_THREAD_DB_PATH": str(folder / "threads.db")}):
        with pytest.raises(RuntimeError, match="in use"):
            with instances.lock_storage(
                {"MINIGENT_THREAD_DB_PATH": str(tmp_path / "alias" / "threads.db")}
            ):
                pass
    assert not (folder / "threads.db").exists()


def test_registry_verifies_identity_and_rejects_stale_port(tmp_path, monkeypatch):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    with instances.InstanceLease("preview", env) as lease:
        record = lease.publish(8100, 8865)

        def client(identity):
            return httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=identity))
            )

        matching = client({"name": "preview", "launch_id": record.launch_id})
        stale = client({"name": "preview", "launch_id": "a" * 32})
        monkeypatch.setattr(instances.httpx, "Client", Mock(side_effect=[matching, stale]))
        assert instances.resolve_instance("preview", env) == record
        with pytest.raises(RuntimeError, match="stale"):
            instances.resolve_instance("preview", env)


def test_invalid_registry_cannot_redirect_to_external_server(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    with instances.InstanceLease("preview", env) as lease:
        lease.publish(8100, 8865)
        path = lease.root / "preview.json"
        data = json.loads(path.read_text())
        data["api_url"] = "https://example.com"
        path.write_text(json.dumps(data))
        with pytest.raises(RuntimeError, match="No valid"):
            instances.resolve_instance("preview", env)


def test_client_routes_by_instance_and_guards_requests(monkeypatch):
    record = instances.InstanceRecord(
        "preview", "a" * 32, "http://127.0.0.1:8123", "http://127.0.0.1:8870", 123, "test"
    )
    monkeypatch.setattr(instances, "resolve_instance", Mock(return_value=record))
    from mindweft_workspace import local_connection
    from mindweft_workspace.local_credentials import LocalCredential

    monkeypatch.setattr(
        local_connection,
        "instance_credential",
        lambda record: LocalCredential(record.launch_id, "x" * 43),
    )
    monkeypatch.setattr(application, "build_client", Mock())
    dispatch = Mock(return_value=0)
    monkeypatch.setattr(application, "dispatch_command", dispatch)
    assert application.main(["--instance", "preview", "chat", "hello"]) == 0
    args, _, config, _ = dispatch.call_args.args
    assert config.base_url == record.api_url
    assert config.extra_headers["X-Mindweft-Launch-Id"] == record.launch_id
    first = state_scope_key(record.api_url, args)
    assert first == state_scope_key("http://127.0.0.1:9999", args)
    args.instance = "daily"
    assert first != state_scope_key(record.api_url, args)


def test_client_refuses_explicit_url_with_instance():
    with pytest.raises(SystemExit):
        application.main(
            ["--instance", "preview", "--base-url", "http://127.0.0.1:8000", "chat", "hello"]
        )


def test_client_never_falls_back_when_named_instance_missing(monkeypatch):
    monkeypatch.setattr(instances, "resolve_instance", Mock(side_effect=RuntimeError("stale")))
    client = Mock()
    monkeypatch.setattr(application, "build_client", client)
    assert application.main(["--instance", "preview", "chat", "hello"]) == 2
    client.assert_not_called()


def test_parser_accepts_instance_before_or_after_code():
    parser = build_parser()
    assert parser.parse_args(["--instance", "preview", "code", "."]).instance == "preview"
    assert parser.parse_args(["code", ".", "--instance", "preview"]).instance == "preview"
    assert parser.parse_args(["code"]).port is None


def test_api_identity_and_stale_launch_guard(monkeypatch):
    from app.main import create_app

    monkeypatch.setenv("MINDWEFT_LOCAL_INSTANCE_NAME", "preview")
    monkeypatch.setenv("MINDWEFT_LOCAL_LAUNCH_ID", "a" * 32)
    monkeypatch.setenv("MINDWEFT_LOCAL_INSTANCE_VERSION", "test")
    with TestClient(create_app()) as client:
        response = client.get("/local-instance")
        assert response.json() == {
            "name": "preview",
            "launch_id": "a" * 32,
            "version": "test",
            "workspace_access": "read-only",
        }
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/threads", headers={"X-Mindweft-Launch-Id": "stale"}).status_code == 409
        # Knowing a launch ID does not grant authentication.
        assert (
            client.post("/threads", headers={"X-Mindweft-Launch-Id": "a" * 32}).status_code == 401
        )


def test_api_without_instance_does_not_advertise_identity(monkeypatch):
    from app.main import create_app

    monkeypatch.delenv("MINDWEFT_LOCAL_INSTANCE_NAME", raising=False)
    monkeypatch.delenv("MINDWEFT_LOCAL_LAUNCH_ID", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/local-instance").status_code == 404


def test_shared_oauth_does_not_hold_lifetime_lock(tmp_path):
    oauth = {"MINDWEFT_OAUTH_STORE_PATH": str(tmp_path / "oauth.json")}
    # Even an older instance's OAuth lifetime lock must not block deliberate sharing.
    with instances.lock_storage(oauth):
        with instances.lock_storage(oauth, shared_oauth=True) as descriptors:
            assert descriptors == ()
        with pytest.raises(RuntimeError, match="OAUTH_STORE_PATH"):
            with instances.lock_storage(oauth):
                pass
    assert not (tmp_path / "oauth.json").exists()


def test_shared_oauth_keeps_conversation_locks(tmp_path):
    env = {
        "MINDWEFT_OAUTH_STORE_PATH": str(tmp_path / "oauth.json"),
        "MINDWEFT_THREAD_DB_PATH": str(tmp_path / "threads.db"),
    }
    with instances.lock_storage(env, shared_oauth=True):
        with pytest.raises(RuntimeError, match="THREAD_DB_PATH"):
            with instances.lock_storage(env, shared_oauth=True):
                pass
