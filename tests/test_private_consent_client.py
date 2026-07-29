from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import Any

from minigent_client.api_client import MinigentAPIClient
from minigent_client.cli import _maybe_resume_private_value_consent


class FakeAPIClient(MinigentAPIClient):
    def __init__(self, responses: list[object]) -> None:
        self._config = SimpleNamespace(base_url="http://minigent.test")
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, url, payload))
        return self.responses.pop(0)


def test_api_client_supports_consent_decision_and_resume() -> None:
    client = FakeAPIClient(
        [
            [{"consent_id": "consent-1"}],
            {"consent_id": "consent-1", "status": "approved"},
            {"reply": "Sent."},
        ]
    )

    assert client.list_pending_private_value_consents("thread-1") == [{"consent_id": "consent-1"}]
    assert (
        client.decide_private_value_consent(
            "consent-1",
            approve=True,
            thread_id="thread-1",
        )["status"]
        == "approved"
    )
    assert client.resume_private_value_consent(
        "consent-1",
        thread_id="thread-1",
    ) == ("Sent.", None)
    assert client.calls == [
        (
            "GET",
            "http://minigent.test/threads/thread-1/private-value-consents/pending",
            None,
        ),
        (
            "POST",
            "http://minigent.test/threads/thread-1/private-value-consents/consent-1",
            {"approve": True, "one_shot": True},
        ),
        (
            "POST",
            "http://minigent.test/threads/thread-1/private-value-consents/consent-1/resume",
            None,
        ),
    ]


class TTYInput(StringIO):
    def isatty(self) -> bool:
        return True


class FakeConsentClient:
    def __init__(self) -> None:
        self.decisions: list[tuple[str, bool, bool]] = []
        self.resumed: list[str] = []

    def list_pending_private_value_consents(self) -> list[dict[str, Any]]:
        return [
            {
                "consent_id": "consent-1",
                "tool_name": "trusted.send",
                "disclosures": [{"path": "recipient.email", "kind": "email", "count": 1}],
            }
        ]

    def decide_private_value_consent(
        self, consent_id: str, *, approve: bool, one_shot: bool
    ) -> None:
        self.decisions.append((consent_id, approve, one_shot))

    def resume_private_value_consent(self, consent_id: str) -> tuple[str, dict[str, Any] | None]:
        self.resumed.append(consent_id)
        return "Sent.", {"resumed": True}


def test_cli_approves_and_resumes_exact_private_tool_action() -> None:
    client = FakeConsentClient()
    output = StringIO()

    reply, metadata = _maybe_resume_private_value_consent(
        client,
        "Approval required.",
        None,
        input_stream=TTYInput("yes\n"),
        output_stream=output,
    )

    assert reply == "Sent."
    assert metadata == {"resumed": True}
    assert client.decisions == [("consent-1", True, True)]
    assert client.resumed == ["consent-1"]
    assert "1 email at recipient.email" in output.getvalue()
    assert "approved; resuming exact tool call" in output.getvalue()


def test_cli_denies_private_tool_action_without_resuming() -> None:
    client = FakeConsentClient()
    output = StringIO()

    reply, metadata = _maybe_resume_private_value_consent(
        client,
        "Approval required.",
        None,
        input_stream=TTYInput("no\n"),
        output_stream=output,
    )

    assert reply == "Approval required."
    assert metadata is None
    assert client.decisions == [("consent-1", False, True)]
    assert client.resumed == []
    assert "denied" in output.getvalue()
