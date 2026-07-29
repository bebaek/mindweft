from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

import httpx

DEFAULT_CONTACTS_PORT = 8877
DEFAULT_API_PORT = 8878


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a sanitized live CardDAV -> MCP -> Minigent smoke test. "
            "Raw contact values are checked in memory but never printed."
        )
    )
    parser.add_argument("--contacts-port", type=int, default=DEFAULT_CONTACTS_PORT)
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--fake", action="store_true", help="Use built-in fake contacts.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    required = (
        "MINIGENT_CARDDAV_URL",
        "MINIGENT_CARDDAV_USERNAME",
        "MINIGENT_CARDDAV_PASSWORD",
    )
    if not args.fake and any(not os.environ.get(name) for name in required):
        print("Required CardDAV environment variables are missing.", file=sys.stderr)
        return 2

    contacts_url = f"http://127.0.0.1:{args.contacts_port}/mcp"
    api_url = f"http://127.0.0.1:{args.api_port}"
    tenant_id = "live-private-contacts"
    headers = {
        "X-Minigent-User-Id": "live-smoke-user",
        "X-Minigent-Tenant-Id": tenant_id,
        "X-Minigent-Admin": "false",
    }
    tenant_config = {
        tenant_id: {
            "llm": {"provider": "mock"},
            "tools": {
                "allowed_local_tools": [],
                "mcp_servers": [
                    {
                        "name": "private-contacts",
                        "url": contacts_url,
                        "headers": {},
                        "allowed_tools": [
                            "contacts_list",
                            "contacts_get",
                            "contacts_protect_text",
                        ],
                    }
                ],
            },
        }
    }
    env = {
        **os.environ,
        "MINIGENT_AUTH_MODE": "dev-headers",
        "MINIGENT_CONFIG_DISCOVERY": "disabled",
        "MINIGENT_LLM_PROVIDER": "mock",
        "MINIGENT_TENANT_EXECUTION_CONFIGS": json.dumps(tenant_config, separators=(",", ":")),
    }
    contact_env = dict(env)
    if args.fake:
        for name in required:
            contact_env.pop(name, None)
    processes: list[subprocess.Popen[str]] = []
    try:
        processes.append(
            _start_process(
                [
                    sys.executable,
                    "scripts/demo_private_contacts_mcp.py",
                    "--port",
                    str(args.contacts_port),
                ],
                contact_env,
            )
        )
        _wait_for_mcp(contacts_url, processes)
        processes.append(
            _start_process(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.api_port),
                ],
                env,
            )
        )
        _wait_for_api(api_url, headers, processes)
        _run_smoke(api_url, headers, reveal_failure=args.fake)
        return 0
    except Exception as exc:
        print(f"Live private-contacts smoke test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for process in reversed(processes):
            _stop_process(process)


def _start_process(command: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        text=True,
    )


def _wait_for_mcp(url: str, processes: list[subprocess.Popen[str]]) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "live-smoke", "version": "0.1"},
        },
    }
    _poll(lambda: httpx.post(url, json=payload, timeout=2), processes, "MCP server")


def _wait_for_api(
    base_url: str,
    headers: dict[str, str],
    processes: list[subprocess.Popen[str]],
) -> None:
    _poll(
        lambda: httpx.get(f"{base_url}/config", headers=headers, timeout=2),
        processes,
        "Minigent API",
    )


