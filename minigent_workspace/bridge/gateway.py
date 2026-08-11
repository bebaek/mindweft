from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Sequence

from fastapi import FastAPI, HTTPException, Request, Response

from minigent_mcp.path_policy import MCPPathPolicy
from minigent_workspace.bridge.stdio import (
    DEFAULT_HOST,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DEFAULT_STDIO_STREAM_LIMIT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    BridgeSettings,
    StdioMCPBridge,
)

DEFAULT_GATEWAY_PATH_PREFIX = "/mcp"


class GatewaySettings:
    def __init__(
        self,
        *,
        bridges: list[BridgeSettings],
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        path_prefix: str = DEFAULT_GATEWAY_PATH_PREFIX,
    ) -> None:
        self.bridges = bridges
        self.host = host
        self.port = port
        self.path_prefix = _normalize_path_prefix(path_prefix)


def create_gateway_app(settings: GatewaySettings) -> FastAPI:
    bridges = {
        bridge_settings.name: StdioMCPBridge(bridge_settings)
        for bridge_settings in settings.bridges
    }
    if len(bridges) != len(settings.bridges):
        raise RuntimeError("MCP stdio gateway server names must be unique")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        started: list[StdioMCPBridge] = []
        try:
            for bridge in bridges.values():
                await bridge.start()
                started.append(bridge)
            app.state.bridges = bridges
            yield
        finally:
            for bridge in reversed(started):
                await bridge.stop()

    app = FastAPI(title="Minigent Stdio MCP Gateway", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(f"{settings.path_prefix}/{{server_name}}")
    async def mcp_endpoint(server_name: str, request: Request) -> Response:
        bridge = bridges.get(server_name)
        if bridge is None:
            raise HTTPException(status_code=404, detail=f"Unknown MCP server '{server_name}'")
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON-RPC payload must be an object")
        return await bridge.handle(
            payload, {key.lower(): value for key, value in request.headers.items()}
        )

    return app


def load_gateway_settings(
    path: Path, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> GatewaySettings:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("MCP stdio gateway config must be a JSON object")

    path_prefix = payload.get("path_prefix", payload.get("pathPrefix", DEFAULT_GATEWAY_PATH_PREFIX))
    if not isinstance(path_prefix, str):
        raise RuntimeError("MCP stdio gateway config path_prefix must be a string")

    config_host = payload.get("host", host)
    if not isinstance(config_host, str) or not config_host:
        raise RuntimeError("MCP stdio gateway config host must be a non-empty string")
    config_port = payload.get("port", port)
    if not isinstance(config_port, int):
        raise RuntimeError("MCP stdio gateway config port must be an integer")

    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list):
        raise RuntimeError("MCP stdio gateway config must contain a servers array")

    return GatewaySettings(
        host=config_host,
        port=config_port,
        path_prefix=path_prefix,
        bridges=[bridge_settings_from_mapping(raw_server) for raw_server in raw_servers],
    )


def bridge_settings_from_mapping(raw_server: Any) -> BridgeSettings:
    if not isinstance(raw_server, dict):
        raise RuntimeError("MCP stdio gateway server entries must be objects")

    name = raw_server.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("MCP stdio gateway server entry requires a non-empty name")

    command = raw_server.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise RuntimeError(f"MCP stdio gateway server '{name}' command must be a string array")

    allowed_tools = raw_server.get("allowed_tools", raw_server.get("allowedTools"))
    if allowed_tools is not None and (
        not isinstance(allowed_tools, list)
        or not all(isinstance(item, str) for item in allowed_tools)
    ):
        raise RuntimeError(
            f"MCP stdio gateway server '{name}' allowed_tools must be a string array or null"
        )

    path_policy = raw_server.get("path_policy", raw_server.get("pathPolicy", {}))
    if not isinstance(path_policy, dict):
        raise RuntimeError(f"MCP stdio gateway server '{name}' path_policy must be an object")

    request_timeout = raw_server.get(
        "request_timeout", raw_server.get("requestTimeout", DEFAULT_TIMEOUT_SECONDS)
    )
    if not isinstance(request_timeout, int | float):
        raise RuntimeError(f"MCP stdio gateway server '{name}' request_timeout must be a number")

    stdio_stream_limit = raw_server.get(
        "stdio_stream_limit",
        raw_server.get("stdioStreamLimit", DEFAULT_STDIO_STREAM_LIMIT_BYTES),
    )
    if not isinstance(stdio_stream_limit, int):
        raise RuntimeError(
            f"MCP stdio gateway server '{name}' stdio_stream_limit must be an integer"
        )
    restart_on_timeout = raw_server.get(
        "restart_on_timeout", raw_server.get("restartOnTimeout", False)
    )
    if not isinstance(restart_on_timeout, bool):
        raise RuntimeError(
            f"MCP stdio gateway server '{name}' restart_on_timeout must be a boolean"
        )

    deny_globs = path_policy.get("deny_globs", path_policy.get("denyGlobs", []))
    allow_globs = path_policy.get("allow_globs", path_policy.get("allowGlobs", []))
    if not isinstance(deny_globs, list) or not all(isinstance(item, str) for item in deny_globs):
        raise RuntimeError(
            f"MCP stdio gateway server '{name}' path_policy.deny_globs must be a string array"
        )
    if not isinstance(allow_globs, list) or not all(isinstance(item, str) for item in allow_globs):
        raise RuntimeError(
            f"MCP stdio gateway server '{name}' path_policy.allow_globs must be a string array"
        )
    extra_env = raw_server.get("env", {})
    if not isinstance(extra_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extra_env.items()
    ):
        raise RuntimeError(
            f"MCP stdio gateway server '{name}' env must be an object of string values"
        )

    return BridgeSettings(
        name=name,
        command=list(command),
        path=DEFAULT_PATH,
        request_timeout=float(request_timeout),
        stdio_stream_limit=stdio_stream_limit,
        allowed_tools=list(allowed_tools) if allowed_tools is not None else None,
        path_policy=MCPPathPolicy(deny_globs=list(deny_globs), allow_globs=list(allow_globs)),
        env=dict(extra_env),
        restart_on_timeout=restart_on_timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose multiple stdio MCP servers as one local Streamable HTTP MCP gateway."
    )
    parser.add_argument("--config", required=True, help="JSON gateway config file.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind. Defaults to 127.0.0.1.")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Port to bind. Defaults to 8765."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_gateway_settings(Path(args.config), host=args.host, port=args.port)

    import uvicorn

    uvicorn.run(create_gateway_app(settings), host=settings.host, port=settings.port)


def _normalize_path_prefix(path_prefix: str) -> str:
    if not path_prefix.startswith("/"):
        path_prefix = f"/{path_prefix}"
    return path_prefix.rstrip("/") or DEFAULT_GATEWAY_PATH_PREFIX


if __name__ == "__main__":
    main()
