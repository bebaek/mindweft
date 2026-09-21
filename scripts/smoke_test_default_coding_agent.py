#!/usr/bin/env python3
"""Installed-wheel trusted coding agent: edit, test, CLI agent selection, persistence."""

from __future__ import annotations

import os
import shlex
import signal
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


def main() -> int:
    executable = str(Path(sys.executable).with_name("mindweft"))
    with tempfile.TemporaryDirectory(prefix="mindweft-trusted-smoke-") as directory:
        root = Path(directory).resolve()
        workspace = root / "project"
        workspace.mkdir()
        env = {
            key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT") if key in os.environ
        }
        env.update(
            HOME=str(root / "home"),
            XDG_STATE_HOME=str(root / "state"),
            XDG_CONFIG_HOME=str(root / "config"),
        )
        registry = root / "state" / "mindweft" / "instances"
        log = root / "startup.log"
        with log.open("w") as output:
            process = subprocess.Popen(
                [
                    executable,
                    "code",
                    str(workspace),
                    "--instance",
                    "trusted",
                    "--trust-workspace",
                    "--demo",
                    "--no-open",
                ],
                cwd=root,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 60
            while not (registry / "trusted.json").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(log.read_text())
                time.sleep(0.1)
            record = InstanceRecord.read(registry / "trusted.json")
            credentials = {
                role: read_credential(
                    credential_directory(registry, "trusted", role), launch_id=record.launch_id
                )
                for role in ("api", "gateway")
            }
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:

                def api(method, path, **kwargs):
                    response = client.request(
                        method,
                        record.api_url + path,
                        headers={"Authorization": f"Bearer {credentials['api'].token}"},
                        **kwargs,
                    )
                    response.raise_for_status()
                    return response.json()

                def tool(server, name, arguments):
                    response = client.post(
                        record.gateway_url + "/mcp/" + server,
                        headers={"Authorization": f"Bearer {credentials['gateway'].token}"},
                        json=mcp_payload("tools/call", name=name, arguments=arguments),
                    )
                    response.raise_for_status()
                    result = response.json()
                    assert not result.get("error") and not result.get("result", {}).get(
                        "isError"
                    ), result
                    return result["result"]

                options = api("GET", "/execution-options")
                assert options["agents"]["default"] in {"coding", "shared:coding"}
                assert options["capability_profiles"]["default"] in {"coding", "shared:coding"}
                target = workspace / "test_answer.py"
                tool(
                    "fs-workspace",
                    "write_file",
                    {
                        "path": str(target),
                        "content": "import unittest\nclass TestAnswer(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(1, 2)\n",
                    },
                )
                tool(
                    "fs-workspace",
                    "edit_file",
                    {
                        "path": str(target),
                        "old_text": "assertEqual(1, 2)",
                        "new_text": "assertEqual(2, 2)",
                    },
                )
                result = tool(
                    "shell-workspace",
                    "run_command",
                    {
                        "command": f"{shlex.quote(sys.executable)} -m unittest -v",
                        "cwd": str(workspace),
                    },
                )
                assert result["structuredContent"]["exit_code"] == 0, result
                # Local auth/provider configuration must not be inherited by commands.
                command = f'{shlex.quote(sys.executable)} -c \'import os; assert not any(k.startswith(("MINDWEFT_", "MINIGENT_")) for k in os.environ)\''
                assert (
                    tool("shell-workspace", "run_command", {"command": command})[
                        "structuredContent"
                    ]["exit_code"]
                    == 0
                )
                assert "assertEqual(2, 2)" in target.read_text()
                chat = subprocess.run(
                    [executable, "--instance", "trusted", "chat", "--agent", "coding", "hello"],
                    env=env,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert chat.returncode == 0, chat.stderr
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=20)
        assert process.returncode == 0, log.read_text()
        assert (registry / "trusted" / "personal-setup.db").exists()
    print(
        "Installed default coding agent passed trust, edit, shell test, secret-env exclusion, CLI agent selection, and shutdown smoke."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
