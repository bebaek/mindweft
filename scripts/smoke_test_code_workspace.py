#!/usr/bin/env python3
"""Installed-wheel coding smoke test with no Node/npm or external executables on PATH."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from mindweft_workspace.instances import InstanceRecord
from mindweft_workspace.local_connection import credential_directory
from mindweft_workspace.local_credentials import read_credential
from mindweft_workspace.readiness import mcp_payload


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def assert_denied(response: httpx.Response) -> None:
    payload = response.json()
    assert response.status_code == 403 or (
        response.status_code == 200
        and (payload.get("error") or payload.get("result", {}).get("isError"))
    ), payload


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mindweft-code-smoke-") as directory:
        root = Path(directory)
        workspace = root / "project with spaces"
        workspace.mkdir()
        second_workspace = root / "second project"
        second_workspace.mkdir()
        (second_workspace / "fixture.txt").write_text("second-root-fixture\n")
        (workspace / "fixture.txt").write_text("coding-smoke-fixture\n")
        # This is test data, not a credential file. The bridge must deny its path.
        (workspace / ".git").mkdir()
        (workspace / ".git" / "blocked.txt").write_text("denied-fixture")
        outside = root / "outside.txt"
        outside.write_text("outside-fixture")
        (workspace / "escaped-link").symlink_to(outside)
        (workspace / "hidden-link").symlink_to(workspace / ".git" / "blocked.txt")
        (workspace / "mindweft.toml").write_text("deliberately invalid repository config")
        api_port = unused_port()
        gateway_port = unused_port()
        while gateway_port == api_port:
            gateway_port = unused_port()
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "TMPDIR") if key in os.environ}
        empty_path = root / "empty-path"
        empty_path.mkdir()
        env["PATH"] = str(empty_path)
        assert shutil.which("node", path=env["PATH"]) is None
        assert shutil.which("npx", path=env["PATH"]) is None
        env.update(
            {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        command = [
            str(Path(sys.executable).with_name("mindweft")),
            "code",
            str(workspace),
            str(second_workspace),
            "--demo",
            "--no-open",
            "--port",
            str(api_port),
            "--gateway-port",
            str(gateway_port),
        ]
        log = root / "startup.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                command, env=env, cwd=workspace, stdout=output, stderr=subprocess.STDOUT
            )
        try:
            deadline = time.monotonic() + 90
            while "Ready:" not in log.read_text():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Coding launcher did not become ready:\n" + log.read_text())
                time.sleep(0.2)
            registry = root / "state" / "mindweft" / "instances"
            record = InstanceRecord.read(registry / "default.json")
            credentials = {
                role: read_credential(
                    credential_directory(registry, "default", role), launch_id=record.launch_id
                )
                for role in ("api", "gateway")
            }
            headers = {
                "Authorization": f"Bearer {credentials['api'].token}",
                "X-Mindweft-Launch-Id": record.launch_id,
            }
            gateway_headers = {
                "Authorization": f"Bearer {credentials['gateway'].token}",
                "X-Mindweft-Launch-Id": record.launch_id,
            }
            with httpx.Client(trust_env=False, timeout=10, follow_redirects=False) as anonymous:
                assert anonymous.get(record.api_url + "/threads").status_code == 401
                assert (
                    anonymous.get(
                        record.api_url + "/execution-options",
                        headers={
                            "X-Mindweft-User-Id": "demo-user",
                            "X-Mindweft-Tenant-Id": "demo-tenant",
                        },
                    ).status_code
                    == 401
                )
                assert (
                    anonymous.post(
                        record.gateway_url + "/mcp/fs-workspace", json=mcp_payload("tools/list")
                    ).status_code
                    == 401
                )
                assert (
                    anonymous.post(
                        record.gateway_url + "/mcp/fs-workspace",
                        headers=headers,
                        json=mcp_payload("tools/list"),
                    ).status_code
                    == 401
                )
                assert (
                    anonymous.get(record.api_url + "/threads", headers=gateway_headers).status_code
                    == 401
                )
            with httpx.Client(
                trust_env=False, timeout=10, follow_redirects=False, headers=headers
            ) as client:
                base = f"http://127.0.0.1:{api_port}"
                assert client.get(base + "/console/").status_code == 200
                response = client.get(base + "/execution-options")
                response.raise_for_status()
                options = response.json()
                assert len(options["capability_profiles"]["items"]) == 1
                for name, tool, arguments in (
                    ("fs-workspace", "read_file", {"path": str(workspace / "fixture.txt")}),
                    (
                        "text-workspace",
                        "read_text_file_lines",
                        {"path": str(workspace / "fixture.txt"), "start_line": 1, "end_line": 1},
                    ),
                ):
                    url = f"http://127.0.0.1:{gateway_port}/mcp/{name}"

                    def call(arguments, url=url, tool=tool):
                        return client.post(
                            url,
                            headers=gateway_headers,
                            json=mcp_payload("tools/call", name=tool, arguments=arguments),
                        )

                    if name == "fs-workspace":
                        listed = client.post(
                            url,
                            headers=gateway_headers,
                            json=mcp_payload(
                                "tools/call",
                                name="list_directory",
                                arguments={"path": str(workspace)},
                            ),
                        ).json()
                        assert "fixture.txt" in str(listed)
                        assert "hidden-link" not in str(listed) and "escaped-link" not in str(
                            listed
                        )
                    result = call(arguments).json()
                    assert not result.get("error") and not result["result"].get("isError"), result
                    assert "coding-smoke-fixture" in str(result)
                    second_result = call(
                        {**arguments, "path": str(second_workspace / "fixture.txt")}
                    ).json()
                    assert not second_result.get("error") and not second_result["result"].get(
                        "isError"
                    ), second_result
                    assert "second-root-fixture" in str(second_result)
                    for denied in (
                        outside,
                        workspace / ".git" / "blocked.txt",
                        workspace / "escaped-link",
                        workspace / "hidden-link",
                    ):
                        assert_denied(call({**arguments, "path": str(denied)}))
                    tools = client.post(
                        url, headers=gateway_headers, json=mcp_payload("tools/list")
                    ).json()["result"]["tools"]
                    assert not {"write_file", "edit_file", "run_command"} & {
                        t["name"] for t in tools
                    }
                    result = client.post(
                        url,
                        headers=gateway_headers,
                        json=mcp_payload(
                            "tools/call",
                            name="write_file",
                            arguments={"path": str(workspace / "unexpected"), "content": "no"},
                        ),
                    )
                    assert_denied(result)
                thread = client.post(base + "/threads").json()["thread_id"]
                client.post(
                    base + f"/threads/{thread}/messages", json={"content": "hello"}
                ).raise_for_status()
                client.post(base + f"/threads/{thread}/run").raise_for_status()
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError("Coding launcher did not shut down cleanly") from None
        assert process.returncode == 0, log.read_text()
        for port in (api_port, gateway_port):
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", port)) != 0, "orphaned service"
        assert not (workspace / "unexpected").exists()
        assert sorted(p.name for p in workspace.iterdir()) == [
            ".git",
            "escaped-link",
            "fixture.txt",
            "hidden-link",
            "mindweft.toml",
        ]
        assert (root / "state" / "mindweft" / "threads.db").is_file()
    print(
        "Installed coding workspace passed launch, console, tools, policy, chat, and shutdown smoke test."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
