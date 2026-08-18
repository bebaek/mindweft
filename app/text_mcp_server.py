"""Compatibility imports for the workspace text MCP server.

The canonical implementation lives in :mod:`minigent_workspace.servers.text`.
"""

from mindweft_workspace.servers.text import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_MATCHES,
    READ_TEXT_FILE_AROUND_DESCRIPTION,
    READ_TEXT_FILE_LINES_DESCRIPTION,
    SEARCH_TEXT_FILE_DESCRIPTION,
    LineSlice,
    TextMCPServer,
    build_parser,
    build_text_sdk_server,
    main,
)

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_MATCHES",
    "READ_TEXT_FILE_AROUND_DESCRIPTION",
    "READ_TEXT_FILE_LINES_DESCRIPTION",
    "SEARCH_TEXT_FILE_DESCRIPTION",
    "LineSlice",
    "TextMCPServer",
    "build_parser",
    "build_text_sdk_server",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
