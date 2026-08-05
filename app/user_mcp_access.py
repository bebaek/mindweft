"""Shared redacted user MCP access views for REST and MCP clients."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.models import Principal
from app.user_execution import effective_execution_catalog


def _store_or_raise(app: Any) -> Any:
    store = getattr(app.state, "admin_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="User execution config storage is not configured"
        )
    return store


def get_user_execution_status(app: Any, principal: Principal) -> dict[str, object]:
    store = _store_or_raise(app)
    record = store.get_user_execution_config(principal.tenant_id, principal.user_id)
    credentials_encrypted = bool(getattr(store, "user_execution_credentials_encrypted", False))
    findings: list[dict[str, str]] = []
    if record is None:
        findings.append(
            {
                "code": "user_execution.unconfigured",
                "severity": "info",
                "message": "No personal execution configuration is stored.",
                "remediation": "Create a personal execution configuration if needed.",
            }
        )
    if not credentials_encrypted:
        findings.append(
            {
                "code": "user_execution.credentials_unavailable",
                "severity": "warning",
                "message": "Encrypted personal MCP credential storage is unavailable.",
                "remediation": "Configure encrypted admin storage before using credential references.",
            }
        )
    counts = {"skills": 0, "mcp_servers": 0, "capability_profiles": 0, "agents": 0}
    if record is not None:
        for key in counts:
            collection = record.config.get(key, {})
            if isinstance(collection, dict) and isinstance(collection.get("items"), list):
                counts[key] = len(collection["items"])
    execution = app.state.execution_resolver.resolve(principal.tenant_id)
    catalog = effective_execution_catalog(
        execution.config,
        store,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
    )
    return {
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "endpoint_path": "/user-mcp",
        "execution_configured": record is not None,
        "execution_config_version": record.version if record is not None else None,
        "encrypted_credentials_available": credentials_encrypted,
        "personal_mcp_servers_allowed": catalog.personal_mcp_servers_allowed,
        **counts,
        "findings": findings,
    }


def _server_summary(server: Any, *, source: str) -> dict[str, object]:
    return {
        "id": server.id if source == "user" else server.name,
        "name": server.name,
        "source": source,
        "allowed_tools": list(server.allowed_tools) if server.allowed_tools is not None else None,
        "credential_configured": bool(source == "user" and getattr(server, "credential_ref", None)),
    }


def list_user_mcp_access(app: Any, principal: Principal) -> dict[str, object]:
    store = _store_or_raise(app)
    execution = app.state.execution_resolver.resolve(principal.tenant_id)
    catalog = effective_execution_catalog(
        execution.config,
        store,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
    )
    personal = [
        _server_summary(server, source="user")
        for server in (catalog.user_config.mcp_servers.items if catalog.user_config else [])
    ]
    shared_servers = list(catalog.tenant_config.tools.mcp_servers)
    item_ids = store.effective_subject_mcp_server_catalog_item_ids(
        principal.tenant_id, principal.user_id
    )
    catalog_items = getattr(app.state.admin_store_settings, "mcp_server_catalog", ())
    if item_ids is not None:
        allowed_ids = set(item_ids)
        allowed_names = {
            item.server.get("name")
            for item in catalog_items
            if item.id in allowed_ids and isinstance(item.server.get("name"), str)
        }
        shared_servers = [server for server in shared_servers if server.name in allowed_names]
    return {
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "endpoint_path": "/user-mcp",
        "personal_mcp_servers_allowed": catalog.personal_mcp_servers_allowed,
        "personal_servers": personal,
        "shared_servers": [_server_summary(server, source="shared") for server in shared_servers],
    }
