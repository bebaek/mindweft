#!/usr/bin/env python3
"""Installed-wheel smoke: two isolated, discoverable coding instances with automatic ports."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from mindweft_workspace.local_connection import credential_directory
from mindweft_workspace.local_credentials import read_credential
from mindweft_workspace.readiness import mcp_payload


def main() -> int:
    executable = str(Path(sys.executable).with_name("mindweft"))
    with tempfile.TemporaryDirectory(prefix="mindweft-instances-") as directory:
        root = Path(directory)
        registry = root / "state" / "mindweft" / "instances"
        env = {key: os.environ[key] for key in ("TMPDIR", "SYSTEMROOT") if key in os.environ}
        empty_path = root / "bin"
        empty_path.mkdir()
        env.update(
            {
                "PATH": str(empty_path),
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        processes: dict[str, subprocess.Popen] = {}
        records = {}
        for name in ("daily", "preview"):
            workspace = root / name
            workspace.mkdir()
            (workspace / "fixture.txt").write_text(f"{name}-only")

        def stop(name):
            process = processes.pop(name)
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=20)
            assert process.returncode == 0
            assert not (registry / f"{name}.json").exists()

        def start(name):
            log_path = root / f"{name}.log"
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    [
                        executable,
                        "code",
                        str(root / name),
                        "--instance",
                        name,
                        "--demo",
                        "--read-only",
                        "--no-open",
                    ],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            processes[name] = process
            deadline = time.monotonic() + 60
            while not (registry / f"{name}.json").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(log_path.read_text())
                time.sleep(0.1)
            return json.loads((registry / f"{name}.json").read_text())

        def headers(name, role="api"):
            record = records[name]
            credential = read_credential(
                credential_directory(registry, name, role), launch_id=record["launch_id"]
            )
            return {
                "Authorization": f"Bearer {credential.token}",
                "X-Mindweft-Launch-Id": record["launch_id"],
            }

        try:
            records["daily"] = start("daily")
            records["preview"] = start("preview")
            all_urls = [
                record[key] for record in records.values() for key in ("api_url", "gateway_url")
            ]
            assert len(set(all_urls)) == 4
            listed = subprocess.run(
                [executable, "--json", "instances", "list"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            assert {
                entry["name"] for entry in json.loads(listed.stdout) if entry["status"] == "running"
            } == {"daily", "preview"}
            duplicate = subprocess.run(
                [
                    executable,
                    "code",
                    str(root / "preview"),
                    "--instance",
                    "preview",
                    "--demo",
                    "--read-only",
                    "--no-open",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert duplicate.returncode != 0 and "already running" in duplicate.stderr
            thread_ids = {}
            with httpx.Client(
                trust_env=False,
                timeout=10,
                follow_redirects=False,
            ) as client:

                def api_get(name, path):
                    response = client.get(records[name]["api_url"] + path, headers=headers(name))
                    response.raise_for_status()
                    return response

                for name, record in records.items():
                    assert client.get(record["api_url"] + "/threads").status_code == 401
                    assert (
                        client.post(
                            record["gateway_url"] + "/mcp/fs-workspace",
                            json=mcp_payload("tools/list"),
                        ).status_code
                        == 401
                    )
                    other = "preview" if name == "daily" else "daily"
                    # Use this launch ID to prove token isolation, not just stale-launch rejection.
                    wrong_token = {**headers(other), "X-Mindweft-Launch-Id": record["launch_id"]}
                    assert (
                        client.get(record["api_url"] + "/threads", headers=wrong_token).status_code
                        == 401
                    )
                    # Exercise the installed CLI's discovery, identity validation, and message routing.
                    subprocess.run(
                        [executable, "--instance", name, "chat", f"hello {name}"],
                        cwd=root,
                        env=env,
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=20,
                    )
                    threads = api_get(name, "/threads").json()["threads"]
                    assert len(threads) == 1
                    thread_ids[name] = threads[0]["thread_id"]
                    url = record["gateway_url"] + "/mcp/fs-workspace"
                    result = client.post(
                        url,
                        headers=headers(name, "gateway"),
                        json=mcp_payload(
                            "tools/call",
                            name="read_file",
                            arguments={"path": str(root / name / "fixture.txt")},
                        ),
                    ).json()
                    assert f"{name}-only" in str(result)
                    other = "preview" if name == "daily" else "daily"
                    denied = client.post(
                        url,
                        headers=headers(name, "gateway"),
                        json=mcp_payload(
                            "tools/call",
                            name="read_file",
                            arguments={"path": str(root / other / "fixture.txt")},
                        ),
                    ).json()
                    assert denied.get("error") or denied.get("result", {}).get("isError")
                assert thread_ids["daily"] != thread_ids["preview"]
                old_preview = records["preview"]
                old_headers = headers("preview")
                stop("preview")
                assert api_get("daily", "/health/ready").status_code == 200
                assert (
                    api_get("daily", "/threads").json()["threads"][0]["thread_id"]
                    == thread_ids["daily"]
                )
                records["preview"] = start("preview")
                assert records["preview"]["launch_id"] != old_preview["launch_id"]
                assert (
                    client.get(
                        records["preview"]["api_url"] + "/threads",
                        headers={
                            **old_headers,
                            "X-Mindweft-Launch-Id": records["preview"]["launch_id"],
                        },
                    ).status_code
                    == 401
                )
                assert (
                    api_get("preview", "/threads").json()["threads"][0]["thread_id"]
                    == thread_ids["preview"]
                )
                assert (
                    client.post(
                        records["preview"]["api_url"] + "/threads",
                        headers={"X-Mindweft-Launch-Id": old_preview["launch_id"]},
                    ).status_code
                    == 409
                )
                # A stale routing record must not result in a chat request or a fallback to daily.
                path = registry / "preview.json"
                current = path.read_text()
                path.write_text(
                    json.dumps({**records["preview"], "launch_id": old_preview["launch_id"]})
                )
                failed = subprocess.run(
                    [executable, "--instance", "preview", "chat", "must not send"],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                path.write_text(current)
                assert failed.returncode != 0 and "stale" in failed.stderr
                assert len(api_get("preview", "/threads").json()["threads"]) == 1
        finally:
            for name in list(processes):
                stop(name)
        assert (registry / "daily" / "threads.db").is_file()
        assert (registry / "preview" / "threads.db").is_file()
        assert not (root / "state" / "mindweft" / "threads.db").exists()
    print(
        "Installed multi-instance smoke passed: concurrent ports, isolated roots/state, CLI routing, stale rejection, restart, independent shutdown."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
