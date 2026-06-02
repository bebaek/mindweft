import json
import urllib.error
from collections.abc import Iterator, Mapping, Sequence
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

from app import cli


class _Response:
    def __init__(
        self, *, body: object | None = None, lines: Sequence[Mapping[str, object]] | None = None
    ) -> None:
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


def test_run_reads_prompt_from_stdin_and_prints_plain_reply(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    calls: list[tuple[str, str]] = []

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            body = json.loads(request.data.decode("utf-8"))
            assert body == {"content": "summarize this"}
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run"):
            return _Response(body={"reply": "summary"})
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("sys.stdin", StringIO("summarize this\n"))

    exit_code = cli.main(["run"])

    assert exit_code == 0
    assert calls == [
        ("POST", "http://127.0.0.1:8000/threads"),
        ("POST", "http://127.0.0.1:8000/threads/thread-1/messages"),
        ("POST", "http://127.0.0.1:8000/threads/thread-1/run"),
    ]
    captured = capsys.readouterr()
    assert captured.out == "summary\n"
    assert captured.err == ""


def test_run_json_outputs_structured_reply(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run"):
            return _Response(body={"reply": "pong"})
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["run", "--json", "ping"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"thread_id": "thread-1", "created_thread": True, "reply": "pong"}


def test_run_quiet_suppresses_streaming_progress(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "tool.call", "thread_id": "thread-1", "name": "echo", "arguments": {"text": "hi"}},
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

    exit_code = cli.main(["run", "--stream", "--quiet", "hello"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert captured.err == ""


def test_run_stream_keyboard_interrupt_reports_abort(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    class InterruptingResponse(_Response):
        def __iter__(self) -> Iterator[bytes]:
            yield (json.dumps({"type": "run.started", "thread_id": "thread-1"}) + "\n").encode(
                "utf-8"
            )
            raise KeyboardInterrupt

    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run/stream"):
            return InterruptingResponse()
        if request.full_url.endswith("/threads/thread-1/run/cancel"):
            return _Response(body={"cancelled": True, "thread_id": "thread-1"})
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["run", "--stream", "hello"])

    assert exit_code == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "locally aborted current run" in captured.err
    assert "server cancellation requested" in captured.err


def test_run_json_keyboard_interrupt_reports_structured_abort(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run"):
            raise KeyboardInterrupt
        if request.full_url.endswith("/threads/thread-1/run/cancel"):
            return _Response(body={"cancelled": False, "thread_id": "thread-1"})
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["run", "--json", "hello"])

    assert exit_code == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": {
            "message": "Run aborted locally.",
            "category": "aborted",
            "server_cancelled": False,
            "detail": "server cancellation unavailable for non-streaming runs",
        }
    }


def test_chat_stream_text_prints_progress_to_stderr(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "tool.call", "thread_id": "thread-1", "name": "echo", "arguments": {"text": "hi"}},
        {
            "type": "tool.result",
            "thread_id": "thread-1",
            "name": "echo",
            "is_error": False,
            "result": {"text": "hi"},
        },
        {"type": "assistant.message", "thread_id": "thread-1", "content": "done"},
        {
            "type": "run.completed",
            "thread_id": "thread-1",
            "usage": {"prompt_tokens": 1800, "completion_tokens": 420, "total_tokens": 2220},
        },
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
    assert "● preparing" in captured.err
    assert '🔧 echo(text="hi") ...' in captured.err
    assert '🔧 echo(text="hi") done' in captured.err
    assert "result:" not in captured.err
    assert '"text": "hi"' not in captured.err
    assert "● done · tokens: prompt 1.8k · completion 420 · total 2.2k" in captured.err


def test_chat_stream_text_can_hide_token_summary(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "assistant.message", "thread_id": "thread-1", "content": "done"},
        {
            "type": "run.completed",
            "thread_id": "thread-1",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
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

    exit_code = cli.main(["chat", "--stream", "--tokens", "off", "hello"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert "● done\n" in captured.err
    assert "tokens:" not in captured.err


def test_chat_stream_json_includes_usage(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "assistant.message", "thread_id": "thread-1", "content": "done"},
        {
            "type": "run.completed",
            "thread_id": "thread-1",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
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

    exit_code = cli.main(["--json", "chat", "--stream", "hello"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_chat_stream_text_can_show_tool_results(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {"type": "tool.call", "thread_id": "thread-1", "name": "echo", "arguments": {"text": "hi"}},
        {
            "type": "tool.result",
            "thread_id": "thread-1",
            "name": "echo",
            "is_error": False,
            "result": {"text": "hi"},
        },
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

    exit_code = cli.main(["chat", "--stream", "--show-tool-results", "hello"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert '🔧 echo(text="hi") done' in captured.err
    assert "   result:" in captured.err
    assert '"text": "hi"' in captured.err


def test_chat_stream_text_coalesces_peer_message_updates(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    stream_events = [
        {"type": "run.started", "thread_id": "thread-1"},
        {
            "type": "peer.task.event",
            "thread_id": "thread-1",
            "task_id": "task-1",
            "event": {"type": "agent_start"},
        },
        {
            "type": "peer.task.event",
            "thread_id": "thread-1",
            "task_id": "task-1",
            "event": {"type": "message_update"},
        },
        {
            "type": "peer.task.event",
            "thread_id": "thread-1",
            "task_id": "task-1",
            "event": {"type": "message_update"},
        },
        {
            "type": "peer.task.event",
            "thread_id": "thread-1",
            "task_id": "task-1",
            "event": {"type": "tool_execution_start", "name": "read"},
        },
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
        {
            "type": "peer.task.created",
            "thread_id": "thread-1",
            "peer": "pi",
            "task_id": "task-1",
            "status": "pending",
        },
        {
            "type": "peer.task.poll",
            "thread_id": "thread-1",
            "peer": "pi",
            "task_id": "task-1",
            "status": "running",
        },
        {
            "type": "peer.task.poll",
            "thread_id": "thread-1",
            "peer": "pi",
            "task_id": "task-1",
            "status": "running",
        },
        {
            "type": "peer.task.poll",
            "thread_id": "thread-1",
            "peer": "pi",
            "task_id": "task-1",
            "status": "completed",
        },
        {
            "type": "peer.task.completed",
            "thread_id": "thread-1",
            "peer": "pi",
            "task_id": "task-1",
            "status": "completed",
        },
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


def test_threads_lists_locally_remembered_threads(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run"):
            return _Response(body={"reply": "hi"})
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert cli.main(["chat", "hello there"]) == 0
    assert cli.main(["threads"]) == 0

    output = capsys.readouterr().out
    assert "hi" in output
    assert "Recent threads" in output
    assert "hello there" in output
    assert "thread-1" in output


def test_resume_defaults_to_latest_thread_and_prints_transcript(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    calls: list[tuple[str, str]] = []

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            if request.get_method() == "POST":
                return _Response(body={"role": "user"})
            return _Response(
                body=[
                    {"role": "user", "content": "hello there"},
                    {"role": "assistant", "content": "hi"},
                ]
            )
        if request.full_url.endswith("/threads/thread-1/run"):
            return _Response(body={"reply": "hi"})
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert cli.main(["chat", "hello there"]) == 0
    capsys.readouterr()
    assert cli.main(["resume"]) == 0

    assert calls[-1] == ("GET", "http://127.0.0.1:8000/threads/thread-1/messages")
    assert capsys.readouterr().out == "user: hello there\nassistant: hi\n"


def test_resume_thread_id_remembers_selected_thread(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads/thread-2/messages"):
            return _Response(body=[{"role": "user", "content": "second thread"}])
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert cli.main(["--json", "resume", "thread-2"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["thread_id"] == "thread-2"

    assert cli.main(["--json", "threads", "list"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["threads"][0]["thread_id"] == "thread-2"
    assert output["threads"][0]["title"] == "second thread"


def test_export_thread_as_markdown(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads/thread-2/messages"):
            return _Response(
                body=[
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ]
            )
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert cli.main(["export", "thread-2"]) == 0

    assert capsys.readouterr().out == (
        "# Minigent transcript\n\nThread: `thread-2`\n\n## User\n\nhello\n\n## Assistant\n\nhi\n"
    )


def test_export_thread_as_json(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    messages = [{"role": "user", "content": "hello"}]

    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads/thread-2/messages"):
            return _Response(body=messages)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert cli.main(["--json", "export", "thread-2", "--format", "json"]) == 0

    assert json.loads(capsys.readouterr().out) == {"thread_id": "thread-2", "messages": messages}


def test_cli_prints_friendly_auth_errors(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    def urlopen(request: Any) -> _Response:
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            Message(),
            BytesIO(b'{"detail":"invalid token"}'),
        )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["chat", "hello"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error: Authentication failed." in captured.err
    assert "invalid token" not in captured.err


def test_cli_verbose_errors_include_technical_detail(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        raise urllib.error.HTTPError(
            request.full_url,
            502,
            "Bad Gateway",
            Message(),
            BytesIO(b'{"detail":"LLM provider returned no message content"}'),
        )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--verbose", "chat", "hello"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert (
        "Error: Minigent server error (502). LLM provider returned no message content"
        in captured.err
    )
    assert "Detail: POST http://127.0.0.1:8000/threads failed: 502" in captured.err


def test_cli_json_errors_are_structured(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    def urlopen(request: Any) -> _Response:
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--json", "health"])

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "error": {
            "category": "server_unavailable",
            "message": "Cannot reach the Minigent API. Check --base-url and make sure the server is running.",
        }
    }


def test_config_doctor_reports_local_and_server_checks(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str]] = []
    config_response = {
        "llm": {"provider": "mock", "model": "mock-model"},
        "agent_backend": {"type": "native", "mcp_broker_enabled": False},
        "quality": {"enabled": False},
        "mcp_servers": [],
        "local_tools": ["echo"],
    }

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/health"):
            return _Response(body={"status": "ok"})
        if request.full_url.endswith("/config"):
            return _Response(body=config_response)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["config", "doctor"])

    assert exit_code == 0
    assert calls == [
        ("GET", "http://127.0.0.1:8000/health"),
        ("GET", "http://127.0.0.1:8000/config"),
    ]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Minigent config doctor" in captured.out
    assert "✓ Base URL configured: http://127.0.0.1:8000" in captured.out
    assert (
        "✓ Trusted principal headers configured: user=demo-user tenant=demo-tenant" in captured.out
    )
    assert "✓ API reachable: ok" in captured.out
    assert "✓ Backend mode: native" in captured.out
    assert "✓ Default model configured: mock-model" in captured.out
    assert "⚠ MCP broker enabled: false or not reported" in captured.out
    assert "No blocking issues found." in captured.out


def test_config_doctor_returns_nonzero_for_bad_base_url(capsys: Any) -> None:
    exit_code = cli.main(["--base-url", "not-a-url", "config", "doctor"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "✗ Base URL configured: Use an http:// or https:// URL." in captured.out
    assert "Blocking issues found." in captured.out


def test_ping_json_reports_server_summary(monkeypatch: Any, capsys: Any) -> None:
    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/health"):
            return _Response(body={"status": "ok"})
        if request.full_url.endswith("/config"):
            return _Response(
                body={
                    "llm": {"provider": "mock", "model": "mock-model"},
                    "agent_backend": {"type": "peer_agent"},
                }
            )
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--json", "ping"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["server"] == {
        "backend": "peer_agent",
        "model": "mock-model",
        "provider": "mock",
    }
    assert output["checks"] == [
        {"status": "ok", "label": "API reachable", "blocking": False, "detail": "ok"},
        {"status": "ok", "label": "Server config readable", "blocking": False},
    ]


def test_ping_returns_nonzero_for_unreachable_server(monkeypatch: Any, capsys: Any) -> None:
    def urlopen(request: Any) -> _Response:
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["ping"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "✗ API reachable: Cannot reach the Minigent API." in captured.out


def test_debug_bundle_json_masks_secrets_and_reports_diagnostics(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/health"):
            return _Response(body={"status": "ok"})
        if request.full_url.endswith("/config"):
            return _Response(
                body={
                    "llm": {"provider": "mock", "model": "mock-model", "api_key": "secret"},
                    "agent_backend": {"type": "peer_agent", "mcp_broker_enabled": True},
                    "mcp_servers": [{"name": "home", "token": "secret-token"}],
                }
            )
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MINIGENT_API_TOKEN", "env-secret")
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--api-token", "cli-secret", "debug-bundle", "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["client"]["auth"] == {
        "mode": "bearer_token",
        "api_token": "<set>",
        "user_id": "demo-user",
        "tenant_id": "demo-tenant",
        "admin": False,
    }
    assert output["server"]["summary"] == {
        "backend": "peer_agent",
        "model": "mock-model",
        "provider": "mock",
    }
    assert output["server"]["config"]["llm"]["api_key"] == "<set>"
    assert output["server"]["config"]["mcp_servers"][0]["token"] == "<set>"
    assert output["mcp"] == {"broker_enabled": True, "server_count": 1}
    dumped = json.dumps(output)
    assert "cli-secret" not in dumped
    assert "env-secret" not in dumped


def test_debug_bundle_output_writes_human_report(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/health"):
            return _Response(body={"status": "ok"})
        if request.full_url.endswith("/config"):
            return _Response(
                body={
                    "llm": {"provider": "mock", "model": "mock-model"},
                    "agent_backend": {"type": "native", "mcp_broker_enabled": False},
                    "mcp_servers": [],
                }
            )
        raise AssertionError(f"Unexpected request: {request.full_url}")

    output_path = tmp_path / "bundle.txt"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["debug-bundle", "--output", str(output_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == f"Wrote debug bundle to {output_path}\n"
    report = output_path.read_text(encoding="utf-8")
    assert "Minigent debug bundle" in report
    assert "backend: native" in report
    assert "model: mock-model" in report


def test_admin_tenants_list_sends_filters(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []
    response = {
        "tenants": [
            {
                "id": "tenant-a",
                "slug": "tenant-a",
                "name": "Tenant A",
                "status": "active",
                "plan": "pro",
                "region": "us",
                "metadata": {},
                "created_at": "2026-05-19T10:00:00Z",
                "updated_at": "2026-05-19T10:01:00Z",
            }
        ],
        "limit": 10,
        "offset": 20,
        "total": 42,
        "next_offset": 30,
    }

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url, dict(request.header_items())))
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "admin",
            "tenants",
            "list",
            "--limit",
            "10",
            "--offset",
            "20",
            "--status",
            "active",
            "--plan",
            "pro",
            "--slug",
            "tenant-a",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:8000/admin/tenants?limit=10&offset=20&status=active&plan=pro&slug=tenant-a",
            calls[0][2],
        )
    ]
    assert calls[0][2]["X-minigent-admin"] == "true"
    output = capsys.readouterr().out
    assert "total=42 limit=10 offset=20 next_offset=30" in output
    assert "tenant-a slug=tenant-a name=Tenant A status=active plan=pro region=us" in output


def test_admin_tenants_create_json_sends_payload(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    response = {
        "id": "tenant-a",
        "slug": "tenant-a",
        "name": "Tenant A",
        "status": "active",
        "plan": "pro",
        "region": "us",
        "metadata": {"owner": "ops"},
        "created_at": "2026-05-19T10:00:00Z",
        "updated_at": "2026-05-19T10:00:00Z",
    }

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "--json",
            "admin",
            "tenants",
            "create",
            "--id",
            "tenant-a",
            "--slug",
            "tenant-a",
            "--name",
            "Tenant A",
            "--status",
            "active",
            "--plan",
            "pro",
            "--region",
            "us",
            "--metadata-json",
            '{"owner":"ops"}',
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8000/admin/tenants",
            {
                "id": "tenant-a",
                "slug": "tenant-a",
                "name": "Tenant A",
                "status": "active",
                "plan": "pro",
                "region": "us",
                "metadata": {"owner": "ops"},
            },
        )
    ]
    assert json.loads(capsys.readouterr().out) == response


def test_admin_tenants_update_and_transition(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        return _Response(
            body={
                "id": "tenant-a",
                "slug": payload.get("slug", "tenant-a"),
                "name": payload.get("name", "Tenant A"),
                "status": "active",
                "plan": payload.get("plan"),
                "region": None,
                "metadata": {},
                "created_at": "2026-05-19T10:00:00Z",
                "updated_at": "2026-05-19T10:01:00Z",
            }
        )

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    update_exit = cli.main(
        [
            "--admin",
            "admin",
            "tenants",
            "update",
            "tenant-a",
            "--slug",
            "tenant-renamed",
            "--name",
            "Tenant Renamed",
            "--plan",
            "pro",
        ]
    )
    activate_exit = cli.main(["--admin", "--json", "admin", "tenants", "activate", "tenant-a"])

    assert update_exit == 0
    assert activate_exit == 0
    assert calls[0] == (
        "PATCH",
        "http://127.0.0.1:8000/admin/tenants/tenant-a",
        {"slug": "tenant-renamed", "name": "Tenant Renamed", "plan": "pro"},
    )
    assert calls[1] == (
        "POST",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/activate",
        {},
    )
    output = capsys.readouterr().out
    assert "tenant-a slug=tenant-renamed name=Tenant Renamed status=active plan=pro" in output
    assert '"id": "tenant-a"' in output


def test_admin_tenants_delete_json(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str]] = []
    response = {"deleted": True, "tenant_id": "tenant-a", "status": "deleted"}

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--admin", "--json", "admin", "tenants", "delete", "tenant-a"])

    assert exit_code == 0
    assert calls == [("DELETE", "http://127.0.0.1:8000/admin/tenants/tenant-a")]
    assert json.loads(capsys.readouterr().out) == response


def test_admin_tenants_seed_sends_options(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    response = {
        "source": "execution-configs",
        "dry_run": True,
        "discovered": 2,
        "existing": 1,
        "created": 0,
        "conflicts": 0,
        "tenants": [
            {
                "id": "tenant-a",
                "slug": "tenant-a",
                "name": "tenant-a",
                "status": "active",
                "action": "exists",
            },
            {
                "id": "tenant-b",
                "slug": "tenant-b",
                "name": "tenant-b",
                "status": "active",
                "action": "would_create",
            },
        ],
    }

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "admin",
            "tenants",
            "seed",
            "--from",
            "execution-configs",
            "--status",
            "active",
            "--plan",
            "pro",
            "--region",
            "us",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8000/admin/tenants/seed",
            {
                "source": "execution-configs",
                "status": "active",
                "dry_run": True,
                "plan": "pro",
                "region": "us",
            },
        )
    ]
    output = capsys.readouterr().out
    assert "source=execution-configs discovered=2 existing=1 created=0 conflicts=0 dry_run=True" in output
    assert "tenant-b slug=tenant-b status=active action=would_create" in output


def test_admin_tenants_seed_json(monkeypatch: Any, capsys: Any) -> None:
    response = {
        "source": "execution-configs",
        "dry_run": False,
        "discovered": 1,
        "existing": 0,
        "created": 1,
        "conflicts": 0,
        "tenants": [
            {
                "id": "tenant-a",
                "slug": "tenant-a",
                "name": "tenant-a",
                "status": "active",
                "action": "created",
            }
        ],
    }

    def urlopen(request: Any) -> _Response:
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--admin", "--json", "admin", "tenants", "seed"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == response


def test_admin_threads_list_json(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []
    response = {
        "tenant_id": "tenant-a",
        "limit": 25,
        "offset": 0,
        "total": 1,
        "next_offset": None,
        "threads": [
            {
                "thread_id": "thread-1",
                "tenant_id": "tenant-a",
                "status": "idle",
                "created_at": "2026-05-19T10:00:00Z",
                "updated_at": "2026-05-19T10:01:00Z",
                "skill_name": None,
                "skill_names": None,
                "capability_profile": None,
                "message_count": 2,
            }
        ],
    }

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url, dict(request.header_items())))
        if request.full_url.endswith("/admin/tenants/tenant-a/threads"):
            return _Response(body=response)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--admin", "--json", "admin", "threads", "list", "--tenant", "tenant-a"])

    assert exit_code == 0
    assert calls[0][0:2] == ("GET", "http://127.0.0.1:8000/admin/tenants/tenant-a/threads")
    assert calls[0][2]["X-minigent-admin"] == "true"
    assert json.loads(capsys.readouterr().out) == response


def test_admin_threads_list_sends_pagination_and_filter_params(
    monkeypatch: Any, capsys: Any
) -> None:
    calls: list[str] = []
    response = {
        "tenant_id": "tenant-a",
        "limit": 10,
        "offset": 20,
        "total": 42,
        "next_offset": 30,
        "threads": [],
    }

    def urlopen(request: Any) -> _Response:
        calls.append(request.full_url)
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "admin",
            "threads",
            "list",
            "--tenant",
            "tenant-a",
            "--limit",
            "10",
            "--offset",
            "20",
            "--status",
            "idle",
            "--profile",
            "dev",
            "--skill",
            "coding",
            "--updated-after",
            "2026-05-19T10:00:00Z",
        ]
    )

    assert exit_code == 0
    assert calls == [
        "http://127.0.0.1:8000/admin/tenants/tenant-a/threads?limit=10&offset=20&status=idle&profile=dev&skill=coding&updated_after=2026-05-19T10%3A00%3A00Z"
    ]
    output = capsys.readouterr().out
    assert "tenant_id=tenant-a total=42 limit=10 offset=20 next_offset=30" in output


def test_admin_threads_delete_json(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str]] = []
    response = {"deleted": True, "tenant_id": "tenant-a", "thread_id": "thread-1"}

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/admin/tenants/tenant-a/threads/thread-1"):
            return _Response(body=response)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        ["--admin", "--json", "admin", "threads", "delete", "thread-1", "--tenant", "tenant-a"]
    )

    assert exit_code == 0
    assert calls == [("DELETE", "http://127.0.0.1:8000/admin/tenants/tenant-a/threads/thread-1")]
    assert json.loads(capsys.readouterr().out) == response


def test_admin_threads_prune_sends_filters(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str]] = []
    response = {
        "tenant_id": "tenant-a",
        "deleted_count": 3,
        "dry_run": False,
        "candidate_thread_ids": ["thread-1", "thread-2", "thread-3"],
        "updated_before": "2026-05-19T10:00:00Z",
    }

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "admin",
            "threads",
            "prune",
            "--tenant",
            "tenant-a",
            "--updated-before",
            "2026-05-19T10:00:00Z",
            "--status",
            "idle",
            "--profile",
            "dev",
            "--skill",
            "coding",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8000/admin/tenants/tenant-a/threads/prune?updated_before=2026-05-19T10%3A00%3A00Z&status=idle&profile=dev&skill=coding",
        )
    ]
    assert (
        capsys.readouterr().out
        == "tenant_id=tenant-a deleted_count=3 dry_run=False candidate_count=3 updated_before=2026-05-19T10:00:00Z\n"
    )


def test_admin_threads_prune_dry_run_json(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str]] = []
    response = {
        "tenant_id": "tenant-a",
        "deleted_count": 0,
        "dry_run": True,
        "candidate_thread_ids": ["thread-1"],
        "updated_before": "2026-05-19T10:00:00Z",
    }

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "--json",
            "admin",
            "threads",
            "prune",
            "--tenant",
            "tenant-a",
            "--updated-before",
            "2026-05-19T10:00:00Z",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8000/admin/tenants/tenant-a/threads/prune?updated_before=2026-05-19T10%3A00%3A00Z&dry_run=True",
        )
    ]
    assert json.loads(capsys.readouterr().out) == response


def test_admin_audit_list_text(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str]] = []
    response = {
        "tenant_id": "tenant-a",
        "limit": 10,
        "offset": 0,
        "total": 1,
        "next_offset": None,
        "audit_records": [
            {
                "audit_id": "audit-1",
                "tenant_id": "tenant-a",
                "actor_user_id": "admin-user",
                "action": "threads.delete",
                "affected_count": 1,
                "thread_ids": ["thread-1"],
                "created_at": "2026-05-19T10:00:00Z",
            }
        ],
    }

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "admin",
            "audit",
            "list",
            "--tenant",
            "tenant-a",
            "--limit",
            "10",
            "--action",
            "threads.delete",
            "--actor",
            "admin-user",
            "--created-after",
            "2026-05-19T09:00:00Z",
            "--created-before",
            "2026-05-19T11:00:00Z",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:8000/admin/tenants/tenant-a/audit-records?limit=10&action=threads.delete&actor=admin-user&created_after=2026-05-19T09%3A00%3A00Z&created_before=2026-05-19T11%3A00%3A00Z",
        )
    ]
    assert capsys.readouterr().out == (
        "tenant_id=tenant-a limit=10 offset=0 total=1 next_offset=None\n"
        "audit-1 action=threads.delete actor=admin-user affected_count=1 created_at=2026-05-19T10:00:00Z\n"
    )


def test_admin_threads_show_text(monkeypatch: Any, capsys: Any) -> None:
    response = {
        "thread_id": "thread-1",
        "tenant_id": "tenant-a",
        "status": "idle",
        "created_at": "2026-05-19T10:00:00Z",
        "updated_at": "2026-05-19T10:01:00Z",
        "skill_name": "coding",
        "skill_names": None,
        "capability_profile": "default",
        "message_count": 2,
        "context": {
            "summary": "Earlier summary.",
            "summarized_message_count": 0,
            "updated_at": "2026-05-19T10:01:00Z",
        },
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
    }

    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/admin/tenants/tenant-a/threads/thread-1"):
            return _Response(body=response)
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    assert (
        cli.main(["--admin", "admin", "threads", "show", "thread-1", "--tenant", "tenant-a"]) == 0
    )

    output = capsys.readouterr().out
    assert "thread_id=thread-1" in output
    assert "tenant_id=tenant-a" in output
    assert "message_count=2" in output
    assert "summary=Earlier summary." in output
    assert "user: hello" in output
    assert "assistant: hi" in output
