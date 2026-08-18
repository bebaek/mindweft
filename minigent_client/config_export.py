from __future__ import annotations

import json
import urllib.parse
from typing import Any

from minigent_client.config_masking import mask_secrets


def export_unified_config_from_server(server_config: dict[str, Any]) -> dict[str, object]:
    export: dict[str, object] = {
        "profile": "exported",
        "_comments": [
            "Generated from a running Mindweft server via /config.",
            "This is a best-effort export from public server config output.",
            "Secrets and original source files are not recoverable; set API keys in your environment.",
        ],
    }
    llm = server_config.get("llm")
    if isinstance(llm, dict):
        llm_export = _export_llm_config(llm)
        if llm_export:
            export["llm"] = llm_export
    quality = server_config.get("quality")
    if isinstance(quality, dict):
        quality_export = _export_quality_config(quality)
        if quality_export:
            export["quality"] = quality_export
    mcp_servers = server_config.get("mcp_servers")
    if isinstance(mcp_servers, list) and mcp_servers:
        export["mcp"] = {"servers": [_export_mcp_server(server) for server in mcp_servers]}
    agent_backend = server_config.get("agent_backend")
    if isinstance(agent_backend, dict) and agent_backend.get("mcp_broker_enabled") is True:
        mcp_export = export.setdefault("mcp", {})
        if isinstance(mcp_export, dict):
            mcp_export["broker_enabled"] = True
    detailed_export = server_config.get("unified_config_export")
    if isinstance(detailed_export, dict):
        _merge_export_details(export, detailed_export)
    coding_export = export.get("coding")
    if isinstance(coding_export, dict):
        coding_export.pop("mcp_servers_file", None)
    if "tenant_execution_configs" in export:
        mcp_export = export.get("mcp")
        if isinstance(mcp_export, dict):
            mcp_export.pop("servers", None)
    _unify_coding_mcp_server_config(export)
    pruned_export = _prune_export(export)
    return pruned_export if isinstance(pruned_export, dict) else export


def _prune_export(export: dict[str, object]) -> dict[str, object]:
    pruned = _prune_empty_values(export)
    if not isinstance(pruned, dict):
        return export
    pruned_export: dict[str, object] = dict(pruned)
    if "_comments" in export:
        pruned_export["_comments"] = export["_comments"]
    return pruned_export


def _append_export_comment(export: dict[str, object], comment: str) -> None:
    comments = export.setdefault("_comments", [])
    if isinstance(comments, list) and comment not in comments:
        comments.append(comment)


def _export_has_coding_gateway_mcp_urls(export: dict[str, object]) -> bool:
    if isinstance(export.get("coding"), dict):
        return False
    tenant_configs = export.get("tenant_execution_configs")
    if not isinstance(tenant_configs, dict):
        return False
    for tenant in tenant_configs.values():
        if not isinstance(tenant, dict):
            continue
        tools = tenant.get("tools")
        if not isinstance(tools, dict):
            continue
        mcp_servers = tools.get("mcp_servers", tools.get("mcpServers"))
        if not isinstance(mcp_servers, list):
            continue
        for server in mcp_servers:
            if not isinstance(server, dict):
                continue
            url = server.get("url")
            if isinstance(url, str) and _looks_like_local_coding_gateway_url(url):
                return True
    return False


def _looks_like_local_coding_gateway_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return len(parts) >= 2 and parts[-2] == "mcp"


def _prune_empty_values(value: object) -> object:
    if isinstance(value, dict):
        pruned: dict[object, object] = {}
        for key, item in value.items():
            pruned_item = _prune_empty_values(item)
            if pruned_item is None:
                continue
            if pruned_item == {} or pruned_item == []:
                continue
            pruned[key] = pruned_item
        return pruned
    if isinstance(value, list):
        return [item for item in (_prune_empty_values(item) for item in value) if item is not None]
    if value == "None":
        return None
    return value


def _merge_export_details(export: dict[str, object], details: dict[str, Any]) -> None:
    for key, value in details.items():
        existing = export.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            existing.update(value)
        else:
            export[key] = value


