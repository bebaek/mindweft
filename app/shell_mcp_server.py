"""Compatibility imports for the workspace shell MCP server.

The canonical implementation lives in :mod:`minigent_workspace.servers.shell`.
"""

from mindweft_workspace.servers.shell import (
    DEFAULT_ENV_ALLOWLIST,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_TIMEOUT_SECONDS,
    RUN_COMMAND_DESCRIPTION,
    ShellMCPServer,
    build_parser,
    build_shell_sdk_server,
    main,
    serve_stdio,
)

__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "RUN_COMMAND_DESCRIPTION",
    "ShellMCPServer",
    "build_parser",
    "build_shell_sdk_server",
    "main",
    "serve_stdio",
]


if __name__ == "__main__":
    raise SystemExit(main())
