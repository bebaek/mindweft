from __future__ import annotations

import asyncio
import json
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
from fastapi.responses import PlainTextResponse
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
    agent_command: tuple[str, ...] = ("opencode",)
    allowed_workspaces: tuple[Path, ...] = ()
    agent_runtime: str = "opencode"
    agent_args_template: tuple[str, ...] = ()
    codex_sandbox: str = "read-only"
    codex_json: bool = True
    tail_chars: int = 20_000
    event_limit: int = 50
    cancel_grace_seconds: float = 5.0
    allowed_task_env_prefixes: tuple[str, ...] = ("MINIGENT_MCP_BROKER_",)


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1)
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    cwd: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    final_output: str = ""
    events_tail: list[dict[str, object]] = Field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    links: dict[str, str] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)


class TaskEventsResponse(BaseModel):
    task_id: str
    next_index: int
    events: list[dict[str, object]] = Field(default_factory=list)


class TaskEventsArtifactResponse(BaseModel):
    task_id: str
    events: list[dict[str, object]] = Field(default_factory=list)


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
    env: dict[str, str] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    final_output: str = ""
    all_events: list[dict[str, object]] = field(default_factory=list)
    events: deque[dict[str, object]] = field(default_factory=deque)
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
            final_output=self.final_output,
            events_tail=list(self.events),
            stdout_tail=self.stdout.text(),
            stderr_tail=self.stderr.text(),
            links=_task_links(self.task_id),
            artifacts=_task_artifacts(self.task_id),
        )

    def events_response(self, *, after: int | None = None) -> TaskEventsResponse:
        start_index = 0 if after is None else max(0, after + 1)
        events = self.all_events[start_index:]
        return TaskEventsResponse(
            task_id=self.task_id,
            next_index=start_index + len(events),
            events=events,
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
            env=_allowed_task_env(request.env, self._settings.allowed_task_env_prefixes),
            all_events=[],
            events=deque(maxlen=self._settings.event_limit),
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
        command = _agent_command(
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
                env=_process_env(record.env),
                start_new_session=True,
            )
            await asyncio.gather(
                _capture_stdout(record.process.stdout, record),
                _capture_stream(record.process.stderr, record.stderr),
            )
            record.exit_code = await record.process.wait()
        except asyncio.CancelledError:
            record.status = TaskStatus.CANCELED
            record.finished_at = utc_now()
            raise
        except FileNotFoundError:
            record.stderr.append(f"Command not found: {self._settings.agent_command[0]}")
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
            if not record.final_output:
                record.final_output = record.stdout.text().strip()
            record.status = TaskStatus.COMPLETED
        else:
            record.status = TaskStatus.FAILED
        record.finished_at = utc_now()


async def _capture_stdout(
    stream: asyncio.StreamReader | None,
    record: TaskRecord,
) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode(errors="replace")
        record.stdout.append(text)
        _capture_json_event(text, record)


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


def _capture_json_event(line: str, record: TaskRecord) -> None:
    stripped = line.strip()
    if not stripped:
        return
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    indexed_event = {"index": len(record.all_events), **event}
    record.all_events.append(indexed_event)
    record.events.append(indexed_event)
    final_output = _extract_final_output(indexed_event)
    if final_output is not None:
        record.final_output = final_output


def _task_links(task_id: str) -> dict[str, str]:
    return {
        "self": f"/tasks/{task_id}",
        "events": f"/tasks/{task_id}/events",
        "cancel": f"/tasks/{task_id}/cancel",
    }


def _task_artifacts(task_id: str) -> dict[str, str]:
    return {
        "final_output": f"/tasks/{task_id}/artifacts/final-output",
        "stdout_tail": f"/tasks/{task_id}/artifacts/stdout-tail",
        "stderr_tail": f"/tasks/{task_id}/artifacts/stderr-tail",
        "events": f"/tasks/{task_id}/artifacts/events",
    }


def _extract_final_output(event: dict[str, object]) -> str | None:
    opencode_text = _opencode_text_part(event)
    if opencode_text is not None:
        return opencode_text

    message = _event_message(event)
    if message is None:
        return None

    text = _text_from_value(message)
    event_type = str(event.get("type") or event.get("event") or "")
    if text is not None and _looks_like_final_event(event_type, event):
        return text

    content = message.get("content")
    if isinstance(content, list):
        text_parts = []
        for item in content:
            text_part = _text_from_value(item)
            if text_part:
                text_parts.append(text_part)
        if text_parts and _looks_like_final_event(event_type, event):
            return "\n".join(text_parts)
    return None


def _opencode_text_part(event: dict[str, object]) -> str | None:
    if event.get("type") != "text":
        return None
    part = event.get("part")
    if not isinstance(part, dict) or part.get("type") != "text":
        return None
    text = part.get("text")
    return text if isinstance(text, str) and text else None


def _event_message(event: dict[str, object]) -> dict[str, object] | None:
    message = event.get("message")
    if isinstance(message, dict):
        return message
    item = event.get("item")
    if isinstance(item, dict):
        return item
    return None


def _text_from_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    for key in ("text", "content", "message"):
        text = value.get(key)
        if isinstance(text, str):
            return text
    return None


def _looks_like_final_event(event_type: str, event: dict[str, object]) -> bool:
    normalized = event_type.lower()
    if "final" in normalized or "complete" in normalized or normalized.endswith("done"):
        return True
    if event.get("is_final") is True or event.get("final") is True:
        return True
    message = _event_message(event)
    if message is None:
        return False
    role = str(message.get("role") or "").lower()
    if role != "assistant":
        return False
    status = str(event.get("status") or message.get("status") or "").lower()
    return status in {"completed", "complete", "done", "final"}


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


def _agent_command(settings: Settings, *, cwd: Path, prompt: str) -> list[str]:
    runtime = settings.agent_runtime.lower()
    if settings.agent_args_template:
        return [
            *settings.agent_command,
            *[_format_agent_arg(arg, cwd=cwd, prompt=prompt) for arg in settings.agent_args_template],
        ]
    if runtime == "codex":
        command = [
            *settings.agent_command,
            "exec",
        ]
        if settings.codex_json:
            command.append("--json")
        command.extend(
            [
                "--sandbox",
                settings.codex_sandbox,
                "--cd",
                str(cwd),
                prompt,
            ]
        )
        return command
    if runtime == "opencode":
        return [*settings.agent_command, "run", "--format", "json", "--dir", str(cwd), prompt]
    if runtime == "plain":
        return [*settings.agent_command, prompt]
    raise HTTPException(
        status_code=503,
        detail=(
            f"Unsupported AGENT_RUNTIME '{settings.agent_runtime}'. "
            "Use opencode, codex, plain, or set AGENT_ARGS_TEMPLATE."
        ),
    )


def _allowed_task_env(values: dict[str, str], allowed_prefixes: tuple[str, ...]) -> dict[str, str]:
    allowed: dict[str, str] = {}
    for key, value in values.items():
        if any(key.startswith(prefix) for prefix in allowed_prefixes):
            allowed[key] = str(value)
    return allowed


def _process_env(task_env: dict[str, str]) -> dict[str, str]:
    env = {**os.environ, **task_env}
    broker_url = env.get("MINIGENT_MCP_BROKER_URL")
    broker_token = env.get("MINIGENT_MCP_BROKER_TOKEN")
    if broker_url and broker_token:
        env["OPENCODE_CONFIG_CONTENT"] = _opencode_config_content(
            env.get("OPENCODE_CONFIG_CONTENT")
        )
    return env


def _opencode_config_content(existing: str | None) -> str:
    config: dict[str, object]
    if existing:
        try:
            parsed = json.loads(existing)
        except json.JSONDecodeError:
            parsed = {}
        config = parsed if isinstance(parsed, dict) else {}
    else:
        config = {}
    mcp = config.get("mcp")
    if not isinstance(mcp, dict):
        mcp = {}
    mcp["minigent"] = {
        "type": "remote",
        "url": "{env:MINIGENT_MCP_BROKER_URL}",
        "enabled": True,
        "oauth": False,
        "headers": {
            "Authorization": "Bearer {env:MINIGENT_MCP_BROKER_TOKEN}",
        },
    }
    config["mcp"] = mcp
    return json.dumps(config, separators=(",", ":"))


def _format_agent_arg(arg: str, *, cwd: Path, prompt: str) -> str:
    return arg.format(cwd=str(cwd), prompt=prompt)


def _resolve_allowed_workspace(cwd: Path, allowed_workspaces: tuple[Path, ...]) -> Path:
    if not allowed_workspaces:
        raise HTTPException(
            status_code=503,
            detail="AGENT_ALLOWED_WORKSPACES must include at least one workspace",
        )
    resolved = cwd.expanduser().resolve()
    for workspace in allowed_workspaces:
        allowed = workspace.expanduser().resolve()
        if resolved == allowed or allowed in resolved.parents:
            return resolved
    raise HTTPException(status_code=403, detail=f"Workspace '{resolved}' is not allowed")


def settings_from_env() -> Settings:
    runtime = os.getenv("AGENT_RUNTIME", "opencode")
    default_command = "codex" if runtime.lower() == "codex" else "opencode"
    command = tuple(shlex.split(os.getenv("AGENT_COMMAND", default_command)))
    allowed = tuple(
        Path(item)
        for item in os.getenv("AGENT_ALLOWED_WORKSPACES", "").split(os.pathsep)
        if item
    )
    args_template = tuple(shlex.split(os.getenv("AGENT_ARGS_TEMPLATE", "")))
    tail_chars = int(os.getenv("AGENT_TAIL_CHARS", "20000"))
    cancel_grace_seconds = float(os.getenv("AGENT_CANCEL_GRACE_SECONDS", "5"))
    event_limit = int(os.getenv("AGENT_EVENT_LIMIT", "50"))
    codex_json = os.getenv("CODEX_AGENT_JSON", "true").lower() not in {"0", "false", "no"}
    env_prefixes = tuple(
        prefix
        for prefix in os.getenv("AGENT_ALLOWED_TASK_ENV_PREFIXES", "MINIGENT_MCP_BROKER_").split(",")
        if prefix
    )
    return Settings(
        agent_command=command,
        allowed_workspaces=allowed,
        agent_runtime=runtime,
        agent_args_template=args_template,
        codex_sandbox=os.getenv("CODEX_AGENT_SANDBOX", "read-only"),
        codex_json=codex_json,
        tail_chars=tail_chars,
        event_limit=event_limit,
        cancel_grace_seconds=cancel_grace_seconds,
        allowed_task_env_prefixes=env_prefixes,
    )


def get_store(request: Request) -> TaskStore:
    return request.app.state.task_store


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or settings_from_env()
    app = FastAPI(title="Minigent Local Agent Wrapper", version="0.1.0")
    app.state.settings = resolved_settings
    app.state.task_store = TaskStore(resolved_settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/agent-card", response_model=AgentCard)
    async def agent_card() -> AgentCard:
        runtime = resolved_settings.agent_runtime.lower()
        return AgentCard(
            name=f"{runtime}-coding-agent",
            description=f"Runs local {runtime} CLI tasks in configured workspaces.",
            version="0.1.0",
            capabilities=["code.inspect", "code.explain", "code.change.proposed"],
            endpoints={
                "create_task": "POST /tasks",
                "get_task": "GET /tasks/{task_id}",
                "get_task_events": "GET /tasks/{task_id}/events",
                "get_final_output_artifact": "GET /tasks/{task_id}/artifacts/final-output",
                "get_stdout_tail_artifact": "GET /tasks/{task_id}/artifacts/stdout-tail",
                "get_stderr_tail_artifact": "GET /tasks/{task_id}/artifacts/stderr-tail",
                "get_events_artifact": "GET /tasks/{task_id}/artifacts/events",
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

    @app.get("/tasks/{task_id}/events", response_model=TaskEventsResponse)
    async def get_task_events(
        task_id: str,
        store: Annotated[TaskStore, Depends(get_store)],
        after: int | None = None,
    ) -> TaskEventsResponse:
        record = await store.get(task_id)
        return record.events_response(after=after)

    @app.get("/tasks/{task_id}/artifacts/final-output", response_class=PlainTextResponse)
    async def get_final_output_artifact(
        task_id: str,
        store: Annotated[TaskStore, Depends(get_store)],
    ) -> str:
        record = await store.get(task_id)
        return record.final_output

    @app.get("/tasks/{task_id}/artifacts/stdout-tail", response_class=PlainTextResponse)
    async def get_stdout_tail_artifact(
        task_id: str,
        store: Annotated[TaskStore, Depends(get_store)],
    ) -> str:
        record = await store.get(task_id)
        return record.stdout.text()

    @app.get("/tasks/{task_id}/artifacts/stderr-tail", response_class=PlainTextResponse)
    async def get_stderr_tail_artifact(
        task_id: str,
        store: Annotated[TaskStore, Depends(get_store)],
    ) -> str:
        record = await store.get(task_id)
        return record.stderr.text()

    @app.get("/tasks/{task_id}/artifacts/events", response_model=TaskEventsArtifactResponse)
    async def get_events_artifact(
        task_id: str,
        store: Annotated[TaskStore, Depends(get_store)],
    ) -> TaskEventsArtifactResponse:
        record = await store.get(task_id)
        return TaskEventsArtifactResponse(task_id=task_id, events=record.all_events)

    @app.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
    async def cancel_task(
        task_id: str,
        store: Annotated[TaskStore, Depends(get_store)],
    ) -> TaskResponse:
        record = await store.cancel(task_id)
        return record.response()

    return app


app = create_app()