def _unify_coding_mcp_server_config(export: dict[str, object]) -> None:
    """Make coding.mcp_server_specs the canonical MCP server source.

    Older/public server exports can contain the same logical MCP server in both
    coding.mcp_server_specs and tenant tools.mcp_servers.  Collapse those split
    entries into the coding spec and remove the derived tenant-side projection.
    """

    coding = export.get("coding")
    if not isinstance(coding, dict):
        return
    coding_specs = coding.get("mcp_server_specs")
    if not isinstance(coding_specs, list):
        return
    tenant_servers = _tenant_mcp_servers_by_name(export)
    for spec in coding_specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        if not isinstance(name, str):
            continue
        for tenant_server in tenant_servers.get(name, []):
            _merge_split_mcp_server_spec(export, spec, tenant_server)
    _remove_tenant_mcp_server_entries(export)


def _tenant_mcp_servers_by_name(export: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    servers_by_name: dict[str, list[dict[str, object]]] = {}
    tenant_configs = export.get("tenant_execution_configs")
    if not isinstance(tenant_configs, dict):
        return servers_by_name
    for tenant in tenant_configs.values():
        if not isinstance(tenant, dict):
            continue
        tools = tenant.get("tools")
        if not isinstance(tools, dict):
            continue
        servers = tools.get("mcp_servers", tools.get("mcpServers"))
        if not isinstance(servers, list):
            continue
        for server in servers:
            if not isinstance(server, dict):
                continue
            name = server.get("name")
            if isinstance(name, str):
                servers_by_name.setdefault(name, []).append(server)
    return servers_by_name


def _merge_split_mcp_server_spec(
    export: dict[str, object],
    coding_spec: dict[object, object],
    tenant_server: dict[str, object],
) -> None:
    merged_tools, lossy = _merge_allowed_tools(
        _string_list(coding_spec.get("allowed_tools")),
        _string_list(tenant_server.get("allowed_tools")),
    )
    if merged_tools is not None:
        coding_spec["allowed_tools"] = merged_tools
    if lossy:
        _append_export_comment(
            export,
            f"MCP server {coding_spec.get('name')!r} had different coding and tenant allowed_tools; exported their intersection.",
        )
    if "path_policy" not in coding_spec and isinstance(tenant_server.get("path_policy"), dict):
        coding_spec["path_policy"] = tenant_server["path_policy"]
    if "result_redaction" not in coding_spec and isinstance(
        tenant_server.get("result_redaction"), dict
    ):
        coding_spec["result_redaction"] = tenant_server["result_redaction"]


def _merge_allowed_tools(
    coding_tools: list[str] | None,
    tenant_tools: list[str] | None,
) -> tuple[list[str] | None, bool]:
    if coding_tools is None:
        return (tenant_tools, False)
    if tenant_tools is None:
        return (coding_tools, False)
    if set(coding_tools) == set(tenant_tools):
        return (coding_tools, False)
    tenant_tool_set = set(tenant_tools)
    return ([tool for tool in coding_tools if tool in tenant_tool_set], True)


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _remove_tenant_mcp_server_entries(export: dict[str, object]) -> None:
    tenant_configs = export.get("tenant_execution_configs")
    if not isinstance(tenant_configs, dict):
        return
    for tenant in tenant_configs.values():
        if not isinstance(tenant, dict):
            continue
        tools = tenant.get("tools")
        if not isinstance(tools, dict):
            continue
        tools.pop("mcp_servers", None)
        tools.pop("mcpServers", None)


def _export_llm_config(llm: dict[str, Any]) -> dict[str, object]:
    provider = llm.get("provider")
    model = llm.get("model")
    base_url = llm.get("base_url")
    exported: dict[str, object] = {}
    if isinstance(provider, str) and provider:
        exported["provider"] = provider
        api_key_env = _api_key_env_for_provider(provider)
        if api_key_env:
            exported["api_key_env"] = api_key_env
    if isinstance(model, str) and model:
        exported["model"] = model
    if isinstance(base_url, str) and base_url:
        exported["base_url"] = base_url
    return exported


def _api_key_env_for_provider(provider: str) -> str | None:
    provider = provider.lower()
    if provider == "openai":
        return "OPENAI_API_KEY"
    if provider == "openrouter":
        return "OPENROUTER_API_KEY"
    if provider in {"google", "gemini", "google-generative-ai"}:
        return "GEMINI_API_KEY"
    return None


def _export_quality_config(quality: dict[str, Any]) -> dict[str, object]:
    exported = _export_public_dict(
        quality,
        allowed={
            "enabled",
            "provider",
            "model",
            "base_url",
            "mode",
            "timeout",
            "max_payload_chars",
        },
    )
    defaults: dict[str, object] = {
        "enabled": False,
        "mode": "critique_draft",
        "provider": "mock",
        "timeout": 30.0,
        "max_payload_chars": 6000,
    }
    return {key: value for key, value in exported.items() if defaults.get(key) != value}


def _export_public_dict(value: dict[str, Any], *, allowed: set[str]) -> dict[str, object]:
    return {
        key: item
        for key, item in value.items()
        if key in allowed and isinstance(item, str | int | float | bool)
    }


def _export_mcp_server(server: object) -> dict[str, object]:
    if not isinstance(server, dict):
        return {"name": "unknown", "url": ""}
    exported: dict[str, object] = {}
    name = server.get("name")
    url = server.get("url")
    if isinstance(name, str):
        exported["name"] = name
    if isinstance(url, str):
        exported["url"] = url
    headers = server.get("headers")
    if isinstance(headers, dict):
        exported["headers"] = mask_secrets(headers)
    return exported


def render_unified_config_toml(export: dict[str, object]) -> str:
    lines: list[str] = []
    comments = export.get("_comments")
    if isinstance(comments, list):
        for comment in comments:
            lines.append(f"# {comment}")
        lines.append("")
    profile = export.get("profile")
    if isinstance(profile, str):
        lines.append(f"profile = {_toml_value(profile)}")
        lines.append("")
    for section in (
        "app",
        "auth",
        "oauth",
        "llm",
        "image_input",
        "coding",
        "mcp",
        "voice",
        "quality",
        "logging",
    ):
        value = export.get(section)
        if isinstance(value, dict) and value:
            _append_toml_table(lines, [section], value)
    for key in ("peer_agents", "tenant_execution_configs", "runtime"):
        value = export.get(key)
        if isinstance(value, dict):
            _append_toml_table(lines, [key], value)
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            for item in value:
                _append_toml_array_table(lines, [key], item)
        elif value is not None:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _append_toml_table(lines: list[str], path: list[str], table: dict[object, object]) -> None:
    nested_tables: list[tuple[str, dict[object, object]]] = []
    array_tables: list[tuple[str, list[object]]] = []
    scalar_lines: list[str] = []
    for key, value in table.items():
        key_str = str(key)
        if isinstance(value, dict):
            nested_tables.append((key_str, value))
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            array_tables.append((key_str, value))
        else:
            scalar_lines.append(f"{_toml_key(key_str)} = {_toml_value(value)}")
    if scalar_lines:
        lines.append("[" + ".".join(_toml_key(str(part)) for part in path) + "]")
        lines.extend(scalar_lines)
        lines.append("")
    for key, value in nested_tables:
        _append_toml_table(lines, [*path, key], value)
    for key, values in array_tables:
        for item in values:
            if isinstance(item, dict):
                _append_toml_array_table(lines, [*path, key], item)


def _append_toml_array_table(
    lines: list[str],
    path: list[str],
    table: dict[object, object],
) -> None:
    lines.append("[[" + ".".join(_toml_key(str(part)) for part in path) + "]]")
    array_tables: list[tuple[str, list[object]]] = []
    for key, value in table.items():
        key_str = str(key)
        if isinstance(value, dict):
            lines.append(f"{_toml_key(key_str)} = {_toml_value(value)}")
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            array_tables.append((key_str, value))
        else:
            lines.append(f"{_toml_key(key_str)} = {_toml_value(value)}")
    lines.append("")
    for key, values in array_tables:
        for item in values:
            if isinstance(item, dict):
                _append_toml_array_table(lines, [*path, key], item)


def _toml_value(value: object, *, multiline_lists: bool = True) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value):
            rendered = ",\n  ".join(_toml_inline_table(item) for item in value)
            if multiline_lists:
                return f"[\n  {rendered},\n]"
            return "[" + ", ".join(_toml_inline_table(item) for item in value) + "]"
        return "[" + ", ".join(_toml_value(item, multiline_lists=False) for item in value) + "]"
    if isinstance(value, dict):
        return _toml_inline_table(value)
    return json.dumps(str(value))


def _toml_inline_table(value: dict[object, object]) -> str:
    parts = [
        f"{_toml_key(str(key))} = {_toml_value(item, multiline_lists=False)}"
        for key, item in value.items()
    ]
    return "{ " + ", ".join(parts) + " }"


def _toml_key(key: str) -> str:
    return key if key.replace("_", "").replace("-", "").isalnum() else json.dumps(key)
