from __future__ import annotations

import argparse
import json
import os
import tomllib
import urllib.parse
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from app.unified_config import (
    CONFIG_FILE_ENV,
    DOTENV_FILE_ENV,
    load_unified_config_env,
    resolve_config_path,
    resolve_dotenv_path,
)
from minigent_client.api_client import MinigentAPIClient
from minigent_client.errors import MinigentAPIError
from minigent_client.output import print_json

DEFAULT_CONFIG_PROFILE = "local-coding"
CONFIG_INIT_PROFILES = ("basic-chat", "openrouter", "local-coding", "voice")

DEFAULT_CONFIG_TEMPLATE = """# Unified Minigent config facade.
# Keep secrets in your shell, OS keychain, or .env.
# Existing MINIGENT_* / provider env vars still override values from this file.

profile = "local-coding"

[app]
host = "127.0.0.1"
port = 8000
thread_db_path = ".data/minigent.db"

[auth]
mode = "development"

[llm]
provider = "mock"
# provider = "openrouter"
# model = "anthropic/claude-sonnet-4.5"
# api_key_env = "OPENROUTER_API_KEY"

[coding]
enabled = true
workspaces = ["/Users/you/code"]
shell_enabled = false
# shell_allowed_command_prefixes = ["uv ", "pytest ", "git "]

[quality]
enabled = false
"""

BASIC_CHAT_CONFIG_TEMPLATE = """# Basic local Minigent config.
# Uses the mock LLM provider so no API key is required.

profile = "basic-chat"

[app]
host = "127.0.0.1"
port = 8000
thread_db_path = ".data/minigent.db"

[auth]
mode = "development"

[llm]
provider = "mock"

[quality]
enabled = false
"""

OPENROUTER_CONFIG_TEMPLATE = """# Minigent config for OpenRouter-backed chat.
# Keep OPENROUTER_API_KEY in your environment or .env; do not commit it.

profile = "openrouter"

[app]
host = "127.0.0.1"
port = 8000
thread_db_path = ".data/minigent.db"

[auth]
mode = "development"

[llm]
provider = "openrouter"
model = "anthropic/claude-sonnet-4.5"
api_key_env = "OPENROUTER_API_KEY"

[quality]
enabled = false
"""

VOICE_CONFIG_TEMPLATE = """# Minigent voice-oriented config facade.
# Detailed audio/VAD tuning remains available through MINIGENT_VOICE_* env vars.

profile = "voice"

[app]
host = "127.0.0.1"
port = 8000
thread_db_path = ".data/minigent-voice.db"

[auth]
mode = "development"

[llm]
provider = "mock"
# provider = "openrouter"
# model = "anthropic/claude-sonnet-4.5"
# api_key_env = "OPENROUTER_API_KEY"

[voice]
tenant_id = "demo-tenant"
user_id = "voice-user"
skill = "assistant"
wake_phrase = "hey minigent"
stt_provider = "whisper"
tts_provider = "piper"

[quality]
enabled = false
"""

CONFIG_PROFILE_TEMPLATES = {
    "basic-chat": BASIC_CHAT_CONFIG_TEMPLATE,
    "openrouter": OPENROUTER_CONFIG_TEMPLATE,
    "local-coding": DEFAULT_CONFIG_TEMPLATE,
    "voice": VOICE_CONFIG_TEMPLATE,
}


@dataclass(frozen=True)
class DiagnosticCheck:
    status: str
    label: str
    detail: str | None = None
    blocking: bool = False

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "label": self.label,
            "blocking": self.blocking,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def run_config(client: MinigentAPIClient, trace_id: str | None) -> int:
    response = client.config()
    if trace_id is not None:
        response = {**response, "trace_id": trace_id}
    print_json(response)
    return 0


def run_config_init(args: argparse.Namespace) -> int:
    output_path = Path(args.output).expanduser()
    if output_path.exists() and not args.force:
        raise RuntimeError(f"{output_path} already exists; use --force to overwrite")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profile = getattr(args, "profile", DEFAULT_CONFIG_PROFILE)
    template = CONFIG_PROFILE_TEMPLATES[profile]
    output_path.write_text(template, encoding="utf-8")
    print(f"Wrote {output_path} ({profile})")
    return 0


