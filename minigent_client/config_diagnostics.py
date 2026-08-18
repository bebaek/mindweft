from __future__ import annotations

import argparse
import os
import tomllib
import urllib.parse
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from minigent_client.api_client import MinigentAPIClient
from minigent_client.errors import MinigentAPIError
from minigent_client.output import print_json
from minigent_config.unified_config import (
    MINDWEFT_CONFIG_FILE,
    load_unified_config_env,
    normalize_mindweft_env,
    resolve_config_path,
    resolve_dotenv_path,
)


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
    print("Mindweft config doctor")
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
                f"not found; run `mindweft config init` to create {MINDWEFT_CONFIG_FILE}",
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
    resolved_env = normalize_mindweft_env({**config_env, **env_snapshot})
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
    llm_section_raw = data.get("llm") if isinstance(data, dict) else None
    llm_section: dict[str, Any] = llm_section_raw if isinstance(llm_section_raw, dict) else {}
    providers = llm_section.get("providers")
    if isinstance(providers, dict) and providers:
        return _named_llm_profile_checks(llm_section, providers, resolved_env)
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
            key for key in ("MINIGENT_LLM_MODEL", "MINIGENT_LLM_URL") if not resolved_env.get(key)
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
    llm_section_raw2 = data.get("llm") if isinstance(data, dict) else None
    if isinstance(llm_section_raw2, dict) and provider:
        checks.append(
            DiagnosticCheck("warning", "LLM provider supported", "not recognized locally")
        )
    return checks


def _named_llm_profile_checks(
    llm_section: dict[str, Any],
    providers: dict[str, Any],
    resolved_env: dict[str, str],
) -> list[DiagnosticCheck]:
    default = str(llm_section.get("default", "")).strip() or next(iter(providers))
    checks = [
        DiagnosticCheck(
            "ok",
            "Named LLM profiles",
            f"{len(providers)} configured; default={default}",
        )
    ]
    for name, raw_profile in providers.items():
        if not isinstance(raw_profile, dict):
            continue
        provider = str(raw_profile.get("provider", "mock")).strip().lower() or "mock"
        missing: list[str] = []
        if provider != "mock" and not str(raw_profile.get("model", "")).strip():
            missing.append("model")
        api_key_env = str(raw_profile.get("api_key_env", "")).strip()
        if provider not in {"mock", "generic-oauth"} and not raw_profile.get("api_key"):
            if not api_key_env or not resolved_env.get(api_key_env):
                missing.append(api_key_env or "api_key_env")
        if provider == "generic-oauth" and not (
            raw_profile.get("url") or raw_profile.get("base_url")
        ):
            missing.append("url/base_url")
        checks.append(
            DiagnosticCheck(
                "ok" if not missing else "error",
                f"LLM profile {name}",
                provider if not missing else f"{provider}; missing: {', '.join(missing)}",
                blocking=bool(missing),
            )
        )
    return checks


def _coding_config_checks(
    data: dict[str, Any], resolved_env: dict[str, str]
) -> list[DiagnosticCheck]:
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
                f"{len(workspaces)} configured"
                if not missing
                else f"missing: {', '.join(missing)}",
            )
        )
    shell_enabled = _config_bool(coding.get("shell_enabled")) or _env_bool_value(
        resolved_env.get("MINIGENT_CODING_SHELL_ENABLED")
    )
    prefixes = [
        prefix.strip()
        for prefix in resolved_env.get("MINIGENT_CODING_SHELL_ALLOWED_COMMAND_PREFIXES", "").split(
            ","
        )
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
    checks.extend(_coding_mcp_timeout_checks(coding, resolved_env))
    return checks


def _coding_mcp_timeout_checks(
    coding: dict[str, Any], resolved_env: dict[str, str]
) -> list[DiagnosticCheck]:
    raw_tool_timeout = resolved_env.get("MINIGENT_TOOL_TIMEOUT_SECONDS", "60")
    try:
        tool_timeout = float(raw_tool_timeout)
    except ValueError:
        return [
            DiagnosticCheck(
                "error",
                "Runtime tool timeout",
                "MINIGENT_TOOL_TIMEOUT_SECONDS must be numeric",
                blocking=True,
            )
        ]
    specs = coding.get("mcp_server_specs", coding.get("mcpServerSpecs", []))
    if not isinstance(specs, list):
        return []
    oversized: list[str] = []
    restart_enabled: list[str] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name", "<unnamed>"))
        request_timeout = _positive_float_or_none(
            spec.get("request_timeout", spec.get("requestTimeout"))
        )
        timeout_seconds = _positive_float_or_none(
            spec.get("timeout_seconds", spec.get("timeoutSeconds"))
        )
        server_timeout = (
            max(value for value in (request_timeout, timeout_seconds) if value is not None)
            if (request_timeout is not None or timeout_seconds is not None)
            else None
        )
        if server_timeout is not None and tool_timeout < server_timeout:
            oversized.append(f"{name}={server_timeout:g}s")
        if spec.get("restart_on_timeout", spec.get("restartOnTimeout")) is True:
            restart_enabled.append(name)
    checks: list[DiagnosticCheck] = []
    if oversized:
        checks.append(
            DiagnosticCheck(
                "warning",
                "Runtime/MCP timeout alignment",
                f"app.tool_timeout_seconds ({tool_timeout:g}s) is shorter than: {', '.join(oversized)}",
            )
        )
    elif specs:
        checks.append(
            DiagnosticCheck(
                "ok",
                "Runtime/MCP timeout alignment",
                f"app.tool_timeout_seconds={tool_timeout:g}s",
            )
        )
    if restart_enabled:
        checks.append(
            DiagnosticCheck(
                "ok",
                "MCP stdio timeout recovery",
                f"restart_on_timeout enabled for: {', '.join(restart_enabled)}",
            )
        )
    return checks


def _positive_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    return None


def _workspace_paths(coding: dict[str, Any], resolved_env: dict[str, str]) -> list[Path]:
    values: list[str] = []
    configured = coding.get("workspaces", coding.get("workspace", []))
    if isinstance(configured, str):
        values = [configured]
    elif isinstance(configured, list):
        values = [str(value) for value in configured if str(value).strip()]
    if not values:
        raw = resolved_env.get("MINIGENT_CODING_WORKSPACES") or resolved_env.get(
            "MINIGENT_CODING_WORKSPACE", ""
        )
        values = [value.strip() for value in raw.split(",") if value.strip()]
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


def package_version() -> str:
    try:
        return version("minigent")
    except PackageNotFoundError:
        return "unknown"
