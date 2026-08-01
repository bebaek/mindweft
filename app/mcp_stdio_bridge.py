from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, Sequence

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.mcp import (
    DEFAULT_MCP_PROTOCOL_VERSION,
    MODERN_MCP_PROTOCOL_VERSION,
    MCPPathPolicy,
    _filter_directory_listing_text,
    _iter_path_arguments,
    _path_denied,
    mcp_request_protocol_version,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_STDIO_STREAM_LIMIT_BYTES = 16 * 1024 * 1024


class BridgeSettings(BaseModel):
    name: str
    command: list[str]
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    request_timeout: float = DEFAULT_TIMEOUT_SECONDS
    stdio_stream_limit: int = DEFAULT_STDIO_STREAM_LIMIT_BYTES
    allowed_tools: list[str] | None = None
    path_policy: MCPPathPolicy = Field(default_factory=MCPPathPolicy)
    env: dict[str, str] = Field(default_factory=dict)
    restart_on_timeout: bool = False


class StdioMCPBridge:
    def __init__(self, settings: BridgeSettings) -> None:
        self._settings = settings
        self._process: asyncio.subprocess.Process | None = None
        self._request_lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._session_id: str | None = None
        self._protocol_version = DEFAULT_MCP_PROTOCOL_VERSION

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self._settings.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self._settings.stdio_stream_limit,
            env={**os.environ, **self._settings.env} if self._settings.env else None,
        )
        self._stderr_task = asyncio.create_task(
            self._log_stderr(),
            name=f"mcp-stdio-bridge-{self._settings.name}-stderr",
        )
        logger.info(
            "Started stdio MCP bridge subprocess: name=%s argv=%s pid=%s",
            self._settings.name,
            _redacted_argv(self._settings.command),
            self._process.pid,
        )

    async def stop(self, *, force: bool = False) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None and not force:
            await self._request_graceful_stdio_shutdown(process)
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0 if not force else 1.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        self._stderr_task = None
        self._process = None

    async def restart(self, *, force: bool = True) -> None:
        await self.stop(force=force)
        await self.start()

    async def _request_graceful_stdio_shutdown(self, process: asyncio.subprocess.Process) -> None:
        stdin = process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
            try:
                await stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self._settings.request_timeout)
        except TimeoutError:
            logger.warning(
                "Timed out waiting for MCP stdio server graceful shutdown: name=%s",
                self._settings.name,
            )

    async def handle(self, payload: dict[str, Any], headers: dict[str, str]) -> Response:
        method = payload.get("method")
        if not isinstance(method, str):
            raise HTTPException(status_code=400, detail="JSON-RPC payload must include method")

        is_modern_request = mcp_request_protocol_version(payload) == MODERN_MCP_PROTOCOL_VERSION
        if method not in {"initialize", "server/discover"} and not is_modern_request:
            self._require_session(headers)

        if "id" not in payload:
            async with self._request_lock:
                await self._write_json(payload)
            return Response(status_code=202)

        self._validate_request_policy(payload)
        file_modes_to_restore = self._file_modes_to_preserve(payload)
        try:
            response_payload = await self._request(payload)
        finally:
            self._restore_file_modes(file_modes_to_restore)
        response_payload = self._filter_response_payload(method, response_payload, payload)
        response_headers = {"content-type": "application/json"}
        if method == "initialize":
            self._session_id = secrets.token_urlsafe(24)
            result = response_payload.get("result")
            if isinstance(result, dict):
                protocol_version = result.get("protocolVersion")
                if isinstance(protocol_version, str) and protocol_version:
                    self._protocol_version = protocol_version
            response_headers["MCP-Session-Id"] = self._session_id
        return Response(
            content=json.dumps(response_payload, ensure_ascii=True),
            media_type="application/json",
            headers=response_headers,
        )

    def _validate_request_policy(self, payload: dict[str, Any]) -> None:
        method = payload.get("method")
        if method != "tools/call":
            return
        params = payload.get("params")
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail="MCP tools/call params must be an object")
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            raise HTTPException(status_code=400, detail="MCP tools/call requires tool name")
        if (
            self._settings.allowed_tools is not None
            and tool_name not in self._settings.allowed_tools
        ):
            raise HTTPException(
                status_code=403,
                detail=f"MCP tool '{tool_name}' is not allowed by bridge '{self._settings.name}'",
            )
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise HTTPException(
                status_code=400, detail="MCP tools/call arguments must be an object"
            )
        for path in _iter_path_arguments(arguments):
            if _path_denied(path, self._settings.path_policy):
                logger.warning(
                    "MCP bridge denied path: name=%s tool=%s path=%s",
                    self._settings.name,
                    tool_name,
                    path,
                )
                raise HTTPException(
                    status_code=403,
                    detail=f"MCP path '{path}' is denied by bridge '{self._settings.name}' policy",
                )

    def _file_modes_to_preserve(self, payload: dict[str, Any]) -> dict[str, int]:
        if payload.get("method") != "tools/call":
            return {}
        params = payload.get("params")
        if not isinstance(params, dict):
            return {}
        tool_name = params.get("name")
        if tool_name not in {"edit_file", "write_file"}:
            return {}
        arguments = params.get("arguments")
        if not isinstance(arguments, dict) or arguments.get("dryRun") is True:
            return {}
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return {}
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            return {}
        return {path: mode}

    def _restore_file_modes(self, modes: dict[str, int]) -> None:
        for path, original_mode in modes.items():
            try:
                current_mode = os.stat(path).st_mode & 0o7777
                if current_mode != original_mode:
                    os.chmod(path, original_mode)
            except FileNotFoundError:
                logger.debug("Edited file disappeared before permission restore: path=%s", path)
            except OSError:
                logger.warning(
                    "Failed to restore edited file permissions: path=%s", path, exc_info=True
                )

    def _filter_response_payload(
        self,
        method: str,
        payload: dict[str, Any],
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = payload.get("result")
        if not isinstance(result, dict):
            return payload
        if method == "tools/list":
            return self._filter_tools_list(payload, result)
        if method == "tools/call":
            return self._filter_tools_call(payload, result, request_payload)
        return payload

    def _filter_tools_list(self, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        if self._settings.allowed_tools is None:
            return payload
        tools = result.get("tools")
        if not isinstance(tools, list):
            return payload
        allowed = set(self._settings.allowed_tools)
        filtered_tools = [
            tool for tool in tools if isinstance(tool, dict) and tool.get("name") in allowed
        ]
        return {**payload, "result": {**result, "tools": filtered_tools}}

    def _filter_tools_call(
        self,
        payload: dict[str, Any],
        result: dict[str, Any],
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        params = request_payload.get("params")
        tool_name = params.get("name") if isinstance(params, dict) else None
        if tool_name != "list_directory" or not self._settings.path_policy.deny_globs:
            return payload
        content = result.get("content")
        if not isinstance(content, list):
            return payload
        filtered_content: list[Any] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                filtered_content.append(item)
                continue
            text = item.get("text")
            if not isinstance(text, str):
                filtered_content.append(item)
                continue
            filtered_content.append(
                {**item, "text": _filter_directory_listing_text(text, self._settings.path_policy)}
            )
        return {**payload, "result": {**result, "content": filtered_content}}

    def _require_session(self, headers: dict[str, str]) -> None:
        if self._session_id is None:
            raise HTTPException(status_code=400, detail="MCP session has not been initialized")
        session_id = headers.get("mcp-session-id")
        if session_id != self._session_id:
            raise HTTPException(status_code=400, detail="No valid MCP session ID provided")

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected_id = payload.get("id")
        async with self._request_lock:
            try:
                await self._write_json(payload)
                return await self._read_json(expected_id=expected_id)
            except asyncio.CancelledError:
                if self._settings.restart_on_timeout:
                    await self.restart(force=True)
                raise

    async def _write_json(self, payload: dict[str, Any]) -> None:
        process = self._live_process()
        stdin = process.stdin
        if stdin is None:
            raise HTTPException(status_code=502, detail="MCP stdio server stdin is unavailable")
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        stdin.write(line + b"\n")
        try:
            await asyncio.wait_for(stdin.drain(), timeout=self._settings.request_timeout)
        except BrokenPipeError as exc:
            raise HTTPException(status_code=502, detail="MCP stdio server closed stdin") from exc
        except TimeoutError as exc:
            if self._settings.restart_on_timeout:
                await self.restart(force=True)
            raise HTTPException(
                status_code=504, detail="Timed out writing to MCP stdio server"
            ) from exc

    async def _read_json(self, *, expected_id: Any) -> dict[str, Any]:
        process = self._live_process()
        stdout = process.stdout
        if stdout is None:
            raise HTTPException(status_code=502, detail="MCP stdio server stdout is unavailable")
        deadline = asyncio.get_running_loop().time() + self._settings.request_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                if self._settings.restart_on_timeout:
                    await self.restart(force=True)
                raise HTTPException(
                    status_code=504, detail="Timed out reading from MCP stdio server"
                )
            try:
                line = await asyncio.wait_for(stdout.readline(), timeout=remaining)
            except TimeoutError as exc:
                if self._settings.restart_on_timeout:
                    await self.restart(force=True)
                raise HTTPException(
                    status_code=504, detail="Timed out reading from MCP stdio server"
                ) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=502,
                    detail="MCP stdio server response exceeded stream buffer limit",
                ) from exc
            if not line:
                raise HTTPException(status_code=502, detail="MCP stdio server closed stdout")
            try:
                payload = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=502, detail="MCP stdio server returned invalid JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise HTTPException(
                    status_code=502, detail="MCP stdio server returned non-object JSON"
                )
            response_id = payload.get("id")
            if response_id == expected_id:
                return payload
            logger.warning(
                "Ignoring stale or unrelated MCP stdio response: name=%s expected_id=%r response_id=%r",
                self._settings.name,
                expected_id,
                response_id,
            )

    def _live_process(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is None:
            raise HTTPException(status_code=502, detail="MCP stdio server is not running")
        if process.returncode is not None:
            raise HTTPException(
                status_code=502,
                detail=f"MCP stdio server exited with code {process.returncode}",
            )
        return process

    async def _log_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            logger.warning(
                "MCP stdio server stderr: name=%s line=%s",
                self._settings.name,
                line.decode("utf-8", errors="replace").rstrip(),
            )


def create_bridge_app(settings: BridgeSettings) -> FastAPI:
    bridge = StdioMCPBridge(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await bridge.start()
        app.state.bridge = bridge
        try:
            yield
        finally:
            await bridge.stop()

    app = FastAPI(title="Minigent Stdio MCP Bridge", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(settings.path)
    async def mcp_endpoint(request: Request) -> Response:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose one stdio MCP server as a local Streamable HTTP MCP endpoint."
    )
    parser.add_argument("--name", required=True, help="Name used in bridge logs.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind. Defaults to 127.0.0.1.")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Port to bind. Defaults to 8765."
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_PATH,
        help="HTTP path for MCP requests. Defaults to /mcp.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Read/write timeout for stdio MCP requests in seconds.",
    )
    parser.add_argument(
        "--stdio-stream-limit",
        type=int,
        default=DEFAULT_STDIO_STREAM_LIMIT_BYTES,
        help="Maximum bytes buffered while reading one stdio MCP response line. Defaults to 16 MiB.",
    )
    parser.add_argument(
        "--allowed-tool",
        action="append",
        default=None,
        help="Allow only this MCP tool name. Repeat to allow multiple tools.",
    )
    parser.add_argument(
        "--deny-glob",
        action="append",
        default=[],
        help="Deny path glob for path-like tool arguments. Repeat for multiple patterns.",
    )
    parser.add_argument(
        "--allow-glob",
        action="append",
        default=[],
        help="Allow path glob that overrides denied path globs. Repeat for multiple patterns.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command argv for the stdio MCP server. Prefix with -- before the command.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("stdio MCP server command is required after --")

    settings = BridgeSettings(
        name=args.name,
        command=command,
        host=args.host,
        port=args.port,
        path=args.path,
        request_timeout=args.request_timeout,
        stdio_stream_limit=args.stdio_stream_limit,
        allowed_tools=args.allowed_tool,
        path_policy=MCPPathPolicy(
            deny_globs=list(args.deny_glob),
            allow_globs=list(args.allow_glob),
        ),
    )
    app = create_bridge_app(settings)

    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)


def _redacted_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for item in argv:
        lowered = item.lower()
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if lowered in {"--token", "--api-key", "--apikey", "--password", "--secret"}:
            redacted.append(item)
            redact_next = True
            continue
        if any(
            marker in lowered
            for marker in ("token=", "api_key=", "apikey=", "password=", "secret=")
        ):
            key, _, _ = item.partition("=")
            redacted.append(f"{key}=<redacted>")
            continue
        redacted.append(item)
    return redacted
