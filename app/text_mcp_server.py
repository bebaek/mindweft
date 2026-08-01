from __future__ import annotations

import argparse
import json
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from app.mcp import (
    LEGACY_MCP_PROTOCOL_VERSION,
    MODERN_MCP_PROTOCOL_VERSION,
    build_mcp_discover_result,
    mcp_request_protocol_version,
    stamp_modern_mcp_result,
)

DEFAULT_MAX_CHARS = 40_000
DEFAULT_MAX_MATCHES = 20

READ_TEXT_FILE_LINES_TOOL = {
    "name": "read_text_file_lines",
    "description": "Read an exact inclusive line range from a text file under the configured workspace roots.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or workspace-relative file path."},
            "start_line": {
                "type": "integer",
                "description": "1-based first line to read, inclusive.",
            },
            "end_line": {
                "type": "integer",
                "description": "1-based last line to read, inclusive.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Optional maximum characters returned in content.",
            },
        },
        "required": ["path", "start_line", "end_line"],
    },
}

READ_TEXT_FILE_AROUND_TOOL = {
    "name": "read_text_file_around",
    "description": "Read text around a 1-based line number from a file under the configured workspace roots.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or workspace-relative file path."},
            "line": {"type": "integer", "description": "1-based center line."},
            "before": {
                "type": "integer",
                "description": "Lines of context before the center line.",
            },
            "after": {"type": "integer", "description": "Lines of context after the center line."},
            "max_chars": {
                "type": "integer",
                "description": "Optional maximum characters returned in content.",
            },
        },
        "required": ["path", "line"],
    },
}

SEARCH_TEXT_FILE_TOOL = {
    "name": "search_text_file",
    "description": "Search a text file for a literal string or regex and return matching line contexts.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or workspace-relative file path."},
            "pattern": {"type": "string", "description": "Pattern to search for."},
            "regex": {
                "type": "boolean",
                "description": "Treat pattern as a Python regular expression. Defaults to false.",
            },
            "before": {"type": "integer", "description": "Lines of context before each match."},
            "after": {"type": "integer", "description": "Lines of context after each match."},
            "max_matches": {"type": "integer", "description": "Maximum matches to return."},
            "max_chars": {
                "type": "integer",
                "description": "Optional maximum characters returned across snippets.",
            },
        },
        "required": ["path", "pattern"],
    },
}

TOOLS = [READ_TEXT_FILE_LINES_TOOL, READ_TEXT_FILE_AROUND_TOOL, SEARCH_TEXT_FILE_TOOL]


class _ShutdownRequested(BaseException):
    """Raised by signal handlers to exit the stdio server without a traceback."""


@dataclass(frozen=True)
class LineSlice:
    start_line: int
    end_line: int
    content: str
    truncated: bool