def _poll(
    request: Any,
    processes: list[subprocess.Popen[str]],
    label: str,
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError(f"A process exited while waiting for {label}")
        try:
            response = request()
            response.raise_for_status()
            return
        except httpx.HTTPError:
            time.sleep(0.25)
    raise RuntimeError(f"{label} did not become ready")


def _run_smoke(base_url: str, headers: dict[str, str], *, reveal_failure: bool = False) -> None:
    config = httpx.get(f"{base_url}/config", headers=headers, timeout=10)
    config.raise_for_status()
    tools = set(config.json().get("tools", config.json().get("tool_names", [])))
    if tools and "private-contacts.contacts_list" not in tools:
        raise RuntimeError("Private contacts tools were not available")

    created = httpx.post(f"{base_url}/threads", headers=headers, json={}, timeout=10)
    created.raise_for_status()
    thread_id = created.json()["thread_id"]

    list_reply = _run_tool(
        base_url,
        headers,
        thread_id,
        "private-contacts.contacts_list",
        {"limit": 1},
    )
    visible_list = _tool_reply_payload(list_reply, reveal_failure=reveal_failure)
    visible_name = visible_list["contacts"][0]["name"]

    protected_message = httpx.post(
        f"{base_url}/threads/{thread_id}/messages",
        headers=headers,
        json={"content": f"What is {visible_name}'s email?"},
        timeout=30,
    )
    protected_message.raise_for_status()
    if visible_name not in protected_message.json()["content"]:
        raise RuntimeError("Authenticated message response did not rehydrate the contact name")

    raw_context = _raw_context(base_url, headers, thread_id)
    raw_tool_results = [
        json.loads(message["content"])
        for message in raw_context["messages"]
        if message["role"] == "tool"
    ]
    contact_ref = raw_tool_results[0]["contacts"][0]["contact_ref"]

    get_reply = _run_tool(
        base_url,
        headers,
        thread_id,
        "private-contacts.contacts_get",
        {"contact_ref": contact_ref, "fields": ["emails", "phones"]},
    )
    visible_get = _tool_reply_payload(get_reply, reveal_failure=reveal_failure)
    visible_private_values = [
        visible_name,
        *visible_get.get("emails", []),
        *visible_get.get("phones", []),
    ]

    raw_text = json.dumps(_raw_context(base_url, headers, thread_id), ensure_ascii=True)
    if visible_name in raw_text or "{{pii:contact:" not in raw_text:
        raise RuntimeError("Known contact name was not masked in stored/model input")
    leaked = [value for value in visible_private_values if value and value in raw_text]
    if leaked:
        raise RuntimeError("Raw contact PII appeared in stored/model context")

    history = httpx.get(
        f"{base_url}/threads/{thread_id}/messages",
        headers=headers,
        timeout=10,
    )
    history.raise_for_status()
    history_text = json.dumps(history.json(), ensure_ascii=True)
    missing = [value for value in visible_private_values if value and value not in history_text]
    if missing:
        raise RuntimeError("Authorized history did not rehydrate private contact values")

    print("live_minigent_carddav_smoke=ok")
    print(f"private_values_verified={len(visible_private_values)}")
    print(f"stored_model_context_raw_pii_count={len(leaked)}")
    print(f"authorized_history_missing_private_values={len(missing)}")
    print("contact_ref_round_trip=ok")
    print("known_contact_input_masking=ok")


def _run_tool(
    base_url: str,
    headers: dict[str, str],
    thread_id: str,
    name: str,
    arguments: dict[str, Any],
) -> str:
    command = f"/tool {name} {json.dumps(arguments, separators=(',', ':'))}"
    posted = httpx.post(
        f"{base_url}/threads/{thread_id}/messages",
        headers=headers,
        json={"content": command},
        timeout=10,
    )
    posted.raise_for_status()
    run = httpx.post(
        f"{base_url}/threads/{thread_id}/run",
        headers=headers,
        timeout=60,
    )
    run.raise_for_status()
    return str(run.json()["reply"])


def _tool_reply_payload(reply: str, *, reveal_failure: bool = False) -> dict[str, Any]:
    prefix = "Tool result: "
    if reply.startswith("{"):
        raw_payload = reply
    elif reply.startswith(prefix):
        raw_payload = reply.removeprefix(prefix)
    else:
        if reply.startswith("Mock reply:"):
            category = "mock_reply"
        elif reply.startswith("Tool"):
            category = "other_tool_reply"
        elif not reply:
            category = "empty"
        else:
            category = "other"
        if reveal_failure:
            raise RuntimeError(f"Unexpected fake tool reply: {reply!r}")
        raise RuntimeError(f"Unexpected tool reply category: {category}; length={len(reply)}")
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        raise RuntimeError("Tool reply was not an object")
    return payload


def _raw_context(base_url: str, headers: dict[str, str], thread_id: str) -> dict[str, Any]:
    response = httpx.get(
        f"{base_url}/threads/{thread_id}/context/raw",
        headers=headers,
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Raw context response was not an object")
    return payload


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
