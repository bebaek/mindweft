from __future__ import annotations

import asyncio
import os
import shlex
import signal
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELING = "canceling"
    CANCELED = "canceled"


class Settings(BaseModel):
    codex_command: tuple[str, ...] = ("codex",)
    allowed_workspaces: tuple[Path, ...] = ()
    codex_sandbox: str = "read-only"
    tail_chars: int = 20_000
    cancel_grace_seconds: float = 5.0


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1)
    cwd: Path


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    cwd: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


class AgentCard(BaseModel):
    name: str
    description: str
    version: str
    capabilities: list[str]
    endpoints: dict[str, str]
    side_effects: list[str]


@dataclass
class OutputTail:
    max_chars: int
    _chunks: deque[str] = field(default_factory=deque)
    _length: int = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        self._length += len(text)
        while self._length > self.max_chars and self._chunks:
            extra = self._length - self.max_chars
            first = self._chunks[0]
            if len(first) <= extra:
                self._length -= len(first)
                self._chunks.popleft()
            else:
                self._chunks[0] = first[extra:]
                self._length -= extra

    def text(self) -> str:
        return "".join(self._chunks)


@dataclass
class TaskRecord:
    task_id: str
    prompt: str
    cwd: Path
    status: TaskStatus = TaskStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    stdout: OutputTail = field(default_factory=lambda: OutputTail(20_000))
    stderr: OutputTail = field(default_factory=lambda: OutputTail(20_000))
    process: asyncio.subprocess.Process | None = None
    worker: asyncio.Task[None] | None = None

    def response(self) -> TaskResponse:
        return TaskResponse(
            task_id=self.task_id,
            status=self.status,
            cwd=str(self.cwd),
            started_at=self.started_at,
            finished_at=self.finished_at,
            exit_code=self.exit_code,
            stdout_tail=self.stdout.text(),
            stderr_tail=self.stderr.text(),
        )


class TaskStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: TaskRequest) -> TaskRecord:
        cwd = _resolve_allowed_workspace(request.cwd, self._settings.allowed_workspaces)
        task_id = f"task_{uuid4().hex}"
        record = TaskRecord(
            task_id=task_id,
            prompt=request.prompt,
            cwd=cwd,
            stdout=OutputTail(self._settings.tail_chars),
            stderr=OutputTail(self._settings.tail_chars),
        )
        async with self._lock:
            self._tasks[task_id] = record
            record.worker = asyncio.create_task(self._run(record))
        return record

    async def get(self, task_id: str) -> TaskRecord:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
            return record

    async def cancel(self, task_id: str) -> TaskRecord:
        record = await self.get(task_id)
        if record.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED}:
            return record
        record.status = TaskStatus.CANCELING
        if record.process is None:
            worker = record.worker
            if worker is not None:
                worker.cancel()
            record.status = TaskStatus.CANCELED
            record.finished_at = utc_now()
            return record

        await _terminate_process_group(
            record.process,
            grace_seconds=self._settings.cancel_grace_seconds,
        )
        record.exit_code = record.process.returncode
        record.status = TaskStatus.CANCELED
        record.finished_at = utc_now()
        return record

    async def _run(self, record: TaskRecord) -> None:
        record.started_at = utc_now()
        record.status = TaskStatus.RUNNING
        command = _codex_exec_command(
            self._settings,
            cwd=record.cwd,
            prompt=record.prompt,
        )
        try:
            record.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=record.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            await asyncio.gather(
                _capture_stream(record.process.stdout, record.stdout),
                _capture_stream(record.process.stderr, record.stderr),
            )
            record.exit_code = await record.process.wait()
        except asyncio.CancelledError:
            record.status = TaskStatus.CANCELED
            record.finished_at = utc_now()
            raise
        except FileNotFoundError:
            record.stderr.append(f"Command not found: {self._settings.codex_command[0]}")
            record.exit_code = 127
            record.status = TaskStatus.FAILED
            record.finished_at = utc_now()
            return
        except Exception as exc:  # pragma: no cover - defensive boundary
            record.stderr.append(f"{type(exc).__name__}: {exc}")
            record.exit_code = 1
            record.status = TaskStatus.FAILED
            record.finished_at = utc_now()
            return

        if record.status in {TaskStatus.CANCELING, TaskStatus.CANCELED}:
            record.status = TaskStatus.CANCELED
        elif record.exit_code == 0:
            record.status = TaskStatus.COMPLETED
        else:
            record.status = TaskStatus.FAILED
        record.finished_at = utc_now()


