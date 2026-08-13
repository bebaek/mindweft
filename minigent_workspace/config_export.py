from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path
from typing import Any

from minigent_config.unified_config import (
    DEFAULT_CODING_DOTENV_FILE,
    DOTENV_FILE_ENV,
    load_unified_config_env,
    resolve_config_path,
)
from minigent_workspace import (
    environment,
    launch_commands,
    mcp_specs,
    runtime_plan,
    runtime_settings,
    scopes,
    tenant_config,
)


def export_local_coding_config(args: Namespace) -> dict[str, object]:
    """Resolve local coding runner config for orchestrator-side export."""

    env_file = None
    if not getattr(args, "no_coding_env_file", False):
        env_file = (
            getattr(args, "coding_env_file", None)
            or getattr(args, "env_file", None)
            or DEFAULT_CODING_DOTENV_FILE
        )
    env_path = Path(env_file).expanduser() if env_file is not None else None
    env, env_base_dir = load_coding_workspace_export_env(env_path)
    workspace_roots = scopes.resolve_workspace_roots(
        None,
        env.get("MINIGENT_CODING_WORKSPACES") or env.get("MINIGENT_CODING_WORKSPACE"),
    )
    bridge_host = env.get("MINIGENT_CODING_BRIDGE_HOST") or mcp_specs.DEFAULT_BRIDGE_HOST
    bridge_name = env.get("MINIGENT_CODING_BRIDGE_NAME") or runtime_settings.DEFAULT_BRIDGE_NAME
    bridge_port = int(env.get("MINIGENT_CODING_BRIDGE_PORT") or mcp_specs.DEFAULT_BRIDGE_PORT)
    bridge_url = f"http://{bridge_host}:{bridge_port}/mcp"
    gateway_enabled = mcp_specs.env_flag_enabled(env.get("MINIGENT_CODING_MCP_GATEWAY_ENABLED"))
    gateway_port = int(env.get("MINIGENT_CODING_MCP_GATEWAY_PORT") or bridge_port)
    gateway_path_prefix = (
        env.get("MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX")
        or mcp_specs.DEFAULT_MCP_GATEWAY_PATH_PREFIX
    )
    text_enabled = mcp_specs.env_flag_enabled(env.get("MINIGENT_CODING_TEXT_ENABLED"))
    text_bridge_name = (
        env.get("MINIGENT_CODING_TEXT_BRIDGE_NAME") or tenant_config.DEFAULT_TEXT_BRIDGE_NAME
    )
    text_bridge_port = int(
        env.get("MINIGENT_CODING_TEXT_BRIDGE_PORT") or tenant_config.DEFAULT_TEXT_BRIDGE_PORT
    )
    text_bridge_url = f"http://{bridge_host}:{text_bridge_port}/mcp"
    shell_enabled = mcp_specs.env_flag_enabled(env.get("MINIGENT_CODING_SHELL_ENABLED"))
    shell_bridge_name = (
        env.get("MINIGENT_CODING_SHELL_BRIDGE_NAME") or tenant_config.DEFAULT_SHELL_BRIDGE_NAME
    )
    shell_bridge_port = int(
        env.get("MINIGENT_CODING_SHELL_BRIDGE_PORT") or tenant_config.DEFAULT_SHELL_BRIDGE_PORT
    )
    shell_bridge_url = f"http://{bridge_host}:{shell_bridge_port}/mcp"
    tenant_id = env.get("MINIGENT_CODING_TENANT_ID") or runtime_plan.DEFAULT_TENANT_ID

    mcp_servers_file = mcp_specs.resolve_mcp_servers_file(None, env, base_dir=env_base_dir)
    raw_specs = env.get("MINIGENT_CODING_MCP_SERVER_SPECS", "").strip()
    if mcp_servers_file is not None:
        try:
            mcp_server_specs = _coding_mcp_specs_from_raw_json(
                mcp_servers_file.read_text(encoding="utf-8")
            )
        except OSError:
            mcp_server_specs = []
    elif raw_specs:
        mcp_server_specs = _coding_mcp_specs_from_raw_json(raw_specs)
    else:
        specs = launch_commands.build_builtin_mcp_server_specs(
            env,
            tenant_id,
            bridge_name=bridge_name,
            bridge_host=bridge_host,
            bridge_port=bridge_port,
            bridge_url=bridge_url,
            workspace_roots=workspace_roots,
            text_enabled=text_enabled,
            text_bridge_name=text_bridge_name,
            text_bridge_port=text_bridge_port,
            text_bridge_url=text_bridge_url,
            shell_enabled=shell_enabled,
            shell_bridge_name=shell_bridge_name,
            shell_bridge_port=shell_bridge_port,
            shell_bridge_url=shell_bridge_url,
        )
        mcp_server_specs = [_coding_mcp_server_spec_to_export_dict(spec) for spec in specs]

    coding: dict[str, object] = {
        "workspaces": [str(workspace) for workspace in workspace_roots],
        "mcp_server_specs": mcp_server_specs,
    }
    workspace_scope = env.get("MINIGENT_CODING_WORKSPACE_SCOPE", "").strip()
    if workspace_scope:
        coding["workspace_scope"] = workspace_scope
    default_workspace_scope = env.get("MINIGENT_CODING_DEFAULT_WORKSPACE_SCOPE", "").strip()
    if default_workspace_scope:
        coding["default_workspace_scope"] = default_workspace_scope
    workspace_scopes = _optional_json_object_env(env, "MINIGENT_CODING_WORKSPACE_SCOPES")
    if workspace_scopes is not None:
        coding["workspace_scopes"] = workspace_scopes
    if tenant_id:
        coding["tenant_id"] = tenant_id
    if gateway_enabled:
        coding["mcp_gateway_enabled"] = True
        coding["mcp_gateway_port"] = gateway_port
        coding["mcp_gateway_path_prefix"] = mcp_specs.normalize_path_prefix(gateway_path_prefix)
    if bridge_host != mcp_specs.DEFAULT_BRIDGE_HOST:
        coding["bridge_host"] = bridge_host
    app: dict[str, object] = {}
    thread_db_path = env.get("MINIGENT_THREAD_DB_PATH", "").strip()
    if thread_db_path:
        app["thread_db_path"] = thread_db_path
    max_iterations = _optional_int_env(env, "MINIGENT_MAX_ITERATIONS")
    if max_iterations is not None:
        app["max_iterations"] = max_iterations
    tool_timeout_seconds = _optional_float_env(env, "MINIGENT_TOOL_TIMEOUT_SECONDS")
    if tool_timeout_seconds is not None:
        app["tool_timeout_seconds"] = tool_timeout_seconds
    context_compaction_enabled = _optional_bool_env(env, "MINIGENT_CONTEXT_COMPACTION_ENABLED")
    if context_compaction_enabled is not None:
        app["context_compaction_enabled"] = context_compaction_enabled

    result: dict[str, object] = {"coding": coding}
    if app:
        result["app"] = app
    return result


