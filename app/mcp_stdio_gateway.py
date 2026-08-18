"""Compatibility imports for the workspace stdio MCP gateway.

The canonical implementation lives in :mod:`minigent_workspace.bridge.gateway`.
"""

from mindweft_workspace.bridge.gateway import (
    DEFAULT_GATEWAY_PATH_PREFIX,
    GatewaySettings,
    bridge_settings_from_mapping,
    build_parser,
    create_gateway_app,
    load_gateway_settings,
    main,
)

__all__ = [
    "DEFAULT_GATEWAY_PATH_PREFIX",
    "GatewaySettings",
    "bridge_settings_from_mapping",
    "build_parser",
    "create_gateway_app",
    "load_gateway_settings",
    "main",
]


if __name__ == "__main__":
    main()
