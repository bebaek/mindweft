from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from app.mcp import DEFAULT_MCP_PROTOCOL_VERSION

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_CHARS = 12_000
DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "UV_CACHE_DIR",
    "XDG_CACHE_HOME",
)

RUN_COMMAND_TOOL = {
    "name": "run_command",
    "description": "Run a non-interactive shell command in the configured workspace.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run."},
            "cwd": {
                "type": "string",
                "description": "Working directory. Must be inside one configured workspace root.",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Optional command timeout in seconds.",
            },
            "max_output_chars": {
                "type": "integer",
                "description": "Optional maximum characters kept for stdout and stderr each.",
            },
        },
        "required": ["command"],
    },
}


class _ShutdownRequested(BaseException):
    """Raised by signal handlers to exit the stdio server without a traceback."""


_ACTIVE_COMMAND_PROCESSES: set[subprocess.Popen[str]] = set()


def _request_shutdown(_signum: int, _frame: Any) -> None:
    for process in tuple(_ACTIVE_COMMAND_PROCESSES):
        if process.poll() is None:
            _terminate_process_group(process)
    raise _ShutdownRequested


def _install_shutdown_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)


class ShellMCPServer:
    def __init__(
        self,
        *,
        workspace: Path | None = None,
        workspaces: Sequence[Path] | None = None,
        shell: str = "/bin/sh",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        env_allowlist: Sequence[str] = DEFAULT_ENV_ALLOWLIST,
        allowed_command_prefixes: Sequence[str] | None = None,
    ) -> None:
        raw_workspaces = list(workspaces or ([] if workspace is None else [workspace]))
        if not raw_workspaces:
            raise RuntimeError("at least one workspace root is required")
        self.workspaces = tuple(path.expanduser().resolve() for path in raw_workspaces)
        self.workspace = self.workspaces[0]
        self.shell = shell
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.env_allowlist = tuple(env_allowlist)
        self.allowed_command_prefixes = tuple(
            prefix.strip() for prefix in (allowed_command_prefixes or ()) if prefix.strip()
        )
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
            if method == "initialize":
                return self._result(
                    request_id,
                    {
                        "protocolVersion": DEFAULT_MCP_PROTOCOL_VERSION,
                        "serverInfo": {"name": "minigent-shell-mcp", "version": "0.1.0"},
                        "capabilities": {"tools": {}},
                    },
                )
            if method == "tools/list":
                return self._result(request_id, {"tools": [RUN_COMMAND_TOOL]})
            if method == "tools/call":
                return self._handle_tool_call(request_id, payload.get("params"))
            return self._error(request_id, -32601, f"unknown method: {method}")
        except Exception as exc:
            return self._error(request_id, -32000, str(exc))

    def _handle_tool_call(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "tools/call params must be an object")
        if params.get("name") != "run_command":
            return self._error(request_id, -32602, "unknown tool")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "tool arguments must be an object")
        result = self.run_command(arguments)
        return self._result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True)}],
                "structuredContent": result,
            },
        )

    def run_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("run_command requires a non-empty command")
        command = command.strip()
        self._validate_command_allowed(command)
        cwd = self._resolve_cwd(arguments.get("cwd"))
        timeout_seconds = self._number_argument(
            arguments.get("timeout_seconds"), self.timeout_seconds, minimum=0.1
        )
        max_output_chars = int(
            self._number_argument(
                arguments.get("max_output_chars"), self.max_output_chars, minimum=1
            )
        )
        env = {key: os.environ[key] for key in self.env_allowlist if key in os.environ}
        started_at = time.perf_counter()
        process = subprocess.Popen(
            [self.shell, "-lc", command],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        _ACTIVE_COMMAND_PROCESSES.add(process)
        timed_out = False
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
                stdout, stderr = process.communicate()
        finally:
            _ACTIVE_COMMAND_PROCESSES.discard(process)
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        stdout_text, stdout_truncated = _truncate_text(stdout or "", max_output_chars)
        stderr_text, stderr_truncated = _truncate_text(stderr or "", max_output_chars)
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }

    def _validate_command_allowed(self, command: str) -> None:
        if not self.allowed_command_prefixes:
            return
        if any(
            _command_matches_prefix(command, prefix) for prefix in self.allowed_command_prefixes
        ):
            return
        raise ValueError("command is not allowed by shell MCP command prefix policy")

    def _resolve_cwd(self, raw_cwd: Any) -> Path:
        if raw_cwd is None or raw_cwd == "":
            cwd = self.workspace
        elif isinstance(raw_cwd, str):
            cwd = Path(raw_cwd).expanduser().resolve()
        else:
            raise ValueError("cwd must be a string")
        if not cwd.exists() or not cwd.is_dir():
            raise ValueError(f"cwd does not exist or is not a directory: {cwd}")
        if not any(cwd == workspace or workspace in cwd.parents for workspace in self.workspaces):
            roots = ", ".join(str(workspace) for workspace in self.workspaces)
            raise ValueError(f"cwd must be inside a workspace root: {roots}")
        return cwd

    @staticmethod
    def _number_argument(value: Any, default: float, *, minimum: float) -> float:
        if value is None:
            number = default
        elif isinstance(value, int | float):
            number = float(value)
        else:
            raise ValueError("numeric argument has invalid type")
        if number < minimum:
            raise ValueError(f"numeric argument must be >= {minimum}")
        return number

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2.0)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _command_matches_prefix(command: str, prefix: str) -> bool:
    return command == prefix or command.startswith(f"{prefix} ")


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 20:
        return text[:max_chars], True
    marker = "\n...<truncated>"
    return text[: max_chars - len(marker)] + marker, True


def serve_stdio(server: ShellMCPServer) -> int:
    _install_shutdown_signal_handlers()
    try:
        for line in sys.stdin:
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    response = ShellMCPServer._error(
                        None, -32600, "JSON-RPC payload must be an object"
                    )
                else:
                    response = server.handle(payload)
            except json.JSONDecodeError:
                response = ShellMCPServer._error(None, -32700, "invalid JSON")
            if response is None:
                continue
            print(json.dumps(response, ensure_ascii=True, separators=(",", ":")), flush=True)
    except (KeyboardInterrupt, _ShutdownRequested):
        return 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Workspace-scoped shell command MCP server.")
    parser.add_argument(
        "--workspace",
        action="append",
        required=True,
        help="Workspace root commands may run under. Repeat to allow multiple roots.",
    )
    parser.add_argument("--shell", default="/bin/sh", help="Shell executable.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Default command timeout in seconds.",
    )
    parser.add_argument(
        "--max-output-chars",
        type=int,
        default=DEFAULT_MAX_OUTPUT_CHARS,
        help="Default maximum stdout/stderr characters retained each.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=None,
        help="Environment variable name to pass through. Repeat to allow multiple. Defaults to a small allowlist.",
    )
    parser.add_argument(
        "--allowed-command-prefix",
        action="append",
        default=None,
        help="Allow only commands matching this exact command or command prefix. Repeat to allow multiple. If omitted, no command prefix allowlist is enforced.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = ShellMCPServer(
        workspaces=[Path(workspace) for workspace in args.workspace],
        shell=args.shell,
        timeout_seconds=args.timeout,
        max_output_chars=args.max_output_chars,
        env_allowlist=args.env if args.env is not None else DEFAULT_ENV_ALLOWLIST,
        allowed_command_prefixes=args.allowed_command_prefix,
    )
    return serve_stdio(server)


if __name__ == "__main__":
    raise SystemExit(main())
