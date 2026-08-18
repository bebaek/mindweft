from __future__ import annotations

import argparse
from dataclasses import dataclass

from mindweft_config.unified_config import normalize_mindweft_env
from mindweft_workspace.mcp_specs import (
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_MCP_GATEWAY_PATH_PREFIX,
    env_flag_enabled,
    normalize_path_prefix,
)
from mindweft_workspace.tenant_config import (
    DEFAULT_SHELL_BRIDGE_NAME,
    DEFAULT_SHELL_BRIDGE_PORT,
    DEFAULT_TEXT_BRIDGE_NAME,
    DEFAULT_TEXT_BRIDGE_PORT,
)

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_BRIDGE_NAME = "fs-workspace"


@dataclass(frozen=True)
class WorkspaceRuntimeSettings:
    api_host: str
    api_port: int
    bridge_host: str
    bridge_port: int
    bridge_name: str
    bridge_url: str
    gateway_enabled: bool
    gateway_port: int
    gateway_path_prefix: str
    gateway_url_prefix: str
    text_enabled: bool
    text_bridge_name: str
    text_bridge_port: int
    text_bridge_url: str
    shell_enabled: bool
    shell_bridge_name: str
    shell_bridge_port: int
    shell_bridge_url: str


def resolve_workspace_runtime_settings(
    args: argparse.Namespace, env: dict[str, str]
) -> WorkspaceRuntimeSettings:
    normalize_mindweft_env(env)
    api_host = args.api_host or env.get("MINIGENT_HOST") or DEFAULT_API_HOST
    api_port = args.api_port or int(env.get("MINIGENT_PORT") or DEFAULT_API_PORT)
    bridge_host = args.bridge_host or env.get("MINIGENT_CODING_BRIDGE_HOST") or DEFAULT_BRIDGE_HOST
    bridge_port = args.bridge_port or int(
        env.get("MINIGENT_CODING_BRIDGE_PORT") or DEFAULT_BRIDGE_PORT
    )
    bridge_name = args.bridge_name or env.get("MINIGENT_CODING_BRIDGE_NAME") or DEFAULT_BRIDGE_NAME
    bridge_url = f"http://{bridge_host}:{bridge_port}/mcp"
    gateway_enabled = args.mcp_gateway or env_flag_enabled(
        env.get("MINIGENT_CODING_MCP_GATEWAY_ENABLED")
    )
    gateway_port = args.mcp_gateway_port or int(
        env.get("MINIGENT_CODING_MCP_GATEWAY_PORT") or bridge_port
    )
    gateway_path_prefix = (
        args.mcp_gateway_path_prefix
        or env.get("MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX")
        or DEFAULT_MCP_GATEWAY_PATH_PREFIX
    )
    gateway_url_prefix = (
        f"http://{bridge_host}:{gateway_port}{normalize_path_prefix(gateway_path_prefix)}"
    )
    text_enabled = args.enable_text or env_flag_enabled(env.get("MINIGENT_CODING_TEXT_ENABLED"))
    text_bridge_name = (
        args.text_bridge_name
        or env.get("MINIGENT_CODING_TEXT_BRIDGE_NAME")
        or DEFAULT_TEXT_BRIDGE_NAME
    )
    text_bridge_port = args.text_bridge_port or int(
        env.get("MINIGENT_CODING_TEXT_BRIDGE_PORT") or DEFAULT_TEXT_BRIDGE_PORT
    )
    text_bridge_url = f"http://{bridge_host}:{text_bridge_port}/mcp"
    shell_enabled = args.enable_shell or env_flag_enabled(env.get("MINIGENT_CODING_SHELL_ENABLED"))
    shell_bridge_name = (
        args.shell_bridge_name
        or env.get("MINIGENT_CODING_SHELL_BRIDGE_NAME")
        or DEFAULT_SHELL_BRIDGE_NAME
    )
    shell_bridge_port = args.shell_bridge_port or int(
        env.get("MINIGENT_CODING_SHELL_BRIDGE_PORT") or DEFAULT_SHELL_BRIDGE_PORT
    )
    shell_bridge_url = f"http://{bridge_host}:{shell_bridge_port}/mcp"
    return WorkspaceRuntimeSettings(
        api_host=api_host,
        api_port=api_port,
        bridge_host=bridge_host,
        bridge_port=bridge_port,
        bridge_name=bridge_name,
        bridge_url=bridge_url,
        gateway_enabled=gateway_enabled,
        gateway_port=gateway_port,
        gateway_path_prefix=gateway_path_prefix,
        gateway_url_prefix=gateway_url_prefix,
        text_enabled=text_enabled,
        text_bridge_name=text_bridge_name,
        text_bridge_port=text_bridge_port,
        text_bridge_url=text_bridge_url,
        shell_enabled=shell_enabled,
        shell_bridge_name=shell_bridge_name,
        shell_bridge_port=shell_bridge_port,
        shell_bridge_url=shell_bridge_url,
    )
