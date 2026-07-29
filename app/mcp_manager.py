from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Coroutine, Protocol

from fastapi import HTTPException

from app.mcp import MCPHTTPClient, MCPServerConfig, MCPServerInfo
from app.models import ToolSpec


class MCPClientProtocol(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any: ...

    def server_info(self) -> MCPServerInfo: ...


@dataclass(frozen=True)
class MCPServerRuntimeState:
    config: MCPServerConfig
    status: str
    tools: list[ToolSpec]
    client: MCPClientProtocol | None
    server_info: MCPServerInfo | None
    last_error: str | None
    last_checked_at: datetime | None
    next_retry_at: datetime | None
    generation: int

    def public_dict(self) -> dict[str, object]:
        info = self.server_info
        return {
            "name": self.config.name,
            "url": self.config.url,
            "protocol_version": (
                info.protocol_version if info is not None else self.config.protocol_version
            ),
            "session": bool(info.session_id) if info is not None else False,
            "server_name": info.server_name if info is not None else None,
            "server_version": info.server_version if info is not None else None,
            "tool_count": len(self.tools),
            "allowed_tools": self.config.allowed_tools,
            "path_policy": {
                "deny_globs": list(self.config.path_policy.deny_globs),
                "allow_globs": list(self.config.path_policy.allow_globs),
            },
            "result_redaction": {
                "enabled": self.config.result_redaction_policy.enabled,
                "mode": self.config.result_redaction_policy.mode,
                "sensitive_tools": sorted(self.config.result_redaction_policy.sensitive_tools),
            },
            "private_value_policy": {
                "mode": self.config.private_value_policy.mode,
                "argument_paths": list(self.config.private_value_policy.argument_paths),
                "tool_overrides": {
                    tool_name: {
                        "mode": policy.mode,
                        "argument_paths": list(policy.argument_paths),
                    }
                    for tool_name, policy in self.config.private_value_tool_policies.items()
                },
            },
            "status": self.status,
            "last_error": self.last_error,
            "last_checked_at": _format_datetime(self.last_checked_at),
            "next_retry_at": _format_datetime(self.next_retry_at),
        }


@dataclass(frozen=True)
class MCPRegistrySnapshot:
    servers: list[MCPServerRuntimeState]
    generation: int


class MCPServerManager:
    def __init__(
        self,
        *,
        initial_retry_seconds: float = 5.0,
        max_retry_seconds: float = 300.0,
        client_factory: Callable[[MCPServerConfig], MCPClientProtocol] | None = None,
    ) -> None:
        self._initial_retry_seconds = initial_retry_seconds
        self._max_retry_seconds = max_retry_seconds
        self._client_factory = client_factory or MCPHTTPClient
        self._states: dict[str, MCPServerRuntimeState] = {}
        self._retry_delays: dict[str, float] = {}
        self._generation = 0
        self._lock = RLock()
        self._wake_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._wake_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="mcp-server-retry-loop")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._wake_event = None

    def snapshot(self, configs: list[MCPServerConfig]) -> MCPRegistrySnapshot:
        missing = [config for config in configs if _state_key(config) not in self._states]
        if missing:
            _run_awaitable_sync(self.refresh(missing, force=True))
            self._wake()

        keys = {_state_key(config) for config in configs}
        with self._lock:
            servers = [state for key, state in self._states.items() if key in keys]
            generation = self._generation
        return MCPRegistrySnapshot(servers=servers, generation=generation)

    async def refresh(
        self,
        configs: list[MCPServerConfig] | None = None,
        *,
        force: bool = False,
    ) -> None:
        if configs is None:
            with self._lock:
                configs = [state.config for state in self._states.values()]

        for config in configs:
            key = _state_key(config)
            with self._lock:
                state = self._states.get(key)
                if state is not None and not force and state.next_retry_at is not None:
                    next_retry_at = state.next_retry_at.timestamp()
                    if next_retry_at > time.time():
                        continue
                elif state is not None and not force and state.status == "connected":
                    continue
                self._states[key] = _pending_state(config, state)

            started = datetime.now(timezone.utc)
            try:
                client = self._client_factory(config)
                tools = await client.list_tools()
                info = client.server_info()
                with self._lock:
                    previous = self._states.get(key)
                    previous_generation = previous.generation if previous is not None else 0
                    self._generation += 1
                    self._retry_delays.pop(key, None)
                    self._states[key] = MCPServerRuntimeState(
                        config=config,
                        status="connected",
                        tools=tools,
                        client=client,
                        server_info=info,
                        last_error=None,
                        last_checked_at=started,
                        next_retry_at=None,
                        generation=previous_generation + 1,
                    )
            except Exception as exc:
                retry_delay = self._next_retry_delay(key)
                next_retry_at = datetime.fromtimestamp(time.time() + retry_delay, timezone.utc)
                with self._lock:
                    previous = self._states.get(key)
                    previous_generation = previous.generation if previous is not None else 0
                    self._generation += 1
                    self._states[key] = MCPServerRuntimeState(
                        config=config,
                        status="unavailable",
                        tools=[],
                        client=None,
                        server_info=None,
                        last_error=_error_detail(exc),
                        last_checked_at=started,
                        next_retry_at=next_retry_at,
                        generation=previous_generation + 1,
                    )

    async def _run(self) -> None:
        while True:
            await self.refresh()
            wait_seconds = self._next_wait_seconds()
            event = self._wake_event
            if event is None:
                await asyncio.sleep(wait_seconds)
                continue
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass

    def _next_wait_seconds(self) -> float:
        with self._lock:
            retry_times = [
                state.next_retry_at.timestamp()
                for state in self._states.values()
                if state.next_retry_at is not None
            ]
        if not retry_times:
            return 60.0
        return max(0.1, min(retry_times) - time.time())

    def _next_retry_delay(self, key: str) -> float:
        previous = self._retry_delays.get(key, self._initial_retry_seconds / 2)
        delay = min(previous * 2, self._max_retry_seconds)
        self._retry_delays[key] = delay
        jitter = random.uniform(0.9, 1.1)
        return delay * jitter

    def _wake(self) -> None:
        event = self._wake_event
        if event is not None:
            event.set()


def _state_key(config: MCPServerConfig) -> str:
    header_items = tuple(sorted(config.headers.items()))
    return repr(
        (
            config.name,
            config.url,
            header_items,
            config.protocol_version,
            config.allowed_tools,
            config.path_policy.deny_globs,
            config.path_policy.allow_globs,
            config.result_redaction_policy.enabled,
            config.result_redaction_policy.mode,
            config.result_redaction_policy.sensitive_tools,
            config.timeout_seconds,
        )
    )


def _pending_state(
    config: MCPServerConfig,
    previous: MCPServerRuntimeState | None,
) -> MCPServerRuntimeState:
    if previous is None:
        return MCPServerRuntimeState(
            config=config,
            status="retrying",
            tools=[],
            client=None,
            server_info=None,
            last_error=None,
            last_checked_at=None,
            next_retry_at=None,
            generation=0,
        )
    return replace(previous, status="retrying")


def _error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _run_awaitable_sync(awaitable: Coroutine[Any, Any, object]) -> object:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()