def run_config_print(args: argparse.Namespace) -> int:
    output = collect_resolved_local_config()
    print_json(output)
    return 0


def run_config_export(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    server_config = client.config(export=True)
    export = export_unified_config_from_server(server_config)
    if not getattr(args, "include_runtime", False):
        export.pop("runtime", None)
    if trace_id is not None:
        export = {**export, "trace_id": trace_id}
    text = json.dumps(export, indent=2, sort_keys=True) + "\n" if args.json else render_unified_config_toml(export)
    output_path = getattr(args, "output", None)
    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
        if not args.json:
            print(f"Wrote {output_path}")
    else:
        print(text, end="")
    return 0


def export_unified_config_from_server(server_config: dict[str, Any]) -> dict[str, object]:
    export: dict[str, object] = {
        "profile": "exported",
        "_comments": [
            "Generated from a running Minigent server via /config.",
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
    if "tenant_execution_configs" in export:
        mcp_export = export.get("mcp")
        if isinstance(mcp_export, dict):
            mcp_export.pop("servers", None)
    pruned_export = _prune_empty_values(export)
    return pruned_export if isinstance(pruned_export, dict) else export


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
        allowed={"enabled", "provider", "model", "base_url", "mode", "timeout", "max_payload_chars"},
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
    lines.append("[" + ".".join(_toml_key(str(part)) for part in path) + "]")
    nested_tables: list[tuple[str, dict[object, object]]] = []
    array_tables: list[tuple[str, list[object]]] = []
    for key, value in table.items():
        key_str = str(key)
        if isinstance(value, dict):
            nested_tables.append((key_str, value))
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            array_tables.append((key_str, value))
        else:
            lines.append(f"{_toml_key(key_str)} = {_toml_value(value)}")
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


def collect_resolved_local_config() -> dict[str, object]:
    cwd = Path.cwd()
    env_snapshot = dict(os.environ)
    dotenv_path = resolve_dotenv_path(base_dir=cwd, env=env_snapshot)
    dotenv_keys: list[str] = []
    if dotenv_path is not None and dotenv_path.exists():
        from dotenv import dotenv_values

        dotenv = {key: value for key, value in dotenv_values(dotenv_path).items() if value is not None}
        dotenv_keys = sorted(dotenv)
        env_snapshot.update(dotenv)
    config_path = resolve_config_path(base_dir=cwd, env=env_snapshot)
    config_env = load_unified_config_env(config_path, source_env=env_snapshot)
    resolved = dict(config_env)
    for key in sorted(set(config_env) | set(env_snapshot)):
        if key in config_env or key.startswith(
            ("MINIGENT_", "OPENAI_", "OPENROUTER_", "GOOGLE_", "GEMINI_")
        ):
            if key in env_snapshot:
                resolved[key] = env_snapshot[key]
    return {
        "config_file": str(config_path) if config_path is not None else None,
        "config_file_env": os.environ.get(CONFIG_FILE_ENV),
        "dotenv_file_env": os.environ.get(DOTENV_FILE_ENV),
        "dotenv_file": str(dotenv_path) if dotenv_path is not None and dotenv_path.exists() else None,
        "dotenv_keys": dotenv_keys,
        "resolved_env": mask_secrets(dict(sorted(resolved.items()))),
    }


def run_config_doctor(
    args: argparse.Namespace,
    client: MinigentAPIClient,
    trace_id: str | None,
) -> int:
    checks: list[DiagnosticCheck] = [*_local_config_checks(args)]
    config_response: dict[str, Any] | None = None
    if not any(check.blocking for check in checks):
        connection_checks, config_response = collect_connection_checks(args, client)
        checks.extend(connection_checks)
    checks.extend(server_config_checks(config_response))
    success = not any(check.blocking for check in checks)

    if args.json:
        output: dict[str, object] = {
            "ok": success,
            "checks": [check.to_dict() for check in checks],
        }
        if trace_id is not None:
            output["trace_id"] = trace_id
        print_json(output)
        return 0 if success else 1

    if trace_id is not None:
        print(f"trace_id={trace_id}")
    print("Minigent config doctor")
    print("")
    for check in checks:
        print(format_check(check))
    print("")
    if success:
        print("No blocking issues found.")
    else:
        print("Blocking issues found. Re-run with --verbose for technical details.")
    return 0 if success else 1


def collect_connection_checks(
    args: argparse.Namespace,
    client: MinigentAPIClient,
) -> tuple[list[DiagnosticCheck], dict[str, Any] | None]:
    checks: list[DiagnosticCheck] = []
    config_response: dict[str, Any] | None = None
    try:
        health_response = client.health()
    except MinigentAPIError as exc:
        checks.append(
            DiagnosticCheck(
                "error",
                "API reachable",
                diagnostic_detail(exc, verbose=args.verbose),
                blocking=True,
            )
        )
        return checks, None
    status = health_response.get("status")
    detail = str(status) if status is not None else None
    checks.append(DiagnosticCheck("ok", "API reachable", detail))

    try:
        config_response = client.config()
    except MinigentAPIError as exc:
        checks.append(
            DiagnosticCheck(
                "error",
                "Server config readable",
                diagnostic_detail(exc, verbose=args.verbose),
                blocking=True,
            )
        )
        return checks, None
    checks.append(DiagnosticCheck("ok", "Server config readable"))
    return checks, config_response


def _local_config_checks(args: argparse.Namespace) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    parsed = urllib.parse.urlparse(args.base_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        checks.append(DiagnosticCheck("ok", "Base URL configured", args.base_url.rstrip("/")))
    else:
        checks.append(
            DiagnosticCheck(
                "error",
                "Base URL configured",
                "Use an http:// or https:// URL.",
                blocking=True,
            )
        )
    if args.api_token:
        checks.append(DiagnosticCheck("ok", "API token present"))
    else:
        checks.append(
            DiagnosticCheck(
                "ok",
                "Trusted principal headers configured",
                f"user={args.user_id} tenant={args.tenant_id}",
            )
        )
    checks.extend(_unified_config_checks())
    return checks


def _unified_config_checks() -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    cwd = Path.cwd()
    env_snapshot = _env_snapshot_with_dotenv(cwd)
    config_path = resolve_config_path(base_dir=cwd, env=env_snapshot)
    if config_path is None:
        checks.append(
            DiagnosticCheck(
                "warning",
                "Unified config file",
                "not found; run `minigent config init` to create minigent.toml",
            )
        )
        return checks
    if not config_path.exists():
        checks.append(
            DiagnosticCheck(
                "error",
                "Unified config file",
                f"{config_path} does not exist",
                blocking=True,
            )
        )
        return checks
    try:
        with config_path.open("rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        checks.append(
            DiagnosticCheck(
                "error",
                "Unified config parses",
                f"{config_path}: {exc}",
                blocking=True,
            )
        )
        return checks
    checks.append(DiagnosticCheck("ok", "Unified config parses", str(config_path)))

    try:
        config_env = load_unified_config_env(config_path, source_env=env_snapshot)
    except Exception as exc:
        checks.append(
            DiagnosticCheck(
                "error",
                "Unified config resolves",
                str(exc),
                blocking=True,
            )
        )
        return checks
    resolved_env = {**config_env, **env_snapshot}
    checks.extend(_llm_config_checks(data, resolved_env))
    checks.extend(_coding_config_checks(data, resolved_env))
    checks.extend(_mcp_config_checks(data))
    return checks


def _env_snapshot_with_dotenv(cwd: Path) -> dict[str, str]:
    env_snapshot = dict(os.environ)
    dotenv_path = resolve_dotenv_path(base_dir=cwd, env=env_snapshot)
    if dotenv_path is not None and dotenv_path.exists():
        from dotenv import dotenv_values

        env_snapshot.update(
            {key: value for key, value in dotenv_values(dotenv_path).items() if value is not None}
        )
    return env_snapshot


def _llm_config_checks(data: dict[str, Any], resolved_env: dict[str, str]) -> list[DiagnosticCheck]:
    provider = resolved_env.get("MINIGENT_LLM_PROVIDER", "mock").strip().lower() or "mock"
    checks = [DiagnosticCheck("ok", "LLM provider", provider)]
    if provider == "mock":
        return checks
    required_key_groups: dict[str, tuple[str, ...]] = {
        "openai": ("OPENAI_API_KEY",),
        "openrouter": ("OPENROUTER_API_KEY",),
        "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "google-generative-ai": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    }
    if provider in required_key_groups:
        keys = required_key_groups[provider]
        if any(resolved_env.get(key) for key in keys):
            checks.append(DiagnosticCheck("ok", "LLM API key configured", " or ".join(keys)))
        else:
            checks.append(
                DiagnosticCheck(
                    "error",
                    "LLM API key configured",
                    f"set one of: {', '.join(keys)}",
                    blocking=True,
                )
            )
        return checks
    if provider == "generic-oauth":
        missing = [
            key
            for key in ("MINIGENT_LLM_MODEL", "MINIGENT_LLM_URL")
            if not resolved_env.get(key)
        ]
        checks.append(
            DiagnosticCheck(
                "ok" if not missing else "error",
                "Generic OAuth LLM settings",
                "configured" if not missing else f"missing: {', '.join(missing)}",
                blocking=bool(missing),
            )
        )
        return checks
    llm_section = data.get("llm") if isinstance(data, dict) else None
    if isinstance(llm_section, dict) and provider:
        checks.append(DiagnosticCheck("warning", "LLM provider supported", "not recognized locally"))
    return checks


def _coding_config_checks(data: dict[str, Any], resolved_env: dict[str, str]) -> list[DiagnosticCheck]:
    coding = data.get("coding") if isinstance(data, dict) else None
    if not isinstance(coding, dict):
        return []
    checks: list[DiagnosticCheck] = []
    profile = str(data.get("profile", "")).strip()
    coding_enabled = _config_bool(coding.get("enabled")) or profile == "local-coding"
    workspaces = _workspace_paths(coding, resolved_env)
    if coding_enabled and not workspaces:
        checks.append(
            DiagnosticCheck(
                "warning",
                "Coding workspaces configured",
                "none configured for local-coding profile",
            )
        )
    elif workspaces:
        missing = [str(path) for path in workspaces if not path.exists()]
        checks.append(
            DiagnosticCheck(
                "ok" if not missing else "warning",
                "Coding workspace paths",
                f"{len(workspaces)} configured" if not missing else f"missing: {', '.join(missing)}",
            )
        )
    shell_enabled = _config_bool(coding.get("shell_enabled")) or _env_bool_value(
        resolved_env.get("MINIGENT_CODING_SHELL_ENABLED")
    )
    prefixes = [
        prefix.strip()
        for prefix in resolved_env.get("MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES", "").split(",")
        if prefix.strip()
    ]
    if shell_enabled and not prefixes:
        checks.append(
            DiagnosticCheck(
                "warning",
                "Coding shell allowlist",
                "shell is enabled but no allowed command prefixes are configured",
            )
        )
    return checks


def _workspace_paths(coding: dict[str, Any], resolved_env: dict[str, str]) -> list[Path]:
    raw = resolved_env.get("MINIGENT_CODING_WORKSPACES") or resolved_env.get(
        "MINIGENT_CODING_WORKSPACE", ""
    )
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if not values:
        configured = coding.get("workspaces", coding.get("workspace", []))
        if isinstance(configured, str):
            values = [configured]
        elif isinstance(configured, list):
            values = [str(value) for value in configured if str(value).strip()]
    return [Path(value).expanduser() for value in values]


def _mcp_config_checks(data: dict[str, Any]) -> list[DiagnosticCheck]:
    mcp = data.get("mcp") if isinstance(data, dict) else None
    if not isinstance(mcp, dict) or "servers" not in mcp:
        return []
    servers = mcp.get("servers")
    if not isinstance(servers, list):
        return [
            DiagnosticCheck(
                "error",
                "MCP server config",
                "mcp.servers must be a list",
                blocking=True,
            )
        ]
    missing: list[str] = []
    names: list[str] = []
    for index, server in enumerate(servers):
        if not isinstance(server, dict):
            missing.append(f"#{index}: not an object")
            continue
        name = server.get("name")
        url = server.get("url")
        if not isinstance(name, str) or not name:
            missing.append(f"#{index}: missing name")
        else:
            names.append(name)
        if not isinstance(url, str) or not url:
            missing.append(f"#{index}: missing url")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if missing or duplicates:
        details = [*missing]
        if duplicates:
            details.append(f"duplicate names: {', '.join(duplicates)}")
        return [
            DiagnosticCheck(
                "error",
                "MCP server config",
                "; ".join(details),
                blocking=True,
            )
        ]
    return [DiagnosticCheck("ok", "MCP server config", f"{len(servers)} configured")]


def _config_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _env_bool_value(value)
    return False


def _env_bool_value(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def server_config_checks(config_response: dict[str, Any] | None) -> list[DiagnosticCheck]:
    if config_response is None:
        return []
    checks: list[DiagnosticCheck] = []
    summary = server_summary(config_response)
    backend = summary.get("backend")
    if backend:
        checks.append(DiagnosticCheck("ok", "Backend mode", backend))
    else:
        checks.append(DiagnosticCheck("warning", "Backend mode", "not reported"))
    model = summary.get("model")
    if model:
        checks.append(DiagnosticCheck("ok", "Default model configured", model))
    else:
        checks.append(DiagnosticCheck("warning", "Default model configured", "not reported"))
    mcp_servers = config_response.get("mcp_servers")
    if isinstance(mcp_servers, list) and mcp_servers:
        checks.append(DiagnosticCheck("ok", "MCP servers configured", str(len(mcp_servers))))
    else:
        checks.append(DiagnosticCheck("warning", "MCP servers configured", "none reported"))
    agent_backend = config_response.get("agent_backend")
    mcp_broker_enabled = (
        agent_backend.get("mcp_broker_enabled") if isinstance(agent_backend, dict) else None
    )
    if mcp_broker_enabled is True:
        checks.append(DiagnosticCheck("ok", "MCP broker enabled"))
    else:
        checks.append(DiagnosticCheck("warning", "MCP broker enabled", "false or not reported"))
    quality = config_response.get("quality")
    quality_enabled = quality.get("enabled") if isinstance(quality, dict) else None
    checks.append(
        DiagnosticCheck(
            "ok" if quality_enabled is True else "warning",
            "Remote quality enhancement",
            "enabled" if quality_enabled is True else "disabled or not reported",
        )
    )
    return checks


def server_summary(config_response: dict[str, Any]) -> dict[str, str]:
    summary: dict[str, str] = {}
    agent_backend = config_response.get("agent_backend")
    if isinstance(agent_backend, dict):
        backend = agent_backend.get("type")
        if isinstance(backend, str) and backend:
            summary["backend"] = backend
    llm = config_response.get("llm")
    if isinstance(llm, dict):
        model = llm.get("model")
        provider = llm.get("provider")
        if isinstance(model, str) and model:
            summary["model"] = model
        if isinstance(provider, str) and provider:
            summary["provider"] = provider
    return summary


def diagnostic_detail(exc: MinigentAPIError, *, verbose: bool) -> str:
    if verbose and exc.detail:
        return f"{exc.message} Detail: {exc.detail}"
    return exc.message


def format_check(check: DiagnosticCheck) -> str:
    marker = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(check.status, "•")
    line = f"{marker} {check.label}"
    if check.detail:
        line = f"{line}: {check.detail}"
    return line


_SECRET_KEY_PARTS = ("token", "secret", "key", "authorization", "password")


def mask_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        return "<set>" if value else ""
    return "<set>"


def mask_secrets(value: object) -> object:
    if isinstance(value, dict):
        masked: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.casefold() for part in _SECRET_KEY_PARTS):
                masked[key_text] = mask_value(item)
            else:
                masked[key_text] = mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def package_version() -> str:
    try:
        return version("minigent")
    except PackageNotFoundError:
        return "unknown"