def _optional_int_env(env: dict[str, str], key: str) -> int | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    return int(raw)


def _optional_float_env(env: dict[str, str], key: str) -> float | int | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    value = float(raw)
    return int(value) if value.is_integer() else value


def _optional_bool_env(env: dict[str, str], key: str) -> bool | None:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def _optional_json_object_env(env: dict[str, str], key: str) -> dict[str, object] | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def load_coding_workspace_export_env(env_path: Path | None) -> tuple[dict[str, str], Path]:
    """Load coding env for export without rereading a preloaded dotenv file.

    `sops exec-file` and similar tools may provide the dotenv as a one-shot file or FIFO.
    The main CLI has already loaded `--env-file` into `os.environ`, so avoid reading the same
    path again when `MINIGENT_DOTENV_FILE` points at it.
    """

    if env_path is None:
        base_dir = Path.cwd()
        env = dict(os.environ)
        config_env = load_unified_config_env(
            resolve_config_path(base_dir=base_dir, env=env), source_env=env
        )
        for key, value in config_env.items():
            env.setdefault(key, value)
        _apply_selected_file_env_values(env, base_dir=base_dir)
        return env, base_dir

    base_dir = env_path.resolve().parent if env_path.exists() else Path.cwd()
    if env_path.exists() and not _env_file_already_loaded(env_path):
        return environment.load_env_file(str(env_path)), base_dir

    env = dict(os.environ)
    config_env = load_unified_config_env(
        resolve_config_path(base_dir=base_dir, env=env), source_env=env
    )
    for key, value in config_env.items():
        env.setdefault(key, value)
    _apply_selected_file_env_values(env, base_dir=base_dir)
    return env, base_dir


