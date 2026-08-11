"""Compatibility imports for the workspace stdio MCP bridge.

The canonical implementation lives in :mod:`minigent_workspace.bridge.stdio`.
"""

from minigent_workspace.bridge.stdio import (
    DEFAULT_HOST,
    DEFAULT_PATH,
    DEFAULT_PORT,
    DEFAULT_STDIO_STREAM_LIMIT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_LEGACY_SESSIONS,
    BridgeSettings,
    StdioMCPBridge,
    build_parser,
    create_bridge_app,
    main,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PATH",
    "DEFAULT_PORT",
    "DEFAULT_STDIO_STREAM_LIMIT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_LEGACY_SESSIONS",
    "BridgeSettings",
    "StdioMCPBridge",
    "build_parser",
    "create_bridge_app",
    "main",
]


if __name__ == "__main__":
    main()