class TextMCPServer:
    def __init__(
        self,
        *,
        workspace: Path | None = None,
        workspaces: Sequence[Path] | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        raw_workspaces = list(workspaces or ([] if workspace is None else [workspace]))
        if not raw_workspaces:
            raise RuntimeError("at least one workspace root is required")
        self.workspaces = tuple(path.expanduser().resolve() for path in raw_workspaces)
        self.workspace = self.workspaces[0]
        self.max_chars = max_chars
        for workspace_root in self.workspaces:
            if not workspace_root.exists() or not workspace_root.is_dir():
                raise RuntimeError(
                    f"workspace does not exist or is not a directory: {workspace_root}"
                )

    def handle(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        request_id = payload.get("id")
        method = payload.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "JSON-RPC payload must include method")
        if request_id is None:
            return None
        try:
            if method == "server/discover":
                return self._result(
                    request_id,
                    build_mcp_discover_result(
                        server_name="minigent-text-mcp",
                        capabilities={"tools": {}},
                    ),
                )
            if method == "initialize":
                return self._result(
                    request_id,
                    {
                        "protocolVersion": LEGACY_MCP_PROTOCOL_VERSION,
                        "serverInfo": {"name": "minigent-text-mcp", "version": "0.1.0"},
                        "capabilities": {"tools": {}},
                    },
                )
            if method == "tools/list":
                result: dict[str, Any] = {"tools": TOOLS}
                if mcp_request_protocol_version(payload) == MODERN_MCP_PROTOCOL_VERSION:
                    result.update({"ttlMs": 0, "cacheScope": "private"})
                    result = stamp_modern_mcp_result(
                        result,
                        server_name="minigent-text-mcp",
                    )
                return self._result(request_id, result)
            if method == "tools/call":
                response = self._handle_tool_call(request_id, payload.get("params"))
                if mcp_request_protocol_version(
                    payload
                ) == MODERN_MCP_PROTOCOL_VERSION and isinstance(response.get("result"), dict):
                    response["result"] = stamp_modern_mcp_result(
                        response["result"],
                        server_name="minigent-text-mcp",
                    )
                return response
            return self._error(request_id, -32601, f"unknown method: {method}")
        except Exception as exc:
            return self._error(request_id, -32000, str(exc))

    def _handle_tool_call(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "tools/call params must be an object")
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "tool arguments must be an object")
        if tool_name == "read_text_file_lines":
            result = self.read_text_file_lines(arguments)
        elif tool_name == "read_text_file_around":
            result = self.read_text_file_around(arguments)
        elif tool_name == "search_text_file":
            result = self.search_text_file(arguments)
        else:
            return self._error(request_id, -32602, "unknown tool")
        return self._result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True)}],
                "structuredContent": result,
            },
        )

    def read_text_file_lines(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(arguments.get("path"))
        start_line = self._int_argument(arguments.get("start_line"), "start_line", minimum=1)
        end_line = self._int_argument(arguments.get("end_line"), "end_line", minimum=1)
        if end_line < start_line:
            raise ValueError("end_line must be >= start_line")
        max_chars = self._optional_int_argument(
            arguments.get("max_chars"), self.max_chars, minimum=1
        )
        lines = self._read_lines(path)
        line_slice = _slice_lines(lines, start_line, end_line, max_chars=max_chars)
        return {
            "path": str(path),
            "start_line": line_slice.start_line,
            "end_line": line_slice.end_line,
            "requested_start_line": start_line,
            "requested_end_line": end_line,
            "line_count": len(lines),
            "content": line_slice.content,
            "truncated": line_slice.truncated,
        }

    def read_text_file_around(self, arguments: dict[str, Any]) -> dict[str, Any]:
        line = self._int_argument(arguments.get("line"), "line", minimum=1)
        before = self._optional_int_argument(arguments.get("before"), 5, minimum=0)
        after = self._optional_int_argument(arguments.get("after"), 5, minimum=0)
        derived = dict(arguments)
        derived["start_line"] = max(1, line - before)
        derived["end_line"] = line + after
        result = self.read_text_file_lines(derived)
        result["line"] = line
        result["before"] = before
        result["after"] = after
        return result

    def search_text_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(arguments.get("path"))
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("search_text_file requires a non-empty pattern")
        use_regex = arguments.get("regex") is True
        before = self._optional_int_argument(arguments.get("before"), 0, minimum=0)
        after = self._optional_int_argument(arguments.get("after"), 0, minimum=0)
        max_matches = self._optional_int_argument(
            arguments.get("max_matches"), DEFAULT_MAX_MATCHES, minimum=1
        )
        max_chars = self._optional_int_argument(
            arguments.get("max_chars"), self.max_chars, minimum=1
        )
        matcher = re.compile(pattern) if use_regex else re.compile(re.escape(pattern))
        lines = self._read_lines(path)
        matches: list[dict[str, Any]] = []
        total_content = 0
        truncated = False
        for index, line_text in enumerate(lines, start=1):
            if matcher.search(line_text) is None:
                continue
            line_slice = _slice_lines(
                lines,
                max(1, index - before),
                index + after,
                max_chars=max(max_chars - total_content, 0),
            )
            total_content += len(line_slice.content)
            matches.append(
                {
                    "line": index,
                    "start_line": line_slice.start_line,
                    "end_line": line_slice.end_line,
                    "content": line_slice.content,
                    "truncated": line_slice.truncated,
                }
            )
            if line_slice.truncated or len(matches) >= max_matches or total_content >= max_chars:
                truncated = line_slice.truncated or total_content >= max_chars
                break
        return {
            "path": str(path),
            "pattern": pattern,
            "regex": use_regex,
            "line_count": len(lines),
            "matches": matches,
            "match_count": len(matches),
            "truncated": truncated,
        }

    def _resolve_file(self, raw_path: Any) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty string")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        path = candidate.resolve()
        if not any(path == workspace or workspace in path.parents for workspace in self.workspaces):
            roots = ", ".join(str(workspace) for workspace in self.workspaces)
            raise ValueError(f"path must be inside a workspace root: {roots}")
        if not path.exists() or not path.is_file():
            raise ValueError(f"path does not exist or is not a file: {path}")
        return path

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        try:
            return path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise ValueError(f"file is not valid UTF-8 text: {path}") from exc

    @staticmethod
    def _int_argument(value: Any, name: str, *, minimum: int) -> int:
        if not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return value

    @classmethod
    def _optional_int_argument(cls, value: Any, default: int, *, minimum: int) -> int:
        if value is None:
            return default
        return cls._int_argument(value, "numeric argument", minimum=minimum)

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _slice_lines(lines: list[str], start_line: int, end_line: int, *, max_chars: int) -> LineSlice:
    actual_start = min(start_line, len(lines) + 1)
    actual_end = min(end_line, len(lines))
    if actual_start > actual_end:
        return LineSlice(actual_start, actual_end, "", False)
    selected = lines[actual_start - 1 : actual_end]
    content = "".join(selected)
    if len(content) <= max_chars:
        return LineSlice(actual_start, actual_end, content, False)
    if max_chars <= 20:
        return LineSlice(actual_start, actual_end, content[:max_chars], True)
    marker = "\n...<truncated>"
    return LineSlice(actual_start, actual_end, content[: max_chars - len(marker)] + marker, True)


def _request_shutdown(_signum: int, _frame: Any) -> None:
    raise _ShutdownRequested


def _install_shutdown_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)


def serve_stdio(server: TextMCPServer) -> int:
    _install_shutdown_signal_handlers()
    try:
        for line in sys.stdin:
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    response = TextMCPServer._error(
                        None, -32600, "JSON-RPC payload must be an object"
                    )
                else:
                    response = server.handle(payload)
            except json.JSONDecodeError:
                response = TextMCPServer._error(None, -32700, "invalid JSON")
            if response is None:
                continue
            print(json.dumps(response, ensure_ascii=True, separators=(",", ":")), flush=True)
    except (KeyboardInterrupt, _ShutdownRequested):
        return 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Workspace-scoped targeted text-read MCP server.")
    parser.add_argument(
        "--workspace",
        action="append",
        required=True,
        help="Workspace root files may be read under. Repeat to allow multiple roots.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="Default maximum text characters returned by each tool.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = TextMCPServer(
        workspaces=[Path(workspace) for workspace in args.workspace],
        max_chars=args.max_chars,
    )
    return serve_stdio(server)


if __name__ == "__main__":
    raise SystemExit(main())
