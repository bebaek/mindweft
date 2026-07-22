import json
import tomllib
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


def test_run_sends_image_parts(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    image_path = tmp_path / "tiny.png"
    image_path.write_bytes(b"hi")

    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/threads"):
            return _Response(body={"thread_id": "thread-1"})
        if request.full_url.endswith("/threads/thread-1/messages"):
            body = json.loads(request.data.decode("utf-8"))
            assert body == {
                "content": "describe",
                "parts": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image",
                        "mime_type": "image/png",
                        "data": "aGk=",
                        "detail": "high",
                    },
                ],
            }
            return _Response(body={"role": "user"})
        if request.full_url.endswith("/threads/thread-1/run"):
            return _Response(body={"reply": "image summary"})
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["run", "--image", str(image_path), "--image-detail", "high", "describe"])

    assert exit_code == 0
    assert capsys.readouterr().out == "image summary\n"


def test_run_json_outputs_structured_reply(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
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


def test_chat_stream_json_includes_usage(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
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


def test_config_init_writes_minigent_toml(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(["config", "init"])

    assert exit_code == 0
    assert (tmp_path / "minigent.toml").exists()
    assert "[llm]" in (tmp_path / "minigent.toml").read_text(encoding="utf-8")
    assert capsys.readouterr().out == "Wrote minigent.toml (local-coding)\n"


def test_config_init_writes_requested_profile(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(["config", "init", "--profile", "openrouter"])

    assert exit_code == 0
    content = (tmp_path / "minigent.toml").read_text(encoding="utf-8")
    assert 'profile = "openrouter"' in content
    assert 'provider = "openrouter"' in content
    assert 'api_key_env = "OPENROUTER_API_KEY"' in content
    assert capsys.readouterr().out == "Wrote minigent.toml (openrouter)\n"


def test_config_init_profile_supports_output_and_force(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    output_path = tmp_path / "custom.toml"
    output_path.write_text('profile = "old"\n', encoding="utf-8")

    exit_code = cli.main(
        ["config", "init", "--profile", "voice", "--output", str(output_path), "--force"]
    )

    assert exit_code == 0
    content = output_path.read_text(encoding="utf-8")
    assert 'profile = "voice"' in content
    assert "[voice]" in content
    assert capsys.readouterr().out == f"Wrote {output_path} (voice)\n"


def test_config_init_refuses_to_overwrite(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "minigent.toml").write_text('profile = "custom"\n', encoding="utf-8")

    exit_code = cli.main(["config", "init"])

    assert exit_code == 1
    assert "already exists; use --force" in capsys.readouterr().err
    assert (tmp_path / "minigent.toml").read_text(encoding="utf-8") == 'profile = "custom"\n'


def test_config_print_resolved_masks_secrets(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-value")
    (tmp_path / "minigent.toml").write_text(
        """
[llm]
provider = "openrouter"
model = "openrouter-model"
api_key_env = "OPENROUTER_API_KEY"

[app]
thread_db_path = ".data/minigent-threads.db"
""".strip(),
        encoding="utf-8",
    )

    exit_code = cli.main(["config", "print", "--resolved"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["config_file"] == str(tmp_path / "minigent.toml")
    assert output["resolved_env"]["MINIGENT_LLM_PROVIDER"] == "openrouter"
    assert output["resolved_env"]["OPENROUTER_MODEL"] == "openrouter-model"
    assert output["resolved_env"]["OPENROUTER_API_KEY"] == "<set>"
    assert "secret-value" not in json.dumps(output)


def test_config_export_prints_toml_from_server(monkeypatch: Any, capsys: Any) -> None:
    def urlopen(request: Any) -> _Response:
        assert request.full_url.endswith("/config?export=true")
        return _Response(
            body={
                "llm": {
                    "provider": "openrouter",
                    "model": "openrouter-model",
                    "base_url": "https://openrouter.ai/api/v1",
                },
                "quality": {"enabled": False},
                "agent_backend": {"mcp_broker_enabled": True},
                "mcp_servers": [
                    {
                        "name": "filesystem",
                        "url": "http://127.0.0.1:8765/mcp",
                        "headers": {"Authorization": "Bearer secret"},
                    }
                ],
                "unified_config_export": {
                    "oauth": {
                        "provider_id": "chatgpt",
                        "client_id": "client-id",
                        "authorize_url": "https://auth.example/authorize",
                        "token_url": "https://auth.example/token",
                    },
                    "image_input": {
                        "enabled": True,
                        "max_bytes": 123456,
                        "allowed_mime_types": ["image/png", "image/webp"],
                    },
                    "coding": {
                        "mcp_servers_file": "mcp-servers.json",
                        "mcp_server_specs": [
                            {
                                "name": "custom-stdio",
                                "transport": "stdio",
                                "command": ["uvx", "custom-mcp"],
                                "env": {"API_KEY": "<set>"},
                            }
                        ],
                    },
                    "tenant_execution_configs": {
                        "*": {
                            "skills": {
                                "default_skill": "coding-workspace",
                                "items": [
                                    {
                                        "name": "coding-workspace",
                                        "system_prompt": "You are a coding agent.",
                                        "mcp_server_names": ["filesystem"],
                                    }
                                ],
                            }
                        }
                    },
                    "runtime": {
                        "mcp_servers": [
                            {"name": "filesystem", "status": "connected", "tool_count": 1}
                        ],
                        "tools": [{"name": "filesystem.read_file", "description": "Read file"}],
                    },
                },
            }
        )

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["config", "export", "--include-runtime"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Generated from a running Minigent server" in output
    assert 'profile = "exported"' in output
    assert "[llm]" in output
    assert 'provider = "openrouter"' in output
    assert 'api_key_env = "OPENROUTER_API_KEY"' in output
    assert "[mcp]" in output
    assert "broker_enabled = true" in output
    assert 'Authorization = "<set>"' not in output
    assert "[oauth]" in output
    assert 'provider_id = "chatgpt"' in output
    assert "[image_input]" in output
    assert "enabled = true" in output
    assert "max_bytes = 123456" in output
    assert 'allowed_mime_types = ["image/png", "image/webp"]' in output
    assert "[[coding.mcp_server_specs]]" in output
    assert "mcp_servers_file" not in output
    assert 'transport = "stdio"' in output
    assert 'command = ["uvx", "custom-mcp"]' in output
    assert "tenant_execution_configs" in output
    assert "coding-workspace" in output
    assert "system_prompt" in output
    assert "runtime" in output
    assert "filesystem.read_file" in output
    assert "[[mcp.servers]]" not in output
    assert 'model = "None"' not in output
    assert 'base_url = "None"' not in output
    parsed = tomllib.loads(output)
    assert parsed["oauth"]["provider_id"] == "chatgpt"
    assert parsed["image_input"]["enabled"] is True
    assert parsed["image_input"]["allowed_mime_types"] == ["image/png", "image/webp"]
    assert parsed["coding"]["mcp_server_specs"][0]["transport"] == "stdio"
    assert parsed["coding"]["mcp_server_specs"][0]["command"] == ["uvx", "custom-mcp"]
    assert parsed["tenant_execution_configs"]["*"]["skills"]["default_skill"] == "coding-workspace"
    assert parsed["runtime"]["tools"][0]["name"] == "filesystem.read_file"
    assert "secret" not in output


def test_config_export_warns_when_api_only_coding_gateway_urls(
    monkeypatch: Any, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        assert request.full_url.endswith("/config?export=true")
        return _Response(
            body={
                "llm": {"provider": "mock"},
                "unified_config_export": {
                    "tenant_execution_configs": {
                        "demo-tenant": {
                            "tools": {
                                "mcp_servers": [
                                    {
                                        "name": "text-workspace",
                                        "url": "http://127.0.0.1:8765/mcp/text-workspace",
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        )

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["config", "export"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "API-only export includes tenant MCP gateway URLs" in output
    assert "--local-coding" in output
    assert "[[coding.mcp_server_specs]]" not in output
    parsed = tomllib.loads(output)
    assert "coding" not in parsed


def test_config_export_local_coding_merges_runner_config(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        assert request.full_url.endswith("/config?export=true")
        return _Response(body={"llm": {"provider": "mock"}})

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "web-fetch",
                        "transport": "stdio",
                        "command": ["uvx", "mcp-server-fetch"],
                        "env": {"FETCH_TOKEN": "secret-value"},
                        "profiles": ["inspect"],
                        "allowed_tools": ["fetch"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env.coding"
    env_file.write_text(
        "\n".join(
            [
                f"MINIGENT_CODING_WORKSPACES={workspace}",
                "MINIGENT_THREAD_DB_PATH=.data/minigent-coding-threads.db",
                "MINIGENT_MAX_ITERATIONS=64",
                "MINIGENT_TOOL_TIMEOUT_SECONDS=90",
                "MINIGENT_CONTEXT_COMPACTION_ENABLED=true",
                "MINIGENT_CODING_TENANT_ID=coding-tenant",
                "MINIGENT_CODING_MCP_GATEWAY_ENABLED=true",
                "MINIGENT_CODING_MCP_GATEWAY_PORT=9876",
                "MINIGENT_CODING_MCP_GATEWAY_PATH_PREFIX=/tools",
                "MINIGENT_CODING_MCP_SERVERS_FILE=mcp-servers.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["config", "export", "--local-coding", "--coding-env-file", str(env_file)])

    assert exit_code == 0
    output = capsys.readouterr().out
    parsed = tomllib.loads(output)
    assert parsed["profile"] == "exported-coding"
    assert parsed["app"]["thread_db_path"] == ".data/minigent-coding-threads.db"
    assert parsed["app"]["max_iterations"] == 64
    assert parsed["app"]["tool_timeout_seconds"] == 90
    assert parsed["app"]["context_compaction_enabled"] is True
    assert parsed["coding"]["workspaces"] == [str(workspace)]
    assert parsed["coding"]["tenant_id"] == "coding-tenant"
    assert parsed["coding"]["mcp_gateway_enabled"] is True
    assert parsed["coding"]["mcp_gateway_port"] == 9876
    assert parsed["coding"]["mcp_gateway_path_prefix"] == "/tools"
    assert parsed["coding"]["mcp_server_specs"][0]["name"] == "web-fetch"
    assert parsed["coding"]["mcp_server_specs"][0]["env"] == {"FETCH_TOKEN": "${FETCH_TOKEN}"}
    assert "mcp_servers_file" not in output
    assert "secret-value" not in output


def test_config_export_local_coding_unifies_split_mcp_server_config(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def urlopen(request: Any) -> _Response:
        assert request.full_url.endswith("/config?export=true")
        return _Response(
            body={
                "llm": {"provider": "mock"},
                "unified_config_export": {
                    "coding": {
                        "system_prompt": "You may use read_file and write_file.",
                    },
                    "tenant_execution_configs": {
                        "coding-tenant": {
                            "tools": {
                                "allowed_local_tools": ["calculator"],
                                "mcp_servers": [
                                    {
                                        "name": "fs-workspace",
                                        "url": "http://127.0.0.1:8765/mcp/fs-workspace",
                                        "allowed_tools": ["write_file", "search_files"],
                                        "path_policy": {"deny_globs": ["**/.env*"]},
                                    }
                                ],
                            },
                            "skills": {"default_skill": "coding-workspace"},
                        }
                    },
                },
            }
        )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    specs_path = tmp_path / "mcp-servers.json"
    specs_path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "fs-workspace",
                        "transport": "stdio",
                        "command": ["uvx", "mcp-server-fs"],
                        "allowed_tools": ["read_file", "write_file", "edit_file"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env.coding"
    env_file.write_text(
        "\n".join(
            [
                f"MINIGENT_CODING_WORKSPACES={workspace}",
                "MINIGENT_CODING_TENANT_ID=coding-tenant",
                "MINIGENT_CODING_MCP_SERVERS_FILE=mcp-servers.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["config", "export", "--local-coding", "--coding-env-file", str(env_file)])

    assert exit_code == 0
    output = capsys.readouterr().out
    parsed = tomllib.loads(output)
    assert parsed["coding"]["system_prompt"] == "You may use read_file and write_file."
    assert parsed["coding"]["mcp_server_specs"][0]["allowed_tools"] == ["write_file"]
    assert parsed["coding"]["mcp_server_specs"][0]["path_policy"] == {"deny_globs": ["**/.env*"]}
    assert "mcp_servers" not in parsed["tenant_execution_configs"]["coding-tenant"]["tools"]
    assert parsed["tenant_execution_configs"]["coding-tenant"]["tools"] == {
        "allowed_local_tools": ["calculator"]
    }
    assert "exported their intersection" in output


def test_config_export_writes_json(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    def urlopen(request: Any) -> _Response:
        assert request.full_url.endswith("/config?export=true")
        return _Response(body={"llm": {"provider": "google", "model": "gemini-model"}})

    output_path = tmp_path / "export.json"
    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--json", "config", "export", "--output", str(output_path)])

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["profile"] == "exported"
    assert output["llm"] == {
        "provider": "google",
        "model": "gemini-model",
        "api_key_env": "GEMINI_API_KEY",
    }


def test_config_print_resolved_uses_custom_env_file(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_DOTENV_FILE", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_MODEL", raising=False)
    (tmp_path / "minigent.toml").write_text(
        """
[llm]
provider = "openrouter"
model = "from-toml"
""".strip(),
        encoding="utf-8",
    )
    custom_env = tmp_path / "custom.env"
    custom_env.write_text("MINIGENT_LLM_PROVIDER=mock\n", encoding="utf-8")

    exit_code = cli.main(["--env-file", str(custom_env), "config", "print", "--resolved"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["dotenv_file_env"] == str(custom_env)
    assert output["dotenv_file"] == str(custom_env)
    assert output["resolved_env"]["MINIGENT_LLM_PROVIDER"] == "mock"
    assert output["resolved_env"]["MINIGENT_LLM_MODEL"] == "from-toml"


def test_config_doctor_reports_unified_config_checks(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_MODEL", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_URL", raising=False)
    (tmp_path / "minigent.toml").write_text(
        f"""
profile = "local-coding"

[llm]
provider = "mock"

[coding]
enabled = true
workspaces = ["{tmp_path}"]

[mcp]
servers = [{{ name = "filesystem", url = "http://127.0.0.1:8765/mcp" }}]
""".strip(),
        encoding="utf-8",
    )

    def urlopen(request: Any) -> _Response:
        if request.full_url.endswith("/health"):
            return _Response(body={"status": "ok"})
        if request.full_url.endswith("/config"):
            return _Response(
                body={
                    "llm": {"provider": "mock", "model": "mock-model"},
                    "agent_backend": {"type": "native", "mcp_broker_enabled": False},
                    "quality": {"enabled": False},
                    "mcp_servers": [],
                }
            )
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["config", "doctor"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "✓ Unified config parses:" in output
    assert "✓ LLM provider: mock" in output
    assert "✓ Coding workspace paths: 1 configured" in output
    assert "✓ MCP server config: 1 configured" in output


def test_config_doctor_blocks_on_invalid_unified_config(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "minigent.toml").write_text('[llm\nprovider = "mock"\n', encoding="utf-8")

    exit_code = cli.main(["config", "doctor"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "✗ Unified config parses:" in output
    assert "Blocking issues found." in output


def test_config_doctor_blocks_when_provider_key_missing(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_DOTENV_FILE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MINIGENT_LLM_PROVIDER", raising=False)
    (tmp_path / "minigent.toml").write_text(
        """
[llm]
provider = "openrouter"
model = "openrouter-model"
""".strip(),
        encoding="utf-8",
    )

    exit_code = cli.main(["config", "doctor"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "✓ LLM provider: openrouter" in output
    assert "✗ LLM API key configured: set one of: OPENROUTER_API_KEY" in output


def test_config_doctor_blocks_on_malformed_mcp_servers(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "minigent.toml").write_text(
        """
[llm]
provider = "mock"

[mcp]
servers = [{ name = "dup", url = "http://one.example/mcp" }, { name = "dup" }]
""".strip(),
        encoding="utf-8",
    )

    exit_code = cli.main(["config", "doctor"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "✗ MCP server config:" in output
    assert "#1: missing url" in output
    assert "duplicate names: dup" in output


def test_config_doctor_reports_local_and_server_checks(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    # Isolate from cwd-local and user-level Minigent config files.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINIGENT_CONFIG_FILE", raising=False)
    monkeypatch.delenv("MINIGENT_DOTENV_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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


def test_debug_bundle_output_writes_human_report(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
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
    assert (
        "source=execution-configs discovered=2 existing=1 created=0 conflicts=0 dry_run=True"
        in output
    )
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


def test_admin_execution_config_import_dry_run_validates_file(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    config_path = tmp_path / "tenant-config.json"
    config_path.write_text(
        json.dumps({"tenant-a": {"llm": {"provider": "mock"}}}),
        encoding="utf-8",
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        assert request.full_url.endswith("/admin/tenants/tenant-a/execution-config/validate")
        return _Response(body={"valid": True, "checks": []})

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "admin",
            "execution-config",
            "import",
            str(config_path),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8000/admin/tenants/tenant-a/execution-config/validate",
            {"config": {"llm": {"provider": "mock"}}},
        )
    ]
    output = capsys.readouterr().out
    assert "tenant_count=1 valid=1 invalid=0 written=0 dry_run=True" in output
    assert "tenant-a valid=True written=False" in output


def test_admin_execution_config_import_upsert_and_seed(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    config_path = tmp_path / "tenant-config.json"
    config_path.write_text(
        json.dumps({"execution_configs": {"tenant-a": {"llm": {"provider": "mock"}}}}),
        encoding="utf-8",
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        if request.full_url.endswith("/admin/tenants/tenant-a/execution-config/validate"):
            return _Response(body={"valid": True})
        if request.full_url.endswith("/admin/tenants/tenant-a/execution-config"):
            return _Response(body={"tenant_id": "tenant-a", "config": payload["config"]})
        if request.full_url.endswith("/admin/tenants/seed"):
            return _Response(
                body={
                    "source": "execution-configs",
                    "discovered": 1,
                    "existing": 0,
                    "created": 1,
                    "conflicts": 0,
                    "dry_run": False,
                    "tenants": [],
                }
            )
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "admin",
            "execution-config",
            "import",
            str(config_path),
            "--upsert",
            "--seed-tenants",
            "--plan",
            "pro",
        ]
    )

    assert exit_code == 0
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8000/admin/tenants/tenant-a/execution-config/validate",
            {"config": {"llm": {"provider": "mock"}}},
        ),
        (
            "PUT",
            "http://127.0.0.1:8000/admin/tenants/tenant-a/execution-config",
            {"config": {"llm": {"provider": "mock"}}},
        ),
        (
            "POST",
            "http://127.0.0.1:8000/admin/tenants/seed",
            {
                "source": "execution-configs",
                "status": "active",
                "dry_run": False,
                "plan": "pro",
            },
        ),
    ]
    output = capsys.readouterr().out
    assert "tenant_count=1 valid=1 invalid=0 written=1 dry_run=False" in output
    assert "seed created=1 existing=0 conflicts=0" in output


def test_admin_execution_config_export_writes_redacted_json(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    output_path = tmp_path / "export.json"
    calls: list[tuple[str, str]] = []

    def urlopen(request: Any) -> _Response:
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/admin/execution-config-tenants"):
            return _Response(body={"tenants": ["tenant-a"]})
        if request.full_url.endswith("/admin/tenants/tenant-a/execution-config"):
            return _Response(
                body={
                    "tenant_id": "tenant-a",
                    "config": {"llm": {"provider": "openai", "api_key": "[REDACTED]"}},
                }
            )
        raise AssertionError(f"Unexpected request: {request.full_url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(
        [
            "--admin",
            "admin",
            "execution-config",
            "export",
            "--out",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert calls == [
        ("GET", "http://127.0.0.1:8000/admin/execution-config-tenants"),
        ("GET", "http://127.0.0.1:8000/admin/tenants/tenant-a/execution-config"),
    ]
    assert json.loads(output_path.read_text(encoding="utf-8")) == {
        "tenant-a": {"llm": {"provider": "openai", "api_key": "[REDACTED]"}}
    }
    assert capsys.readouterr().out == (
        f"Wrote execution configs for 1 tenant(s) to {output_path}\n"
    )


def test_admin_tenant_users_create_list_and_show(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    user = {
        "id": "membership-1",
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "email": "user@example.com",
        "display_name": "User One",
        "role": "admin",
        "status": "active",
        "metadata": {"team": "engineering"},
        "created_at": "2026-05-19T10:00:00Z",
        "updated_at": "2026-05-19T10:00:00Z",
    }

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        if request.full_url.startswith("http://127.0.0.1:8000/admin/tenants/tenant-a/users?"):
            return _Response(
                body={
                    "tenant_id": "tenant-a",
                    "users": [user],
                    "limit": 10,
                    "offset": 0,
                    "total": 1,
                    "next_offset": None,
                }
            )
        return _Response(body=user)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    create_exit = cli.main(
        [
            "--admin",
            "admin",
            "tenants",
            "users",
            "create",
            "tenant-a",
            "--user-id",
            "user-1",
            "--email",
            "user@example.com",
            "--display-name",
            "User One",
            "--role",
            "admin",
            "--status",
            "active",
            "--metadata-json",
            '{"team":"engineering"}',
        ]
    )
    list_exit = cli.main(
        [
            "--admin",
            "admin",
            "tenants",
            "users",
            "list",
            "tenant-a",
            "--limit",
            "10",
            "--status",
            "active",
            "--role",
            "admin",
            "--email",
            "user@example.com",
        ]
    )
    show_exit = cli.main(
        ["--admin", "--json", "admin", "tenants", "users", "show", "tenant-a", "membership-1"]
    )

    assert create_exit == 0
    assert list_exit == 0
    assert show_exit == 0
    assert calls[0] == (
        "POST",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/users",
        {
            "user_id": "user-1",
            "email": "user@example.com",
            "display_name": "User One",
            "role": "admin",
            "status": "active",
            "metadata": {"team": "engineering"},
        },
    )
    assert calls[1] == (
        "GET",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/users?limit=10&status=active&role=admin&email=user%40example.com",
        {},
    )
    assert calls[2] == (
        "GET",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/users/membership-1",
        {},
    )
    output = capsys.readouterr().out
    assert "membership-1 tenant_id=tenant-a user_id=user-1" in output
    assert '"id": "membership-1"' in output


def test_admin_tenant_users_update_transitions_and_delete(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        if request.get_method() == "DELETE":
            return _Response(
                body={
                    "deleted": True,
                    "tenant_id": "tenant-a",
                    "id": "membership-1",
                    "status": "deleted",
                }
            )
        return _Response(
            body={
                "id": "membership-1",
                "tenant_id": "tenant-a",
                "user_id": "user-1",
                "email": "new@example.com",
                "display_name": "User Renamed",
                "role": "viewer",
                "status": "active",
                "metadata": {"team": "support"},
                "created_at": "2026-05-19T10:00:00Z",
                "updated_at": "2026-05-19T11:00:00Z",
            }
        )

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    update_exit = cli.main(
        [
            "--admin",
            "--json",
            "admin",
            "tenants",
            "users",
            "update",
            "tenant-a",
            "membership-1",
            "--email",
            "new@example.com",
            "--display-name",
            "User Renamed",
            "--role",
            "viewer",
            "--status",
            "active",
            "--metadata-json",
            '{"team":"support"}',
        ]
    )
    activate_exit = cli.main(
        ["--admin", "admin", "tenants", "users", "activate", "tenant-a", "membership-1"]
    )
    suspend_exit = cli.main(
        ["--admin", "admin", "tenants", "users", "suspend", "tenant-a", "membership-1"]
    )
    delete_exit = cli.main(
        ["--admin", "--json", "admin", "tenants", "users", "delete", "tenant-a", "membership-1"]
    )

    assert update_exit == 0
    assert activate_exit == 0
    assert suspend_exit == 0
    assert delete_exit == 0
    assert calls[0] == (
        "PATCH",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/users/membership-1",
        {
            "email": "new@example.com",
            "display_name": "User Renamed",
            "role": "viewer",
            "status": "active",
            "metadata": {"team": "support"},
        },
    )
    assert calls[1] == (
        "POST",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/users/membership-1/activate",
        {},
    )
    assert calls[2] == (
        "POST",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/users/membership-1/suspend",
        {},
    )
    assert calls[3] == (
        "DELETE",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/users/membership-1",
        {},
    )
    assert '"deleted": true' in capsys.readouterr().out


def test_admin_tenant_entitlements_set_and_show(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    response = {
        "tenant_id": "tenant-a",
        "features": {"mcp": True},
        "limits": {"max_threads": 100},
        "version": 1,
        "updated_at": "2026-05-19T10:00:00Z",
    }

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    set_exit = cli.main(
        [
            "--admin",
            "admin",
            "tenants",
            "entitlements",
            "set",
            "tenant-a",
            "--features-json",
            '{"mcp":true}',
            "--limits-json",
            '{"max_threads":100}',
        ]
    )
    show_exit = cli.main(
        ["--admin", "--json", "admin", "tenants", "entitlements", "show", "tenant-a"]
    )

    assert set_exit == 0
    assert show_exit == 0
    assert calls[0] == (
        "PUT",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/entitlements",
        {"features": {"mcp": True}, "limits": {"max_threads": 100}},
    )
    assert calls[1] == (
        "GET",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/entitlements",
        {},
    )
    output = capsys.readouterr().out
    assert "tenant_id=tenant-a version=1 updated_at=2026-05-19T10:00:00Z" in output
    assert '"tenant_id": "tenant-a"' in output
    assert '"max_threads": 100' in output


def test_admin_tenant_entitlements_validate_and_delete(monkeypatch: Any, capsys: Any) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def urlopen(request: Any) -> _Response:
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}
        calls.append((request.get_method(), request.full_url, payload))
        if request.get_method() == "DELETE":
            return _Response(body=None)
        return _Response(
            body={
                "valid": True,
                "features": {"ok": True, "errors": []},
                "limits": {"ok": True, "errors": []},
            }
        )

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    validate_exit = cli.main(
        [
            "--admin",
            "--json",
            "admin",
            "tenants",
            "entitlements",
            "validate",
            "tenant-a",
            "--features-json",
            '{"mcp":true}',
        ]
    )
    assert validate_exit == 0
    validate_output = capsys.readouterr().out
    assert '"valid": true' in validate_output

    delete_exit = cli.main(
        ["--admin", "--json", "admin", "tenants", "entitlements", "delete", "tenant-a"]
    )
    assert delete_exit == 0
    assert calls[0] == (
        "POST",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/entitlements/validate",
        {"features": {"mcp": True}, "limits": {}},
    )
    assert calls[1] == (
        "DELETE",
        "http://127.0.0.1:8000/admin/tenants/tenant-a/entitlements",
        {},
    )
    delete_output = json.loads(capsys.readouterr().out)
    assert delete_output == {"deleted": True, "tenant_id": "tenant-a"}


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


def test_admin_audit_list_json_passes_structured_fields(monkeypatch: Any, capsys: Any) -> None:
    response = {
        "tenant_id": "tenant-a",
        "limit": 50,
        "offset": 0,
        "total": 1,
        "next_offset": None,
        "audit_records": [
            {
                "audit_id": "audit-1",
                "tenant_id": "tenant-a",
                "actor_user_id": "admin-user",
                "action": "tenants.update",
                "affected_count": 1,
                "thread_ids": [],
                "resource_type": "tenant",
                "resource_id": "tenant-a",
                "old_values": {"status": "active"},
                "new_values": {"status": "suspended"},
                "metadata": {"reason": "test"},
                "created_at": "2026-05-19T10:00:00Z",
            }
        ],
    }

    def urlopen(request: Any) -> _Response:
        return _Response(body=response)

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)

    exit_code = cli.main(["--admin", "--json", "admin", "audit", "list", "--tenant", "tenant-a"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == response


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
