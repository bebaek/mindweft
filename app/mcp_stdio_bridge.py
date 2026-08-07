from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Sequence

import anyio
from fastapi import FastAPI, HTTPException, Request, Response
from mcp import Client
from mcp.shared.message import SessionMessage
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    Implementation,
    jsonrpc_message_adapter,
)
from pydantic import BaseModel, Field

from app.mcp import (
    MODERN_MCP_PROTOCOL_VERSION,
    MCPPathPolicy,
    _filter_directory_listing_text,
    _iter_path_arguments,
    _path_denied,
    mcp_jsonrpc_error,
    mcp_jsonrpc_result,
    mcp_request_protocol_version,
    strip_modern_mcp_result_envelope,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_STDIO_STREAM_LIMIT_BYTES = 16 * 1024 * 1024
MAX_LEGACY_SESSIONS = 256


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
        self._session_ids: dict[str, None] = {}
        self._sdk_worker_task: asyncio.Task[None] | None = None
        self._sdk_queue: (
            asyncio.Queue[tuple[dict[str, Any], bool, asyncio.Future[dict[str, Any]]] | None] | None
        ) = None
        self._sdk_ready: asyncio.Future[None] | None = None
        self._sdk_transport_error: asyncio.Future[Exception] | None = None

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
        await self._stop_sdk_worker(force=force)
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
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()
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
            if method != "notifications/initialized":
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported MCP notification for tools-only bridge: {method}",
                )
            return Response(status_code=202)

        self._validate_request_policy(payload)
        file_modes_to_restore = self._file_modes_to_preserve(payload)
        try:
            async with self._request_lock:
                response_payload = await self._sdk_request(
                    payload, is_modern_request=is_modern_request
                )
        finally:
            self._restore_file_modes(file_modes_to_restore)
        response_payload = self._filter_response_payload(method, response_payload, payload)
        response_headers = {"content-type": "application/json"}
        if method == "initialize" and "result" in response_payload:
            session_id = secrets.token_urlsafe(24)
            self._session_ids[session_id] = None
            if len(self._session_ids) > MAX_LEGACY_SESSIONS:
                del self._session_ids[next(iter(self._session_ids))]
            response_headers["MCP-Session-Id"] = session_id
        return Response(
            content=json.dumps(response_payload, ensure_ascii=True),
            media_type="application/json",
            headers=response_headers,
        )

    async def _sdk_request(
        self,
        payload: dict[str, Any],
        *,
        is_modern_request: bool,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self._settings.request_timeout):
                await self._ensure_sdk_worker()
                queue = self._sdk_queue
                if queue is None:
                    raise RuntimeError("MCP SDK worker queue is unavailable")
                future = asyncio.get_running_loop().create_future()
                future.add_done_callback(_consume_future_exception)
                await queue.put((payload, is_modern_request, future))
                transport_error = self._sdk_transport_error
                if transport_error is None:
                    raise RuntimeError("MCP SDK transport error signal is unavailable")
                done, _ = await asyncio.wait(
                    (future, transport_error),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if transport_error in done:
                    future.cancel()
                    error = transport_error.result()
                    await self.restart(force=True)
                    raise HTTPException(status_code=502, detail=str(error))
                return future.result()
        except TimeoutError as exc:
            if self._settings.restart_on_timeout:
                await self.restart(force=True)
            raise HTTPException(
                status_code=504, detail="Timed out waiting for MCP stdio server"
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"MCP stdio server request failed: {_exception_detail(exc)}",
            ) from exc

    async def _ensure_sdk_worker(self) -> None:
        if self._sdk_worker_task is not None and self._sdk_worker_task.done():
            await self.restart(force=True)
        if self._sdk_worker_task is None:
            loop = asyncio.get_running_loop()
            self._sdk_queue = asyncio.Queue()
            self._sdk_ready = loop.create_future()
            self._sdk_transport_error = loop.create_future()
            self._sdk_worker_task = asyncio.create_task(
                self._run_sdk_worker(),
                name=f"mcp-stdio-bridge-{self._settings.name}-sdk",
            )
        ready = self._sdk_ready
        if ready is None:
            raise RuntimeError("MCP SDK worker readiness signal is unavailable")
        await asyncio.shield(ready)

    async def _run_sdk_worker(self) -> None:
        ready = self._sdk_ready
        queue = self._sdk_queue
        assert ready is not None
        assert queue is not None
        try:
            async with Client(
                _process_stdio_transport(self),
                client_info=Implementation(name="minigent-stdio-bridge", version="0.1.0"),
                mode="auto",
                read_timeout_seconds=self._settings.request_timeout,
            ) as client:
                if not ready.done():
                    ready.set_result(None)
                while True:
                    work = await queue.get()
                    if work is None:
                        return
                    payload, is_modern_request, future = work
                    if future.cancelled():
                        continue
                    try:
                        response = await self._sdk_request_with_client(
                            client,
                            payload,
                            is_modern_request=is_modern_request,
                        )
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(exc)
                    else:
                        if not future.done():
                            future.set_result(response)
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            while not queue.empty():
                work = queue.get_nowait()
                if work is not None and not work[2].done():
                    work[2].set_exception(RuntimeError("MCP SDK worker stopped"))
            if isinstance(exc, asyncio.CancelledError):
                raise
            self._signal_transport_error(
                RuntimeError(f"MCP stdio SDK worker stopped: {_exception_detail(exc)}")
            )
            logger.warning(
                "MCP stdio SDK worker stopped: name=%s detail=%s",
                self._settings.name,
                _exception_detail(exc),
            )

    async def _sdk_request_with_client(
        self,
        client: Client,
        payload: dict[str, Any],
        *,
        is_modern_request: bool,
    ) -> dict[str, Any]:
        request_id = payload.get("id")
        method = payload["method"]
        if method == "server/discover":
            if client.protocol_version != MODERN_MCP_PROTOCOL_VERSION:
                return mcp_jsonrpc_error(request_id, -32601, "Method not found")
            result: Any = client.session.discover_result
        elif method == "initialize":
            result = _legacy_initialize_result(client)
        elif method == "tools/list":
            params = payload.get("params")
            cursor = params.get("cursor") if isinstance(params, dict) else None
            result = await client.list_tools(
                cursor=cursor if isinstance(cursor, str) else None,
                cache_mode="bypass",
            )
        elif method == "tools/call":
            params = payload.get("params")
            assert isinstance(params, dict)
            arguments = params.get("arguments") or {}
            assert isinstance(arguments, dict)
            result = await client.session.send_request(
                CallToolRequest(
                    params=CallToolRequestParams(
                        name=params["name"],
                        arguments=arguments,
                    )
                ),
                CallToolResult,
                request_read_timeout_seconds=self._settings.request_timeout,
            )
        else:
            return mcp_jsonrpc_error(request_id, -32601, f"unknown method: {method}")

        if isinstance(result, dict):
            result_payload = result
        elif result is None:
            return mcp_jsonrpc_error(request_id, -32603, "MCP SDK returned no result")
        else:
            result_payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
        if is_modern_request:
            result_payload.setdefault("resultType", "complete")
            result_payload.setdefault("ttlMs", 0)
            result_payload.setdefault("cacheScope", "private")
        response_payload = mcp_jsonrpc_result(request_id, result_payload)
        if not is_modern_request and method in {"tools/list", "tools/call"}:
            return strip_modern_mcp_result_envelope(response_payload)
        return response_payload

    async def _stop_sdk_worker(self, *, force: bool = False) -> None:
        task = self._sdk_worker_task
        queue = self._sdk_queue
        self._sdk_worker_task = None
        self._sdk_queue = None
        self._sdk_ready = None
        self._sdk_transport_error = None
        if task is None:
            return
        if force and not task.done():
            task.cancel()
        elif queue is not None and not task.done():
            await queue.put(None)
        try:
            await asyncio.wait_for(task, timeout=self._settings.request_timeout)
        except asyncio.CancelledError:
            if task.cancelled():
                return
            raise
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _signal_transport_error(self, error: Exception) -> None:
        signal = self._sdk_transport_error
        if signal is not None and not signal.done():
            signal.set_result(error)

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
        if not self._session_ids:
            raise HTTPException(status_code=400, detail="MCP session has not been initialized")
        session_id = headers.get("mcp-session-id")
        if session_id not in self._session_ids:
            raise HTTPException(status_code=400, detail="No valid MCP session ID provided")

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
            raise HTTPException(
                status_code=504, detail="Timed out writing to MCP stdio server"
            ) from exc

    async def _read_stdio_message(self) -> SessionMessage | Exception | None:
        process = self._live_process()
        stdout = process.stdout
        if stdout is None:
            return RuntimeError("MCP stdio server stdout is unavailable")
        try:
            line = await stdout.readline()
        except ValueError:
            return ValueError("MCP stdio server response exceeded stream buffer limit")
        if not line:
            return None
        try:
            message = jsonrpc_message_adapter.validate_json(line, by_name=False)
        except ValueError:
            return ValueError("MCP stdio server returned invalid JSON")
        return SessionMessage(message)

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


@asynccontextmanager
async def _process_stdio_transport(
    bridge: StdioMCPBridge,
) -> AsyncIterator[tuple[Any, Any]]:
    """Adapt the bridge-managed subprocess pipes to the SDK client transport contract."""
    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](
        0
    )
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    async def stdout_reader() -> None:
        async with read_stream_writer:
            while True:
                message = await bridge._read_stdio_message()
                if message is None:
                    bridge._signal_transport_error(RuntimeError("MCP stdio server closed stdout"))
                    return
                if isinstance(message, Exception):
                    bridge._signal_transport_error(message)
                try:
                    await read_stream_writer.send(message)
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    return

    async def stdin_writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    await bridge._write_json(
                        session_message.message.model_dump(
                            by_alias=True,
                            mode="json",
                            exclude_unset=True,
                        )
                    )
        except Exception as exc:
            bridge._signal_transport_error(exc)
            with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                await read_stream_writer.send(exc)

    async with (
        read_stream,
        write_stream,
        anyio.create_task_group() as task_group,
    ):
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            task_group.cancel_scope.cancel()


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


def _legacy_initialize_result(client: Client) -> dict[str, Any]:
    server_info = client.server_info
    capabilities = client.server_capabilities
    return {
        "protocolVersion": "2025-11-25",
        "serverInfo": (
            server_info.model_dump(by_alias=True, mode="json", exclude_none=True)
            if server_info is not None
            else {"name": "stdio-mcp-server", "version": "unknown"}
        ),
        "capabilities": (
            capabilities.model_dump(by_alias=True, mode="json", exclude_none=True)
            if capabilities is not None
            else {}
        ),
    }


def _exception_detail(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        details = [_exception_detail(item) for item in exc.exceptions]
        return "; ".join(dict.fromkeys(detail for detail in details if detail))
    return str(exc)


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