def _apply_selected_file_env_values(env: dict[str, str], *, base_dir: Path) -> None:
    """Expand only file-backed env vars needed for coding config export.

    Avoid scanning every inherited ``*_FILE`` variable from the shell; some may point at
    FIFOs, device files, or unrelated credential helpers and can block local export.
    """

    for file_key in ("MINIGENT_TENANT_EXECUTION_CONFIGS_FILE",):
        raw_path = env.get(file_key, "").strip()
        if not raw_path:
            continue
        target_key = file_key[: -len("_FILE")]
        value_path = Path(raw_path).expanduser()
        if not value_path.is_absolute():
            value_path = base_dir / value_path
        env[target_key] = value_path.read_text(encoding="utf-8").strip()


def _env_file_already_loaded(env_path: Path) -> bool:
    configured = os.environ.get(DOTENV_FILE_ENV, "").strip()
    if not configured:
        return False
    configured_path = Path(configured).expanduser()
    try:
        return configured_path.resolve() == env_path.resolve()
    except OSError:
        return configured_path == env_path


def _coding_mcp_specs_from_raw_json(raw_json: str) -> list[dict[str, object]]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    raw_servers = payload.get("servers") if isinstance(payload, dict) else payload
    if not isinstance(raw_servers, list):
        return []
    return [
        _sanitize_coding_mcp_server_spec(server)
        for server in raw_servers
        if isinstance(server, dict)
    ]


def _sanitize_coding_mcp_server_spec(server: dict[str, Any]) -> dict[str, object]:
    exported: dict[str, object] = {}
    for key, value in server.items():
        if key == "headers" and isinstance(value, dict):
            exported[key] = {header: "<set>" for header in sorted(value)}
        elif key == "env" and isinstance(value, dict):
            exported[key] = {
                env_key: value
                if isinstance(value, str) and value.startswith("${")
                else f"${{{env_key}}}"
                for env_key, value in sorted(value.items())
            }
        elif _is_public_config_value(value):
            exported[key] = value
    return exported


def _coding_mcp_server_spec_to_export_dict(spec: Any) -> dict[str, object]:
    exported: dict[str, object] = {
        "name": spec.name,
        "transport": spec.transport,
    }
    if spec.command is not None:
        exported["command"] = list(spec.command)
    if spec.transport == "http" and spec.url:
        exported["url"] = spec.url
    if spec.profiles:
        exported["profiles"] = list(spec.profiles)
    if spec.allowed_tools is not None:
        exported["allowed_tools"] = list(spec.allowed_tools)
    if spec.path_policy:
        exported["path_policy"] = spec.path_policy
    if spec.env:
        exported["env"] = {key: f"${{{key}}}" for key in sorted(spec.env)}
    if spec.headers:
        exported["headers"] = {key: "<set>" for key in sorted(spec.headers)}
    if spec.managed:
        exported["managed"] = True
    if spec.health_url:
        exported["health_url"] = spec.health_url
    if spec.request_timeout != 30.0:
        exported["request_timeout"] = spec.request_timeout
    if spec.timeout_seconds != 30.0:
        exported["timeout_seconds"] = spec.timeout_seconds
    if not spec.enabled:
        exported["enabled"] = False
    return exported


def _is_public_config_value(value: object) -> bool:
    if isinstance(value, str | int | float | bool) or value is None:
        return True
    if isinstance(value, list):
        return all(_is_public_config_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_public_config_value(item) for key, item in value.items()
        )
    return False
