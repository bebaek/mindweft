"""Opinionated inspect-only front door; the advanced workspace runner stays configurable."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import sys
import tempfile
import webbrowser
from contextlib import contextmanager
from pathlib import Path

from mindweft_client.config_diagnostics import _llm_config_checks
from mindweft_config.unified_config import (
    default_mindweft_user_config_path,
    default_user_config_path,
    load_unified_config_env,
    normalize_mindweft_env,
    preferred_mindweft_env,
)
from mindweft_workspace.cli import parse_args
from mindweft_workspace.environment import apply_coding_workspace_state_defaults
from mindweft_workspace.orchestration import run_workspace_processes
from mindweft_workspace.readiness import wait_for_code_ready
from mindweft_workspace.runtime_plan import prepare_workspace_runtime

# Only provider, authentication, and persistence preferences belong in this launch mode.
# In particular: no tenant overlays, local/peer backends, MCP specs, admin database, or skills.
_RUNTIME_PREFIXES = ("LLM_", "OAUTH_", "AUTH_", "SESSION_")
_RUNTIME_KEYS = {
    "THREAD_DB_PATH",
    "ATTACHMENT_DB_PATH",
    "ATTACHMENT_ENCRYPTION_KEY",
    "ATTACHMENT_ENCRYPTION_KEYS",
}


def load_code_environment(source: dict[str, str]) -> dict[str, str]:
    """Load explicitly selected or user-level TOML, never cwd TOML/dotenv/_FILE secrets.

    The resulting child environment disables all further file discovery. Both canonical
    and legacy names are filtered so normalization cannot restore wider permissions.
    """
    source = normalize_mindweft_env(dict(source))
    explicit = preferred_mindweft_env("CONFIG_FILE", source)
    path = Path(explicit).expanduser().resolve() if explicit else None
    if path is not None and not path.is_file():
        raise RuntimeError("Explicit Mindweft config file does not exist.")
    if path is None and preferred_mindweft_env("CONFIG_DISCOVERY", source) not in {
        "disabled",
        "false",
        "0",
        "off",
        "no",
        "explicit",
    }:
        path = next(
            (
                p
                for p in (
                    default_mindweft_user_config_path(source),
                    default_user_config_path(source),
                )
                if p.is_file()
            ),
            None,
        )
    try:
        configured = load_unified_config_env(path, source_env=source)
    except (ValueError, OSError) as exc:
        # TOML/schema exceptions may contain secret values; do not echo their contents.
        raise RuntimeError(
            "Cannot load Mindweft config; check its TOML/schema with mindweft config doctor."
        ) from exc
    combined = {**configured, **source}
    env = {}
    for key, value in combined.items():
        if key.startswith(("MINDWEFT_", "MINIGENT_")):
            suffix = key.split("_", 1)[1]
            if suffix.endswith("_FILE") or not (
                suffix.startswith(_RUNTIME_PREFIXES) or suffix in _RUNTIME_KEYS
            ):
                continue
        if key in {"PYTHONPATH", "PYTHONHOME"}:
            continue
        env[key] = value
    for suffix in ("THREAD_DB_PATH", "ATTACHMENT_DB_PATH", "OAUTH_STORE_PATH"):
        value = preferred_mindweft_env(suffix, env)
        if value:
            base = (
                Path.cwd()
                if preferred_mindweft_env(suffix, source) is not None
                else (path.parent if path else Path.cwd())
            )
            resolved = Path(value).expanduser()
            if not resolved.is_absolute():
                resolved = base / resolved
            env[f"MINDWEFT_{suffix}"] = str(resolved.resolve())
    env["MINDWEFT_CONFIG_DISCOVERY"] = "disabled"
    env["PYTHONSAFEPATH"] = "1"
    normalize_mindweft_env(env)
    return env


def check_provider(env: dict[str, str], *, demo: bool) -> None:
    if demo:
        for prefix in ("MINDWEFT_", "MINIGENT_"):
            for key in list(env):
                if key.startswith(prefix + "LLM_"):
                    del env[key]
        env["MINIGENT_LLM_PROVIDER"] = "mock"
        return
    provider = env.get("MINIGENT_LLM_PROVIDER", "mock").strip().lower()
    if not provider or provider == "mock":
        raise RuntimeError(
            "Configure a real provider in user-level mindweft.toml or the environment first. "
            "See mindweft config init --help and mindweft config doctor. "
            "For a mock-only demo, use mindweft code --demo."
        )
    supported = {
        "openai",
        "openrouter",
        "anthropic",
        "google",
        "gemini",
        "google-generative-ai",
        "generic-oauth",
    }
    if provider not in supported:
        raise RuntimeError("Unsupported LLM provider; run mindweft config doctor.")
    checks = _llm_config_checks({}, env)
    failures = [check.detail for check in checks if check.blocking and check.detail]
    if provider == "anthropic" and not env.get("ANTHROPIC_API_KEY"):
        failures.append("set ANTHROPIC_API_KEY")
    if failures:
        raise RuntimeError(
            "Provider prerequisites missing: "
            + "; ".join(failures)
            + ". Run mindweft config doctor."
        )


def check_port(port: int, flag: str) -> None:
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{flag} must be between 1 and 65535.")
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RuntimeError(
            f"Local port {port} is unavailable; choose another with {flag}."
        ) from exc


def open_console(url: str) -> None:
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print(f"Could not open a browser. Open {url} manually.", file=sys.stderr)


@contextmanager
def shutdown_on_sigterm():
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_code_command(args: argparse.Namespace) -> int:
    try:
        if args.env_file:
            raise RuntimeError(
                "mindweft code does not load dotenv files; use environment or user-level TOML settings."
            )
        workspace = Path(args.path).expanduser().resolve()
        if not workspace.is_dir() or not os.access(workspace, os.R_OK | os.X_OK):
            raise RuntimeError(f"Workspace must be an accessible directory: {workspace}")
        env = load_code_environment(dict(os.environ))
        if not shutil.which("npx", path=env.get("PATH")):
            raise RuntimeError(
                "The filesystem integration requires Node.js/npm (npx). Install Node.js, then rerun this command."
            )
        check_provider(env, demo=args.demo)
        auth_mode = env.get("MINIGENT_AUTH_MODE", "dev-headers")
        if (
            args.api_token
            or auth_mode not in {"dev-headers", "development"}
            or env.get("MINIGENT_SESSION_CREDENTIALS")
        ):
            raise RuntimeError(
                "mindweft code currently supports trusted-local development authentication only; use mindweft-coding-workspace for configured token/session/JWT authentication."
            )
        env["MINDWEFT_AUTH_MODE"] = "dev-headers"
        check_port(args.port, "--port")
        check_port(args.gateway_port, "--gateway-port")
        if args.port == args.gateway_port:
            raise RuntimeError("--port and --gateway-port must be different.")
        apply_coding_workspace_state_defaults(env)
        runner_args = parse_args(
            [
                "--no-env-file",
                "--workspace",
                str(workspace),
                "--enable-text",
                "--mcp-gateway",
                "--api-host",
                "127.0.0.1",
                "--bridge-host",
                "127.0.0.1",
                "--api-port",
                str(args.port),
                "--mcp-gateway-port",
                str(args.gateway_port),
            ]
        )
        plan = prepare_workspace_runtime(runner_args, env)
        url = f"http://127.0.0.1:{args.port}/console/"
        print(f"Workspace: {workspace}\nAccess: inspect (read-only)", flush=True)
        print("Trusted-local development mode; do not expose these ports to a network.", flush=True)
        if args.demo:
            print("Demo provider: mock (no real AI responses).", flush=True)
        else:
            print(
                "Provider configuration checked locally; credentials have not been tested with the provider.",
                flush=True,
            )

        def ready(processes):
            wait_for_code_ready(processes, api_port=args.port, specs=plan.mcp_servers.tenant_specs)
            print(f"Ready: {url}\nPress Ctrl+C to stop.", flush=True)
            if not args.no_open:
                open_console(url)

        # Avoid importing workspace-local Python modules or picking up project npm config.
        with (
            shutdown_on_sigterm(),
            tempfile.TemporaryDirectory(prefix="mindweft-code-") as process_dir,
        ):
            return run_workspace_processes(
                env=env,
                mcp_server_specs=plan.mcp_servers.process_specs,
                skip_bridge=False,
                gateway_enabled=True,
                bridge_host="127.0.0.1",
                gateway_port=args.gateway_port,
                skip_api=False,
                api_host="127.0.0.1",
                api_port=args.port,
                tenant_id=plan.tenant_id,
                workspace=workspace,
                bridge_name=plan.settings.bridge_name,
                text_bridge_name=plan.settings.text_bridge_name,
                shell_bridge_name=None,
                on_started=ready,
                process_cwd=Path(process_dir),
            )
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
