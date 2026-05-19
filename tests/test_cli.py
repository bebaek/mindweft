import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from app import cli


class _Response:
    def __init__(self, *, body: object | None = None, lines: Sequence[Mapping[str, object]] | None = None) -> None:
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

    assert cli.main(["--admin", "admin", "threads", "show", "thread-1", "--tenant", "tenant-a"]) == 0

    output = capsys.readouterr().out
    assert "thread_id=thread-1" in output
    assert "tenant_id=tenant-a" in output
    assert "message_count=2" in output
    assert "summary=Earlier summary." in output
    assert "user: hello" in output
    assert "assistant: hi" in output
