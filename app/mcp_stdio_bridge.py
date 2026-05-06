from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, Sequence

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from app.mcp import DEFAULT_MCP_PROTOCOL_VERSION

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
DEFAULT_TIMEOUT_SECONDS = 30.0


class BridgeSettings(BaseModel):
    name: str
    command: list[str]
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    request_timeout: float = DEFAULT_TIMEOUT_SECONDS


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

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None
        self._process = None

    async def handle(self, payload: dict[str, Any], headers: dict[str, str]) -> Response:
        method = payload.get("method")
        if not isinstance(method, str):
            raise HTTPException(status_code=400, detail="JSON-RPC payload must include method")

        if method != "initialize":
            self._require_session(headers)

        if "id" not in payload:
            async with self._request_lock:
                await self._write_json(payload)
            return Response(status_code=202)

        response_payload = await self._request(payload)
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

    def _require_session(self, headers: dict[str, str]) -> None:
        if self._session_id is None:
            raise HTTPException(status_code=400, detail="MCP session has not been initialized")
        session_id = headers.get("mcp-session-id")
        if session_id != self._session_id:
            raise HTTPException(status_code=400, detail="No valid MCP session ID provided")

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._request_lock:
            await self._write_json(payload)
            return await self._read_json()

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
            raise HTTPException(status_code=504, detail="Timed out writing to MCP stdio server") from exc

    async def _read_json(self) -> dict[str, Any]:
        process = self._live_process()
        stdout = process.stdout
        if stdout is None:
            raise HTTPException(status_code=502, detail="MCP stdio server stdout is unavailable")
        try:
            line = await asyncio.wait_for(stdout.readline(), timeout=self._settings.request_timeout)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Timed out reading from MCP stdio server") from exc
        if not line:
            raise HTTPException(status_code=502, detail="MCP stdio server closed stdout")
        try:
            payload = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="MCP stdio server returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail="MCP stdio server returned non-object JSON")
        return payload

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
        return await bridge.handle(payload, {key.lower(): value for key, value in request.headers.items()})

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Expose one stdio MCP server as a local Streamable HTTP MCP endpoint."
    )
    parser.add_argument("--name", required=True, help="Name used in bridge logs.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind. Defaults to 8765.")
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
        if any(marker in lowered for marker in ("token=", "api_key=", "apikey=", "password=", "secret=")):
            key, _, _ = item.partition("=")
            redacted.append(f"{key}=<redacted>")
            continue
        redacted.append(item)
    return redacted
