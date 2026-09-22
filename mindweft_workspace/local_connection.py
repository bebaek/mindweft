"""Trusted local client credential discovery and browser handoff."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode

import httpx

from mindweft_workspace.instances import InstanceRecord, registry_dir, validate_name
from mindweft_workspace.local_credentials import LocalCredential, read_credential


def credential_directory(root: Path, name: str, role: str = "api") -> Path:
    validate_name(name)
    if role not in {"api", "gateway"}:
        raise ValueError("Invalid credential role")
    # Canonicalize only the selected state root, not the private descendants.
    return root.resolve() / ".credentials" / name / role


def instance_credential(record: InstanceRecord) -> LocalCredential:
    return read_credential(
        credential_directory(registry_dir(), record.name), launch_id=record.launch_id
    )


def validate_local_url(url: str) -> None:
    if re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", url) is None:
        raise RuntimeError("Refusing to send local credential to a non-loopback origin")
    if not 1 <= int(url.rsplit(":", 1)[1]) <= 65535:
        raise RuntimeError("Invalid local port")


def browser_url(record: InstanceRecord, credential: LocalCredential) -> str:
    validate_local_url(record.api_url)
    if credential.launch_id != record.launch_id:
        raise RuntimeError("Local instance identity changed; reconnect")
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            response = client.post(
                record.api_url + "/auth/session/ticket",
                headers={
                    "Authorization": f"Bearer {credential.token}",
                    "X-Mindweft-Launch-Id": record.launch_id,
                },
            )
            response.raise_for_status()
            ticket = response.json()["ticket"]
            if not isinstance(ticket, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket) is None:
                raise ValueError
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise RuntimeError(
            "Could not create browser session; restart or reopen the local instance"
        ) from None
    return record.api_url + "/console/#" + urlencode({"local_ticket": ticket})
