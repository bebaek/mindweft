import json

import pytest

from app import cli


def test_chat_creates_thread_and_prints_reply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None, dict[str, str] | None]] = []

    def fake_request_json(
        method: str,
        url: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        calls.append((method, url, payload, headers))
        if method == "POST" and url == "http://127.0.0.1:8000/threads":
            return {"thread_id": "thread-123"}
        if method == "POST" and url.endswith("/messages"):
            return {"id": "message-1"}
        if method == "POST" and url.endswith("/run"):
            return {"reply": "Mock reply: hello"}
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    exit_code = cli.main(["chat", "--print-thread-id", "hello"])

    assert exit_code == 0
    assert capsys.readouterr().out == "thread_id=thread-123\nMock reply: hello\n"
    assert calls == [
        (
            "POST",
            "http://127.0.0.1:8000/threads",
            None,
            {
                "X-Minigent-User-Id": "demo-user",
                "X-Minigent-Tenant-Id": "demo-tenant",
                "X-Minigent-Admin": "false",
            },
        ),
        (
            "POST",
            "http://127.0.0.1:8000/threads/thread-123/messages",
            {"content": "hello"},
            {
                "X-Minigent-User-Id": "demo-user",
                "X-Minigent-Tenant-Id": "demo-tenant",
                "X-Minigent-Admin": "false",
            },
        ),
        (
            "POST",
            "http://127.0.0.1:8000/threads/thread-123/run",
            None,
            {
                "X-Minigent-User-Id": "demo-user",
                "X-Minigent-Tenant-Id": "demo-tenant",
                "X-Minigent-Admin": "false",
            },
        ),
    ]


def test_chat_json_can_include_transcript(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_request_json(
        method: str,
        url: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        del payload, headers
        if method == "POST" and url == "http://127.0.0.1:8000/threads":
            return {"thread_id": "thread-123"}
        if method == "POST" and url.endswith("/messages"):
            return {"id": "message-1"}
        if method == "POST" and url.endswith("/run"):
            return {"reply": "Mock reply: hello"}
        if method == "GET" and url.endswith("/messages"):
            return [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "Mock reply: hello"},
            ]
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    exit_code = cli.main(["--json", "chat", "--transcript", "hello"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "created_thread": True,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Mock reply: hello"},
        ],
        "reply": "Mock reply: hello",
        "thread_id": "thread-123",
    }


def test_threads_show_formats_tool_messages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_request_json(
        method: str,
        url: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        del payload, headers
        assert method == "GET"
        assert url == "http://127.0.0.1:8000/threads/thread-123/messages"
        return [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "", "tool_name": "calculator"},
            {"role": "tool", "content": '{"result": 3}', "tool_name": "calculator"},
            {"role": "assistant", "content": "3"},
        ]

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    exit_code = cli.main(["threads", "show", "thread-123"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "user: hello\n"
        "assistant (calculator): \n"
        'tool (calculator): {"result": 3}\n'
        "assistant: 3\n"
    )


def test_threads_delete_json_reports_deleted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_request_json(
        method: str,
        url: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        del payload, headers
        assert method == "DELETE"
        assert url == "http://127.0.0.1:8000/threads/thread-123"
        return None

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    exit_code = cli.main(["--json", "threads", "delete", "thread-123"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": True, "thread_id": "thread-123"}


def test_health_uses_bearer_token_auth(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured_headers: dict[str, str] | None = None

    def fake_request_json(
        method: str,
        url: str,
        *,
        payload: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        nonlocal captured_headers
        del payload
        assert method == "GET"
        assert url == "http://127.0.0.1:8000/health"
        captured_headers = headers
        return {"status": "ok"}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    exit_code = cli.main(["--api-token", "secret-token", "health"])

    assert exit_code == 0
    assert capsys.readouterr().out == "ok\n"
    assert captured_headers == {"Authorization": "Bearer secret-token"}
