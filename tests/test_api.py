from fastapi.testclient import TestClient

from app.main import create_app
from app.models import MessageRole


def test_thread_lifecycle_endpoints() -> None:
    client = TestClient(create_app())

    create_response = client.post("/threads")
    assert create_response.status_code == 200
    thread_id = create_response.json()["thread_id"]

    add_response = client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "hello"},
    )
    assert add_response.status_code == 200
    assert add_response.json()["role"] == MessageRole.USER

    run_response = client.post(f"/threads/{thread_id}/run")
    assert run_response.status_code == 200
    assert run_response.json() == {"reply": "Mock reply: hello"}

    messages_response = client.get(f"/threads/{thread_id}/messages")
    assert messages_response.status_code == 200
    messages = messages_response.json()
    assert [message["role"] for message in messages] == [MessageRole.USER, MessageRole.ASSISTANT]

    delete_response = client.delete(f"/threads/{thread_id}")
    assert delete_response.status_code == 204

    missing_response = client.get(f"/threads/{thread_id}/messages")
    assert missing_response.status_code == 404


def test_run_endpoint_handles_tool_call_flow() -> None:
    client = TestClient(create_app())
    thread_id = client.post("/threads").json()["thread_id"]

    client.post(
        f"/threads/{thread_id}/messages",
        json={"content": "/tool echo hello from api"},
    )

    run_response = client.post(f"/threads/{thread_id}/run")
    assert run_response.status_code == 200
    assert run_response.json() == {"reply": 'Tool result: {"echo": "hello from api"}'}

    messages = client.get(f"/threads/{thread_id}/messages").json()
    assert [message["role"] for message in messages] == [
        MessageRole.USER,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
