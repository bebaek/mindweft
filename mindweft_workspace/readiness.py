"""Bounded readiness for the simple coding launcher (not a provider credential probe)."""

from __future__ import annotations

import subprocess
import time

import httpx

from mindweft_mcp.protocol import DEFAULT_MCP_PROTOCOL_VERSION, MCP_PROTOCOL_VERSION_META_KEY
from mindweft_workspace.mcp_specs import CodingMCPServerSpec


def mcp_payload(method: str, **params: object) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            **params,
            "_meta": {
                MCP_PROTOCOL_VERSION_META_KEY: DEFAULT_MCP_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": "mindweft-code", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def wait_for_code_ready(
    processes: list[subprocess.Popen[str]],
    *,
    api_port: int,
    specs: list[CodingMCPServerSpec],
    timeout: float = 60,
    expected_identity: dict[str, str] | None = None,
    auth_headers: dict[str, str] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    pending = "API"
    headers = {"X-Mindweft-User-Id": "demo-user", "X-Mindweft-Tenant-Id": "demo-tenant"}
    if auth_headers is not None:
        headers = auth_headers
    # Local probes must not route through an inherited HTTP proxy.
    with httpx.Client(trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(
                        "A workspace process exited before readiness. Check startup output above."
                    )
            try:
                pending = "API readiness"
                response = client.get(f"http://127.0.0.1:{api_port}/health/ready", headers=headers)
                response.raise_for_status()
                if response.json().get("status") != "ready":
                    raise ValueError("not ready")
                if expected_identity is not None:
                    pending = "local instance identity"
                    identity_response = client.get(f"http://127.0.0.1:{api_port}/local-instance")
                    identity_response.raise_for_status()
                    identity = identity_response.json()
                    if any(identity.get(key) != value for key, value in expected_identity.items()):
                        raise ValueError("instance identity mismatch")
                pending = "packaged console"
                client.get(f"http://127.0.0.1:{api_port}/console/").raise_for_status()
                pending = "inspect execution profile"
                response = client.get(
                    f"http://127.0.0.1:{api_port}/execution-options", headers=headers
                )
                response.raise_for_status()
                profiles = response.json()["capability_profiles"]
                if profiles["default"] not in {"inspect", "shared:inspect"}:
                    raise ValueError("inspect profile missing")
                for spec in specs:
                    pending = f"{spec.name} tool discovery"
                    response = client.post(
                        spec.url,
                        json=mcp_payload("tools/list"),
                        headers={
                            **spec.headers,
                            "MCP-Protocol-Version": DEFAULT_MCP_PROTOCOL_VERSION,
                        },
                    )
                    response.raise_for_status()
                    tools = {item["name"] for item in response.json()["result"]["tools"]}
                    if tools != set(spec.allowed_tools or []):
                        raise ValueError("unexpected or missing tools")
                # Do not announce readiness if a child died during the probes.
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError("A workspace process exited during readiness checks.")
                return
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                # Do not print remote response bodies: they may contain credentials.
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    raise RuntimeError(
        f"Workspace did not become ready within {timeout:g}s ({pending}). Check startup output and port settings."
    )