async def _capture_stream(
    stream: asyncio.StreamReader | None,
    tail: OutputTail,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        tail.append(chunk.decode(errors="replace"))


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return
    signals = [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    for sig in signals:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            return
        except TimeoutError:
            continue


def _codex_exec_command(settings: Settings, *, cwd: Path, prompt: str) -> list[str]:
    return [
        *settings.codex_command,
        "exec",
        "--sandbox",
        settings.codex_sandbox,
        "--cd",
        str(cwd),
        prompt,
    ]


def _resolve_allowed_workspace(cwd: Path, allowed_workspaces: tuple[Path, ...]) -> Path:
    if not allowed_workspaces:
        raise HTTPException(
            status_code=503,
            detail="CODEX_AGENT_ALLOWED_WORKSPACES must include at least one workspace",
        )
    resolved = cwd.expanduser().resolve()
    for workspace in allowed_workspaces:
        allowed = workspace.expanduser().resolve()
        if resolved == allowed or allowed in resolved.parents:
            return resolved
    raise HTTPException(status_code=403, detail=f"Workspace '{resolved}' is not allowed")


def settings_from_env() -> Settings:
    command = tuple(shlex.split(os.getenv("CODEX_AGENT_COMMAND", "codex")))
    allowed = tuple(
        Path(item)
        for item in os.getenv("CODEX_AGENT_ALLOWED_WORKSPACES", "").split(os.pathsep)
        if item
    )
    tail_chars = int(os.getenv("CODEX_AGENT_TAIL_CHARS", "20000"))
    cancel_grace_seconds = float(os.getenv("CODEX_AGENT_CANCEL_GRACE_SECONDS", "5"))
    return Settings(
        codex_command=command,
        allowed_workspaces=allowed,
        codex_sandbox=os.getenv("CODEX_AGENT_SANDBOX", "read-only"),
        tail_chars=tail_chars,
        cancel_grace_seconds=cancel_grace_seconds,
    )


def get_store(request: Request) -> TaskStore:
    return request.app.state.task_store


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or settings_from_env()
    app = FastAPI(title="Minigent Codex Agent", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.task_store = TaskStore(resolved_settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/agent-card", response_model=AgentCard)
    async def agent_card() -> AgentCard:
        return AgentCard(
            name="codex-coding-agent",
            description="Runs local Codex CLI tasks in configured workspaces.",
            version="0.1.0",
            capabilities=["code.inspect", "code.explain", "code.change.proposed"],
            endpoints={
                "create_task": "POST /tasks",
                "get_task": "GET /tasks/{task_id}",
                "cancel_task": "POST /tasks/{task_id}/cancel",
            },
            side_effects=["process_execution", "filesystem_read"],
        )

    @app.post("/tasks", response_model=TaskResponse)
    async def create_task(
        body: TaskRequest,
        store: Annotated[TaskStore, Depends(get_store)],
    ) -> TaskResponse:
        record = await store.create(body)
        return record.response()

    @app.get("/tasks/{task_id}", response_model=TaskResponse)
    async def get_task(
        task_id: str,
        store: Annotated[TaskStore, Depends(get_store)],
    ) -> TaskResponse:
        record = await store.get(task_id)
        return record.response()

    @app.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
    async def cancel_task(
        task_id: str,
        store: Annotated[TaskStore, Depends(get_store)],
    ) -> TaskResponse:
        record = await store.cancel(task_id)
        return record.response()

    return app


app = create_app()
