from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from minigent_config.unified_config import normalize_mindweft_env

DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 8765
DEFAULT_MCP_GATEWAY_PATH_PREFIX = "/mcp"

_ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class CodingMCPServerSpec:
    """Declarative MCP server entry for the coding workspace runner.

    Stdio servers are launched behind Mindweft's stdio-to-HTTP bridge or gateway. HTTP
    servers are registered in tenant config; when ``managed`` is true, the runner also
    starts their ``command`` as a child process and can wait on ``health_url`` before
    starting the Mindweft API.

    For stdio servers, ``host``/``port``/``path`` describe the compatibility mode where
    the runner starts one HTTP bridge per stdio server. They are not used by the shared
    stdio gateway; gateway tenant URLs are derived from the gateway bind address and the
    server name.
    """

    def __init__(
        self,
        *,
        name: str,
        url: str,
        transport: str = "stdio",
        command: list[str] | None = None,
        host: str = DEFAULT_BRIDGE_HOST,
        port: int = DEFAULT_BRIDGE_PORT,
        path: str = "/mcp",
        profiles: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        path_policy: dict[str, list[str]] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        managed: bool = False,
        health_url: str | None = None,
        startup_timeout_seconds: float = 30.0,
        request_timeout: float = 30.0,
        timeout_seconds: float = 30.0,
        restart_on_timeout: bool = False,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.url = url
        self.transport = transport
        self.command = command
        self.host = host
        self.port = port
        self.path = path
        self.profiles = profiles or ["inspect"]
        self.allowed_tools = allowed_tools
        self.path_policy = path_policy or {}
        self.env = env or {}
        self.headers = headers or {}
        self.managed = managed
        self.health_url = health_url
        self.startup_timeout_seconds = startup_timeout_seconds
        self.request_timeout = request_timeout
        self.timeout_seconds = timeout_seconds
        self.restart_on_timeout = restart_on_timeout
        self.enabled = enabled


def env_flag_enabled(value: str | None) -> bool:
    return value is not None and value.lower() not in {"", "0", "false", "no"}


def interpolate_config_string(value: str, env: dict[str, str]) -> str:
    """Replace ${NAME} placeholders in declarative MCP config strings."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return env.get(name, "")

    return _ENV_PLACEHOLDER_PATTERN.sub(replace, value)


def normalize_path_prefix(path_prefix: str) -> str:
    if not path_prefix.startswith("/"):
        path_prefix = f"/{path_prefix}"
    return path_prefix.rstrip("/") or DEFAULT_MCP_GATEWAY_PATH_PREFIX


def mcp_server_specs_for_gateway(
    specs: list[CodingMCPServerSpec], gateway_url_prefix: str
) -> list[CodingMCPServerSpec]:
    normalized_prefix = gateway_url_prefix.rstrip("/")
    transformed: list[CodingMCPServerSpec] = []
    for spec in specs:
        if spec.transport == "stdio":
            transformed.append(
                CodingMCPServerSpec(
                    name=spec.name,
                    url=f"{normalized_prefix}/{spec.name}",
                    transport=spec.transport,
                    command=spec.command,
                    host=spec.host,
                    port=spec.port,
                    path=spec.path,
                    profiles=list(spec.profiles),
                    allowed_tools=list(spec.allowed_tools)
                    if spec.allowed_tools is not None
                    else None,
                    path_policy={key: list(value) for key, value in spec.path_policy.items()},
                    env=dict(spec.env),
                    headers=dict(spec.headers),
                    managed=spec.managed,
                    health_url=spec.health_url,
                    startup_timeout_seconds=spec.startup_timeout_seconds,
                    request_timeout=spec.request_timeout,
                    timeout_seconds=spec.timeout_seconds,
                    restart_on_timeout=spec.restart_on_timeout,
                    enabled=spec.enabled,
                )
            )
            continue
        transformed.append(spec)
    return transformed


def mcp_gateway_config_from_specs(specs: list[CodingMCPServerSpec]) -> dict[str, Any]:
    servers: list[dict[str, Any]] = []
    for spec in specs:
        if spec.transport != "stdio":
            continue
        if spec.command is None:
            raise RuntimeError(f"MCP server '{spec.name}' requires a command")
        server: dict[str, Any] = {
            "name": spec.name,
            "command": spec.command,
            "request_timeout": spec.request_timeout,
        }
        if spec.restart_on_timeout:
            server["restart_on_timeout"] = True
        if spec.allowed_tools is not None:
            server["allowed_tools"] = spec.allowed_tools
        if spec.path_policy:
            server["path_policy"] = spec.path_policy
        if spec.env:
            server["env"] = spec.env
        servers.append(server)
    return {"servers": servers}


def write_mcp_gateway_config(specs: list[CodingMCPServerSpec]) -> Path:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="mindweft-mcp-gateway-",
        suffix=".json",
        delete=False,
    ) as file:
        json.dump(mcp_gateway_config_from_specs(specs), file, separators=(",", ":"))
        file.write("\n")
        return Path(file.name)


def resolve_mcp_servers_file(
    cli_path: str | None, env: dict[str, str], *, base_dir: Path | None = None
) -> Path | None:
    normalize_mindweft_env(env)
    raw_path = cli_path or env.get("MINIGENT_CODING_MCP_SERVERS_FILE")
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()


def load_coding_mcp_server_specs(
    path: Path,
    *,
    bridge_host: str,
    workspace_roots: list[Path],
    env: dict[str, str] | None = None,
) -> list[CodingMCPServerSpec]:
    return load_coding_mcp_server_specs_from_json(
        path.read_text(encoding="utf-8"),
        bridge_host=bridge_host,
        workspace_roots=workspace_roots,
        env=env,
    )


def load_coding_mcp_server_specs_from_json(
    raw_json: str,
    *,
    bridge_host: str,
    workspace_roots: list[Path],
    env: dict[str, str] | None = None,
) -> list[CodingMCPServerSpec]:
    payload = json.loads(raw_json)
    raw_servers = payload.get("servers") if isinstance(payload, dict) else payload
    if not isinstance(raw_servers, list):
        raise RuntimeError('coding.mcp_server_specs must be a JSON array or {"servers": [...]}')

    specs: list[CodingMCPServerSpec] = []
    interpolation_env = dict(env or os.environ)
    normalize_mindweft_env(interpolation_env)
    for index, raw_server in enumerate(raw_servers):
        if not isinstance(raw_server, dict):
            raise RuntimeError("each coding MCP server entry must be an object")
        specs.append(
            coding_mcp_server_spec_from_mapping(
                raw_server,
                default_host=bridge_host,
                default_port=DEFAULT_BRIDGE_PORT + index,
                workspace_roots=workspace_roots,
                env=interpolation_env,
            )
        )
    return [spec for spec in specs if spec.enabled]


def coding_mcp_server_spec_from_mapping(
    raw_server: dict[str, Any],
    *,
    default_host: str,
    default_port: int,
    workspace_roots: list[Path],
    env: dict[str, str],
) -> CodingMCPServerSpec:
    name = raw_server.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("coding MCP server entry requires a non-empty name")

    transport = raw_server.get("transport", "stdio")
    if transport not in {"stdio", "http"}:
        raise RuntimeError(f"coding MCP server '{name}' has unsupported transport '{transport}'")

    managed = env_flag_enabled(str(raw_server.get("managed", "false")))
    if transport == "stdio":
        managed = False

    host = raw_server.get("host", default_host)
    if not isinstance(host, str) or not host:
        raise RuntimeError(f"coding MCP server '{name}' has invalid host")
    port = raw_server.get("port")
    if port is None:
        port = default_port
    if not isinstance(port, int):
        raise RuntimeError(f"coding MCP server '{name}' has invalid port")
    path = raw_server.get("path", "/mcp")
    if not isinstance(path, str) or not path.startswith("/"):
        raise RuntimeError(f"coding MCP server '{name}' has invalid path")
    url = raw_server.get("url") or f"http://{host}:{port}{path}"
    if not isinstance(url, str) or not url:
        raise RuntimeError(f"coding MCP server '{name}' has invalid url")
    url = interpolate_config_string(url, env)

    command = raw_server.get("command")
    if command is not None:
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise RuntimeError(f"coding MCP server '{name}' command must be a string array")
        command = expand_coding_mcp_command(
            [interpolate_config_string(item, env) for item in command], workspace_roots
        )
    elif transport == "stdio" or managed:
        raise RuntimeError(
            f"coding MCP server '{name}' requires command for managed or stdio transport"
        )

    allowed_tools = raw_server.get("allowed_tools", raw_server.get("allowedTools"))
    if allowed_tools is not None and (
        not isinstance(allowed_tools, list)
        or not all(isinstance(item, str) for item in allowed_tools)
    ):
        raise RuntimeError(
            f"coding MCP server '{name}' allowed_tools must be a string array or null"
        )

    path_policy = raw_server.get("path_policy", raw_server.get("pathPolicy", {}))
    if not isinstance(path_policy, dict):
        raise RuntimeError(f"coding MCP server '{name}' path_policy must be an object")

    profiles = raw_server.get("profiles", ["inspect"])
    if not isinstance(profiles, list) or not all(
        isinstance(item, str) and item for item in profiles
    ):
        raise RuntimeError(f"coding MCP server '{name}' profiles must be a non-empty string array")

    extra_env = raw_server.get("env", {})
    if not isinstance(extra_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extra_env.items()
    ):
        raise RuntimeError(f"coding MCP server '{name}' env must be an object of string values")
    extra_env = {key: interpolate_config_string(value, env) for key, value in extra_env.items()}

    headers = raw_server.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise RuntimeError(f"coding MCP server '{name}' headers must be an object of string values")
    headers = {key: interpolate_config_string(value, env) for key, value in headers.items()}

    health_url = raw_server.get("health_url", raw_server.get("healthUrl"))
    if health_url is not None:
        if not isinstance(health_url, str) or not health_url:
            raise RuntimeError(f"coding MCP server '{name}' health_url must be a non-empty string")
        health_url = interpolate_config_string(health_url, env)

    startup_timeout_seconds = raw_server.get(
        "startup_timeout_seconds", raw_server.get("startupTimeoutSeconds", 30.0)
    )
    if not isinstance(startup_timeout_seconds, int | float) or startup_timeout_seconds < 0:
        raise RuntimeError(
            f"coding MCP server '{name}' startup_timeout_seconds must be a non-negative number"
        )
    request_timeout = raw_server.get("request_timeout", raw_server.get("requestTimeout", 30.0))
    if not isinstance(request_timeout, int | float) or request_timeout <= 0:
        raise RuntimeError(f"coding MCP server '{name}' request_timeout must be a positive number")
    timeout_seconds = raw_server.get(
        "timeout_seconds", raw_server.get("timeoutSeconds", request_timeout)
    )
    if not isinstance(timeout_seconds, int | float) or timeout_seconds <= 0:
        raise RuntimeError(f"coding MCP server '{name}' timeout_seconds must be a positive number")
    restart_on_timeout = raw_server.get(
        "restart_on_timeout", raw_server.get("restartOnTimeout", False)
    )
    if not isinstance(restart_on_timeout, bool):
        raise RuntimeError(f"coding MCP server '{name}' restart_on_timeout must be a boolean")

    return CodingMCPServerSpec(
        name=name,
        url=url,
        transport=transport,
        command=command,
        host=host,
        port=port,
        path=path,
        profiles=list(profiles),
        allowed_tools=list(allowed_tools) if allowed_tools is not None else None,
        path_policy={
            key: list(value) for key, value in path_policy.items() if isinstance(value, list)
        },
        env=dict(extra_env),
        headers=dict(headers),
        managed=managed,
        health_url=health_url,
        startup_timeout_seconds=float(startup_timeout_seconds),
        request_timeout=float(request_timeout),
        timeout_seconds=float(timeout_seconds),
        restart_on_timeout=restart_on_timeout,
        enabled=env_flag_enabled(str(raw_server.get("enabled", "true"))),
    )


def expand_coding_mcp_command(command: list[str], workspace_roots: list[Path]) -> list[str]:
    expanded: list[str] = []
    first_workspace = str(workspace_roots[0])
    workspace_roots_csv = ",".join(str(workspace) for workspace in workspace_roots)
    index = 0
    while index < len(command):
        item = command[index]
        if item == "{workspace_roots}":
            expanded.extend(str(workspace) for workspace in workspace_roots)
            index += 1
            continue
        if item == "{workspace_args}":
            for workspace in workspace_roots:
                expanded.extend(["--workspace", str(workspace)])
            index += 1
            continue
        if (
            item == "--workspace"
            and index + 1 < len(command)
            and command[index + 1] == "{workspace}"
        ):
            for workspace in workspace_roots:
                expanded.extend(["--workspace", str(workspace)])
            index += 2
            continue
        expanded.append(
            item.replace("{workspace}", first_workspace).replace(
                "{workspace_roots_csv}", workspace_roots_csv
            )
        )
        index += 1
    return expanded
