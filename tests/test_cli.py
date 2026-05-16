import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app import cli


class _Response:
    def __init__(self, *, body: object | None = None, lines: list[dict[str, object]] | None = None) -> None:
        self._body = body
        self._lines = lines or []

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        if self._body is None:
            return b""
        return json.dumps(self._body).encode("utf-8")

    def __iter__(self) -> Iterator[bytes]:
        for line in self._lines:
            yield (json.dumps(line) + "\n").encode("utf-8")


def test_chat_stream_json_prints_events(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    calls: list[tuple[str, str]] = []
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "llm.request", "thread_id": "thread-1", "iteration": 1},
        {"type": "assistant.message", "thread_id": "thread-1", "content": "streamed reply"},
        {"type": "run.completed", "thread_id": "thread-1"},
    ]

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run/stream"):
            return _Response(lines=stream_events)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--json", "chat", "--stream", "hello"])

    assert exit_code == 0
    assert calls == [
        ("POST", "http://127.0.0.1:8000/threads"),
        ("POST", "http://127.0.0.1:8000/threads/thread-1/messages"),
        ("POST", "http://127.0.0.1:8000/threads/thread-1/run/stream"),
    ]
    output = json.loads(capsys.readouterr().out)
    assert output["thread_id"] == "thread-1"
    assert output["reply"] == "streamed reply"
    assert output["events"] == stream_events


def test_chat_stream_text_prints_progress_to_stderr(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "tool.call", "thread_id": "thread-1", "name": "echo"},
        {"type": "tool.result", "thread_id": "thread-1", "name": "echo", "is_error": False},
        {"type": "assistant.message", "thread_id": "thread-1", "content": "done"},
        {"type": "run.completed", "thread_id": "thread-1"},
    ]

    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run/stream"):
            return _Response(lines=stream_events)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["chat", "--stream", "hello"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert "[run] started" in captured.err
    assert "[tool] call echo" in captured.err
    assert "[tool] result echo ok" in captured.err
    assert "[run] completed" in captured.err


def test_chat_stream_text_coalesces_peer_message_updates(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "peer.task.event", "thread_id": "thread-1", "task_id": "task-1", "event": {"type": "agent_start"}},
        {"type": "peer.task.event", "thread_id": "thread-1", "task_id": "task-1", "event": {"type": "message_update"}},
        {"type": "peer.task.event", "thread_id": "thread-1", "task_id": "task-1", "event": {"type": "message_update"}},
        {"type": "peer.task.event", "thread_id": "thread-1", "task_id": "task-1", "event": {"type": "tool_execution_start", "name": "read"}},
        {"type": "assistant.message", "thread_id": "thread-1", "content": "done"},
        {"type": "run.completed", "thread_id": "thread-1"},
    ]

    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run/stream"):
            return _Response(lines=stream_events)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert cli.main(["chat", "--stream", "hello"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert "[peer] agent started" in captured.err
    assert captured.err.count("[peer] message updating...") == 1
    assert "[peer] tool start read" in captured.err


def test_chat_stream_text_coalesces_repeated_peer_poll_statuses(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "peer.task.created", "thread_id": "thread-1", "peer": "pi", "task_id": "task-1", "status": "pending"},
        {"type": "peer.task.poll", "thread_id": "thread-1", "peer": "pi", "task_id": "task-1", "status": "running"},
        {"type": "peer.task.poll", "thread_id": "thread-1", "peer": "pi", "task_id": "task-1", "status": "running"},
        {"type": "peer.task.poll", "thread_id": "thread-1", "peer": "pi", "task_id": "task-1", "status": "completed"},
        {"type": "peer.task.completed", "thread_id": "thread-1", "peer": "pi", "task_id": "task-1", "status": "completed"},
        {"type": "assistant.message", "thread_id": "thread-1", "content": "done"},
        {"type": "run.completed", "thread_id": "thread-1"},
    ]

    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run/stream"):
            return _Response(lines=stream_events)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert cli.main(["chat", "--stream", "hello"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert captured.err.count("status=running") == 1
    assert captured.err.count("status=completed") == 2
