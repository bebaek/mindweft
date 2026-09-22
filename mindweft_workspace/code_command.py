"""Trust-gated coding front door; the advanced workspace runner stays configurable."""

from __future__ import annotations

import argparse
import os
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
from mindweft_workspace.instances import InstanceLease, lock_storage, package_version, reserve_ports
from mindweft_workspace.local_connection import browser_url, credential_directory
from mindweft_workspace.local_credentials import issue_credential
from mindweft_workspace.orchestration import run_workspace_processes
from mindweft_workspace.readiness import wait_for_code_ready
from mindweft_workspace.runtime_plan import prepare_workspace_runtime
from mindweft_workspace.workspace_trust import choose_coding_access

# Only provider, authentication, and persistence preferences belong in this launch mode.
# In particular: no tenant overlays, local/peer backends, MCP specs, admin database, or skills.
_RUNTIME_PREFIXES = ("LLM_", "OAUTH_", "AUTH_", "SESSION_")
_RUNTIME_KEYS = {
    "THREAD_DB_PATH",
    "ATTACHMENT_DB_PATH",
    "ATTACHMENT_ENCRYPTION_KEY",
    "ATTACHMENT_ENCRYPTION_KEYS",
}


def load_code_environment(source: dict[str, str], *, report_source: bool = False) -> dict[str, str]:
    """Load explicitly selected or user-level TOML, never cwd TOML/dotenv/_FILE secrets.

    The resulting child environment disables all further file discovery. Both canonical
    and legacy names are filtered so normalization cannot restore wider permissions.
    """
    source = normalize_mindweft_env(dict(source))
    explicit = preferred_mindweft_env("CONFIG_FILE", source)
    selected_path = Path(explicit).expanduser().absolute() if explicit else None
    path = selected_path.resolve() if selected_path is not None else None
    if path is not None and not path.is_file():
        raise RuntimeError("Explicit Mindweft config file does not exist.")
    discovery_disabled = preferred_mindweft_env("CONFIG_DISCOVERY", source) in {
        "disabled",
        "false",
        "0",
        "off",
        "no",
        "explicit",
    }
    if path is None and not discovery_disabled:
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
    if report_source:
        if path is not None:
            selected = selected_path or path.absolute()
            print(f"Config: loaded {str(selected)!r}", file=sys.stderr)
            resolved = path.resolve()
            if selected != resolved:
                print(f"Config target: {str(resolved)!r}", file=sys.stderr)
            print(
                "Config scope: provider/auth/storage settings only; tool, tenant, skill, "
                "and workspace settings are not inherited.",
                file=sys.stderr,
            )
        elif discovery_disabled:
            print("Config: environment only (file discovery disabled)", file=sys.stderr)
        else:
            print("Config: environment only (no user-level TOML found)", file=sys.stderr)
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
        print(
            "Could not open a browser. Retry with mindweft instances open NAME; authentication tickets are never printed.",
            file=sys.stderr,
        )


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
        workspaces = list(
            dict.fromkeys(Path(path).expanduser().resolve() for path in (args.paths or ["."]))
        )
        for workspace in workspaces:
            if not workspace.is_dir() or not os.access(workspace, os.R_OK | os.X_OK):
                raise RuntimeError(f"Workspace must be an accessible directory: {workspace}")
        env = load_code_environment(dict(os.environ), report_source=True)
        check_provider(env, demo=args.demo)
        auth_mode = env.get("MINIGENT_AUTH_MODE", "dev-headers")
        if (
            args.api_token
            or auth_mode not in {"dev-headers", "development"}
            or env.get("MINIGENT_SESSION_CREDENTIALS")
        ):
            raise RuntimeError(
                "mindweft code provisions its own user credentials; use mindweft-coding-workspace for configured token/session/JWT authentication."
            )
        trusted = choose_coding_access(args, workspaces, env)
        env["MINDWEFT_LOCAL_CODING_TRUSTED"] = "1" if trusted else "0"
        if args.port is not None and args.port == args.gateway_port:
            raise RuntimeError("--port and --gateway-port must be different.")
        with (
            shutdown_on_sigterm(),
            InstanceLease(args.instance or "default", env) as instance,
            reserve_ports(args.port, args.gateway_port) as (api_socket, gateway_socket),
        ):
            return run_code_instance(args, env, workspaces, instance, api_socket, gateway_socket)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def run_code_instance(args, env, workspaces, instance, api_socket, gateway_socket) -> int:
    api_port = api_socket.getsockname()[1]
    if instance.name == "default" and args.port is None and api_port != 8000:
        raise RuntimeError(
            "Default port 8000 is occupied. Use --instance preview for isolated state; an older running launcher may still use the default databases."
        )
    gateway_port = gateway_socket.getsockname()[1]
    shared_oauth = bool(preferred_mindweft_env("OAUTH_STORE_PATH", env))
    if shared_oauth:
        print("Provider OAuth: reusing configured store (no credentials copied).", flush=True)
        if not (
            preferred_mindweft_env("OAUTH_ENCRYPTION_KEYS", env)
            or preferred_mindweft_env("OAUTH_ENCRYPTION_KEY", env)
        ):
            print(
                "Provider OAuth uses a shared JSON store without cross-process refresh coordination; "
                "avoid simultaneous provider runs in multiple instances. Encrypted SQLite stores coordinate refreshes.",
                file=sys.stderr,
            )
    if instance.name != "default":
        for suffix, filename in (
            ("THREAD_DB_PATH", "threads.db"),
            ("ATTACHMENT_DB_PATH", "attachments.db"),
            (
                "OAUTH_STORE_PATH",
                "oauth.db"
                if env.get("MINIGENT_OAUTH_ENCRYPTION_KEYS")
                or env.get("MINIGENT_OAUTH_ENCRYPTION_KEY")
                else "oauth.json",
            ),
        ):
            if suffix == "OAUTH_STORE_PATH" and shared_oauth:
                continue
            if preferred_mindweft_env(suffix, env):
                print(
                    f"Instance storage overrides configured {suffix} (no data copied).",
                    file=sys.stderr,
                )
            env[f"MINDWEFT_{suffix}"] = str(instance.state_dir / filename)
        env["XDG_STATE_HOME"] = str(instance.state_dir / "xdg")
    env["MINDWEFT_LOCAL_INSTANCE_NAME"] = instance.name
    env["MINDWEFT_LOCAL_LAUNCH_ID"] = instance.launch_id
    env["MINDWEFT_LOCAL_INSTANCE_VERSION"] = package_version()
    credentials = {}
    for role, port in (("API", api_port), ("GATEWAY", gateway_port)):
        directory = credential_directory(instance.root, instance.name, role.lower())
        credentials[role] = issue_credential(directory, launch_id=instance.launch_id)
        env[f"MINDWEFT_LOCAL_{role}_CREDENTIAL_DIR"] = str(directory)
        env[f"MINDWEFT_LOCAL_{role}_ORIGIN"] = f"http://127.0.0.1:{port}"
    apply_coding_workspace_state_defaults(env)
    runner_args = parse_args(
        [
            "--no-env-file",
            *[arg for workspace in workspaces for arg in ("--workspace", str(workspace))],
            "--enable-text",
            "--mcp-gateway",
            "--api-host",
            "127.0.0.1",
            "--bridge-host",
            "127.0.0.1",
            "--api-port",
            str(api_port),
            "--mcp-gateway-port",
            str(gateway_port),
        ]
    )
    env["MINDWEFT_ADMIN_DB_PATH"] = str(instance.state_dir / "personal-setup.db")
    from mindweft_workspace.provisioning import provision_coding_user

    provision_coding_user(
        env,
        token=credentials["API"].token,
        origin=env["MINDWEFT_LOCAL_API_ORIGIN"],
        binding=instance.launch_id,
    )
    plan = prepare_workspace_runtime(runner_args, env, bundled_readonly=True)
    url = f"http://127.0.0.1:{api_port}/console/"
    print(f"Instance: {instance.name} (Mindweft {package_version()})", flush=True)
    print(f"Runtime: {sys.executable}\nCode: {Path(__file__).parent}", flush=True)
    print(f"Instance state root: {instance.state_dir}", flush=True)
    print("Workspaces:", flush=True)
    for workspace in workspaces:
        print(f"  {workspace}", flush=True)
    print("Agent: coding", flush=True)
    print(
        "Access: coding (edit and shell; not a sandbox)"
        if env["MINDWEFT_LOCAL_CODING_TRUSTED"] == "1"
        else "Access: inspect (read-only)",
        flush=True,
    )
    print("Credential-protected local mode; do not expose these ports to a network.", flush=True)
    if args.demo:
        print("Demo provider: mock (no real AI responses).", flush=True)
    else:
        print(
            "Provider configuration checked locally; credentials have not been tested with the provider.",
            flush=True,
        )

    def ready(processes):
        wait_for_code_ready(
            processes,
            api_port=api_port,
            specs=plan.mcp_servers.tenant_specs,
            expected_profile="coding" if env["MINDWEFT_LOCAL_CODING_TRUSTED"] == "1" else "inspect",
            expected_identity={"name": instance.name, "launch_id": instance.launch_id},
            auth_headers={
                "Authorization": f"Bearer {credentials['API'].token}",
                "X-Mindweft-Launch-Id": instance.launch_id,
            },
        )
        record = instance.publish(api_port, gateway_port)
        print(f"Ready: {url}\nPress Ctrl+C to stop.", flush=True)
        if not args.no_open:
            open_console(browser_url(record, credentials["API"]))

    # Avoid importing workspace-local Python modules or picking up project npm config.
    with (
        tempfile.TemporaryDirectory(prefix="mindweft-code-") as process_dir,
        lock_storage(env, shared_oauth=shared_oauth) as storage_fds,
    ):
        return run_workspace_processes(
            env=env,
            mcp_server_specs=plan.mcp_servers.process_specs,
            skip_bridge=False,
            gateway_enabled=True,
            bridge_host="127.0.0.1",
            gateway_port=gateway_port,
            skip_api=False,
            api_host="127.0.0.1",
            api_port=api_port,
            tenant_id=plan.tenant_id,
            workspace=workspaces[0],
            bridge_name=plan.settings.bridge_name,
            text_bridge_name=plan.settings.text_bridge_name,
            shell_bridge_name=None,
            on_started=ready,
            process_cwd=Path(process_dir),
            api_fd=api_socket.fileno(),
            gateway_fd=gateway_socket.fileno(),
            inherited_fds=(instance.fd, *storage_fds),
        )
